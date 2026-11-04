"""Act as the human admin on the Switch Gateway API (run this yourself, not an agent).

Why: Switch Console 0.40 runs the local server natively, and a native local server has no
Gateway web page (Console hides "Full admin interface"). These are the person-side actions the
spec puts "in the Gateway", done through the Gateway API instead:

    uv run python scripts/gateway.py key                       # registration key -> .env
    uv run python scripts/gateway.py addressing --agent leasedesk-probe --mode anyone
    uv run python scripts/gateway.py library-doc --room <room_id> --name human-note \
        --description "..." --instructions "..." --content-file note.md

Signs in with the admin login Console generated for the local server (read from Console's
switch-core.env), or with --prompt. Secrets are never printed; exchanges are appended,
redacted, to logs/http.jsonl.
"""

from __future__ import annotations

import getpass
import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import typer

sys.path.insert(0, str(Path(__file__).resolve().parent / "spikes"))
from _common import record

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"
AGENTS = ROOT / ".secrets" / "agents"
CONSOLE_ENV = (
    Path.home()
    / "Library/Application Support/switchdash/local-switch-server/native/run/switch-core.env"
)
app = typer.Typer(add_completion=False)
PROMPT = typer.Option(False, "--prompt", help="Type the admin email and password instead")


def read_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def set_env_value(name: str, value: str) -> None:
    lines = ENV.read_text().splitlines() if ENV.exists() else []
    out = [ln for ln in lines if not ln.startswith(f"{name}=")]
    out.append(f"{name}={value}")
    ENV.touch(mode=0o600, exist_ok=True)
    ENV.chmod(0o600)
    ENV.write_text("\n".join(out) + "\n")
    ENV.chmod(0o600)


@contextmanager
def admin_session(prompt: bool) -> Iterator[httpx.Client]:
    base = read_env_file(ENV).get("SWITCH_BASE_URL", "") if ENV.exists() else ""
    if not base:
        raise SystemExit("SWITCH_BASE_URL is not set in .env")
    if prompt:
        email = input("Gateway admin email: ").strip()
        password = getpass.getpass("Gateway admin password (hidden): ")
    else:
        if not CONSOLE_ENV.exists():
            raise SystemExit(f"{CONSOLE_ENV} not found; re-run with --prompt")
        console = read_env_file(CONSOLE_ENV)
        email = console.get("GATEWAY_ADMIN_EMAIL", "")
        password = console.get("GATEWAY_ADMIN_PASSWORD", "")
        if not email or not password:
            raise SystemExit("admin login not in the Console env file; use --prompt")
    with httpx.Client(base_url=base.rstrip("/") + "/gateway", timeout=20) as c:
        r = c.post("/auth/login", json={"email": email, "password": password})
        del password
        if r.status_code != 200:
            raise SystemExit(f"login failed: HTTP {r.status_code} {r.text[:200]}")
        try:
            yield c
        finally:
            c.post("/auth/logout")


def agent_id(name_or_id: str) -> str:
    path = AGENTS / f"{name_or_id}.json"
    return str(json.loads(path.read_text())["id"]) if path.exists() else name_or_id


def call(c: httpx.Client, kind: str, method: str, url: str, body: Any = None) -> httpx.Response:
    r = c.request(method, url, json=body)
    record(f"gateway:{kind}", r, body)
    return r


@app.command()
def key(label: str = typer.Option("leasedesk-registration"), prompt: bool = PROMPT) -> None:
    """Create a registration key (POST /gateway/api-keys) and save it to .env."""
    with admin_session(prompt) as c:
        existing = [k for k in c.get("/api-keys").json() if k.get("label") == label]
        if existing:
            typer.echo(f"note: {len(existing)} key(s) labelled {label!r} already exist; adding one")
        r = c.post("/api-keys", json={"label": label})
        if r.status_code != 200:
            raise SystemExit(f"create failed: HTTP {r.status_code} {r.text[:200]}")
        data = r.json()
        set_env_value("SWITCH_REGISTRATION_KEY", data["key"])
        typer.echo(
            f"created registration key {data.get('label')!r} id={data.get('id')} "
            f"({len(data['key'])} chars); saved to .env (mode 600)"
        )


@app.command()
def addressing(
    agent: str = typer.Option(..., help="Agent name (from .secrets/agents) or id"),
    mode: str = typer.Option("anyone", help="anyone | owner_and_owner_agents | owner_only"),
    prompt: bool = PROMPT,
) -> None:
    """Set who may instruct an agent (PUT /gateway/agents/{id}/addressing-policy)."""
    rule = {"rooms": "*", "room_groups": "*", "users": [], "agents": [], "platform": False}
    policies: dict[str, Any] = {
        "anyone": None,
        "owner_and_owner_agents": {"rules": [{**rule, "owner": True, "owner_agents": True}]},
        "owner_only": {"rules": [{**rule, "owner": True, "owner_agents": False}]},
    }
    if mode not in policies:
        raise typer.BadParameter(f"mode must be one of {sorted(policies)}")
    aid = agent_id(agent)
    with admin_session(prompt) as c:
        r = call(
            c, "addressing", "PUT", f"/agents/{aid}/addressing-policy", {"policy": policies[mode]}
        )
    if r.status_code != 200:
        raise SystemExit(f"failed: HTTP {r.status_code} {r.text[:300]}")
    typer.echo(f"{agent}: addressing_policy now {json.dumps(r.json().get('addressing_policy'))}")


@app.command("library-doc")
def library_doc(
    name: str = typer.Option(...),
    description: str = typer.Option(...),
    instructions: str = typer.Option(...),
    content_file: Path = typer.Option(..., exists=True, readable=True),  # noqa: B008
    room: list[str] = typer.Option([], help="Room id(s) to attach it to"),  # noqa: B008
    visibility: str = typer.Option("public", help="read visibility: private | public"),
    prompt: bool = PROMPT,
) -> None:
    """Write a library document as the admin (POST /gateway/documents) and attach it."""
    body = {
        "name": name,
        "description": description,
        "instructions": instructions,
        "content": content_file.read_text(),
        "read_visibility": visibility,
        "write_visibility": "private",
    }
    with admin_session(prompt) as c:
        r = call(c, "create_document", "POST", "/documents", body)
        if r.status_code != 201:
            raise SystemExit(f"create failed: HTTP {r.status_code} {r.text[:300]}")
        doc_id = r.json()["id"]
        typer.echo(f"library document {name!r} id={doc_id}")
        for room_id in room:
            r = call(c, "attach_document", "POST", f"/rooms/{room_id}/documents/{doc_id}")
            typer.echo(f"  attach to {room_id}: HTTP {r.status_code}")


if __name__ == "__main__":
    app()
