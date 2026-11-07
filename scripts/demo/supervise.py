"""Run keymaster and sentinel together for the demo (``make up``) and keep them running.

A child that crashes (any exit code but 0, 2, 3 or 4) restarts after 1 s, then 2, 4, 8, 16
and 30 s at most; a child that ran for a minute or more starts again from 1 s. Exit codes 2
(configuration refused), 3 (taken over by another process) and 4 (Switch rejected the key)
are never restarted, because restarting can't fix them, and the reason is printed. 0 means
the child was asked to stop. Ctrl-C (or SIGTERM) stops both children and the supervisor.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

FINAL = {
    0: "it stopped on request",
    2: "configuration refused (see its log line above)",
    3: "another process took over this agent's connection",
    4: "Switch rejected the API key (run `make register`)",
}
BASE_DELAY_S = 1.0
MAX_DELAY_S = 30.0
HEALTHY_AFTER_S = 60.0
STOP_GRACE_S = 10.0


def _say(text: str) -> None:
    # Flushed so the lines show up at once when make up's output goes to a file.
    print(text, flush=True)


@dataclass
class Child:
    name: str
    argv: list[str]
    proc: asyncio.subprocess.Process | None = None
    failures: int = 0


class Supervisor:
    def __init__(
        self, children: dict[str, list[str]], *, say: Callable[[str], None] | None = None
    ) -> None:
        self.children = {name: Child(name, argv) for name, argv in children.items()}
        self.say = say or _say
        self.sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
        self._stop = asyncio.Event()

    def request_stop(self) -> None:
        self._stop.set()

    def _delay(self, child: Child) -> float:
        return min(MAX_DELAY_S, BASE_DELAY_S * 2 ** (child.failures - 1))

    async def _keep(self, child: Child) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            child.proc = await asyncio.create_subprocess_exec(*child.argv)
            code = await child.proc.wait()
            if self._stop.is_set():
                return
            if code in FINAL:
                self.say(f"supervise: {child.name} exited {code}: {FINAL[code]}; not restarting")
                return
            if time.monotonic() - started >= HEALTHY_AFTER_S:
                child.failures = 0
            child.failures += 1
            delay = self._delay(child)
            self.say(f"supervise: {child.name} exited {code}; restarting in {delay:g} s")
            await self.sleep(delay)

    async def _terminate(self, child: Child) -> None:
        proc = child.proc
        if proc is None or proc.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            proc.send_signal(signal.SIGINT)
        try:
            await asyncio.wait_for(proc.wait(), STOP_GRACE_S)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()

    async def run(self) -> int:
        """0 when stopped on request; 1 when every child ended for good on its own."""
        keepers = [asyncio.ensure_future(self._keep(c)) for c in self.children.values()]
        stop = asyncio.ensure_future(self._stop.wait())
        await asyncio.wait([stop, asyncio.gather(*keepers)], return_when=asyncio.FIRST_COMPLETED)
        stopped = self._stop.is_set()
        self._stop.set()
        await asyncio.gather(*(self._terminate(c) for c in self.children.values()))
        for task in [*keepers, stop]:
            task.cancel()
        await asyncio.gather(*keepers, stop, return_exceptions=True)
        return 0 if stopped else 1


async def _main() -> int:
    python = sys.executable
    sup = Supervisor(
        {
            "keymaster": [python, "-m", "leasedesk.keymaster"],
            "sentinel": [python, "-m", "leasedesk.sentinel"],
        }
    )
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, sup.request_stop)
    _say("supervise: keymaster and sentinel running; Ctrl-C stops both.")
    code = await sup.run()
    _say("supervise: stopped.")
    return code


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
