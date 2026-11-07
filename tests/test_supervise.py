"""scripts/demo/supervise.py: restart with backoff, never restart 0/2/3/4, stop on Ctrl-C."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "supervise", ROOT / "scripts" / "demo" / "supervise.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["supervise"] = mod  # dataclasses look their module up while it loads
    spec.loader.exec_module(mod)
    return mod


sup = load()


def exits(code: int) -> list[str]:
    return [sys.executable, "-c", f"raise SystemExit({code})"]


async def test_crashing_child_restarts_with_backoff() -> None:
    said: list[str] = []
    delays: list[float] = []
    s = sup.Supervisor({"crashy": exits(1)}, say=said.append)

    async def fake_sleep(d: float) -> None:
        delays.append(d)
        if len(delays) == 7:
            s.request_stop()

    s.sleep = fake_sleep
    assert await asyncio.wait_for(s.run(), 30) == 0
    assert delays == [1, 2, 4, 8, 16, 30, 30]
    assert any("crashy exited 1" in line for line in said)


async def test_final_exit_codes_are_not_restarted() -> None:
    said: list[str] = []
    s = sup.Supervisor(
        {"cfg": exits(2), "taken": exits(3), "auth": exits(4), "done": exits(0)}, say=said.append
    )
    assert await asyncio.wait_for(s.run(), 30) == 1
    text = "\n".join(said)
    assert "cfg exited 2" in text and "configuration" in text
    assert "taken exited 3" in text and "auth exited 4" in text
    assert "not restarting" in text and "restarting in" not in text


async def test_stop_ends_running_children() -> None:
    s = sup.Supervisor(
        {"slow": [sys.executable, "-c", "import time; time.sleep(30)"]}, say=lambda _: None
    )
    task = asyncio.ensure_future(s.run())
    for _ in range(100):
        if s.children["slow"].proc is not None:
            break
        await asyncio.sleep(0.02)
    s.request_stop()
    assert await asyncio.wait_for(task, 15) == 0
    assert s.children["slow"].proc.returncode is not None
