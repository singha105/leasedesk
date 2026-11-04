"""Shared helpers for the protocol spikes. Raw httpx on purpose: spikes exist to see the wire.

Secrets: agent keys live in .secrets/agents/<name>.json (mode 600) and are never printed.
Every HTTP exchange is appended, redacted, to logs/http.jsonl for docs/PROTOCOL_NOTES.md.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from leasedesk.common.logging import redact
from leasedesk.config import Settings

ROOT = Path(__file__).resolve().parents[2]
LOGS = ROOT / "logs"
STATE = ROOT / ".state"
AGENTS_DIR = ROOT / ".secrets" / "agents"


def settings() -> Settings:
    os.chdir(ROOT)  # so .env resolves
    return Settings()


def base_url() -> str:
    url = settings().switch_base_url
    if not url:
        raise SystemExit("SWITCH_BASE_URL is not set in .env (run scripts/spikes/health.py)")
    return url.rstrip("/")


def registration_key() -> str:
    key = settings().switch_registration_key
    if key is None or not key.get_secret_value():
        raise SystemExit(
            "SWITCH_REGISTRATION_KEY is empty. Load it with:\n"
            "  uv run python scripts/set_env_secret.py SWITCH_REGISTRATION_KEY --clipboard"
        )
    return key.get_secret_value()


@dataclass(frozen=True)
class AgentCreds:
    name: str
    id: str
    api_key: str

    def __repr__(self) -> str:  # never show the key
        return f"AgentCreds(name={self.name!r}, id={self.id!r}, api_key=[REDACTED])"


def agent_path(name: str) -> Path:
    return AGENTS_DIR / f"{name}.json"


def save_agent(name: str, data: dict[str, Any]) -> Path:
    for d in (ROOT / ".secrets", AGENTS_DIR):
        d.mkdir(mode=0o700, exist_ok=True)
        d.chmod(0o700)
    path = agent_path(name)
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    path.write_text(json.dumps(data, indent=2) + "\n")
    path.chmod(0o600)
    return path


def load_agent(name: str) -> AgentCreds:
    path = agent_path(name)
    if not path.exists():
        raise SystemExit(f"{path.relative_to(ROOT)} not found; run register.py --name {name}")
    data = json.loads(path.read_text())
    return AgentCreds(name=name, id=data["id"], api_key=data["api_key"])


def state_path(name: str) -> Path:
    STATE.mkdir(mode=0o700, exist_ok=True)
    return STATE / f"spike-{name}.json"


def load_state(name: str) -> dict[str, Any]:
    p = state_path(name)
    return json.loads(p.read_text()) if p.exists() else {}


def save_state(name: str, data: dict[str, Any]) -> None:
    p = state_path(name)
    p.write_text(json.dumps(data, indent=2) + "\n")
    p.chmod(0o600)


def _body(r: httpx.Response) -> Any:
    try:
        return r.json()
    except ValueError:
        return r.text[:2000]


def record(kind: str, r: httpx.Response, request_json: Any = None) -> None:
    """Append one redacted request/response pair to logs/http.jsonl."""
    LOGS.mkdir(exist_ok=True)
    entry = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "kind": kind,
        "request": {
            "method": r.request.method,
            "path": r.request.url.raw_path.decode(),
            "headers": redact(
                {k: v for k, v in r.request.headers.items() if k.lower().startswith(("x-", "auth"))}
            ),
            "json": redact(request_json),
        },
        "response": {"status": r.status_code, "json": redact(_body(r))},
    }
    with (LOGS / "http.jsonl").open("a") as fh:
        fh.write(json.dumps(entry) + "\n")


def client(bearer: str) -> httpx.Client:
    return httpx.Client(
        base_url=base_url(),
        headers={"Authorization": f"Bearer {bearer}"},
        timeout=20,
    )


def show(r: httpx.Response) -> str:
    """Status plus redacted body, safe to print."""
    return f"{r.status_code} {json.dumps(redact(_body(r)), indent=2)[:4000]}"
