"""Act as the human admin on the Switch Gateway API (run this yourself, not an agent).

Why: Switch Console 0.40 runs the local server natively, and a native local server has no
Gateway web page (Console hides "Full admin interface"). These are the person-side actions the
spec puts "in the Gateway", done through the Gateway API instead:

    uv run python scripts/gateway.py key                       # registration key -> .env
    uv run python scripts/gateway.py addressing --agent leasedesk-probe --mode anyone
    uv run python scripts/gateway.py library-doc --room <room_id> --name human-note \
        --description "..." --instructions "..." --content-file note.md
    uv run python scripts/gateway.py reference                 # GitHub reference to the repo
    uv run python scripts/gateway.py package --doc <id> --doc <id> --ref <id>
    uv run python scripts/gateway.py package-attach --room <room_id> --room <room_id>
    uv run python scripts/gateway.py template-validate switch/leasedesk.template.yaml
    uv run python scripts/gateway.py template-run switch/leasedesk.template.yaml  # defaults

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

from leasedesk.common import envfile
from leasedesk.common.envfile import read_env_file

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"
AGENTS = ROOT / ".secrets" / "agents"
CONSOLE_ENV = (
    Path.home()
    / "Library/Application Support/switchdash/local-switch-server/native/run/switch-core.env"
)
app = typer.Typer(add_completion=False)
PROMPT = typer.Option(False, "--prompt", help="Type the admin email and password instead")


def set_env_value(name: str, value: str) -> None:
    envfile.set_env_value(ENV, name, value)


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


@app.command("library-doc-update")
def library_doc_update(
    doc_id: str = typer.Option(...),
    content_file: Path = typer.Option(..., exists=True, readable=True),  # noqa: B008
    prompt: bool = PROMPT,
) -> None:
    """Replace a library document's content as the admin (PATCH /gateway/documents/{id})."""
    with admin_session(prompt) as c:
        body = {"content": content_file.read_text()}
        r = call(c, "update_document", "PATCH", f"/documents/{doc_id}", body)
        detail = "" if r.status_code < 300 else f" {r.text[:200]}"
        typer.echo(f"update {doc_id}: HTTP {r.status_code}{detail}")


# Part 4: the governance package, the repo reference and template checks ----------------------


def validation_report(body: dict[str, Any]) -> tuple[str, int]:
    """POST /templates/validate's answer as lines, and the exit code (1 on any finding)."""
    errors, warnings = body.get("errors") or [], body.get("warnings") or []
    head = "blocked" if body.get("blocked") else ("ok" if body.get("ok") else "not ok")
    lines = [f"{head}: {len(errors)} errors, {len(warnings)} warnings"]
    for kind, items in (("error", errors), ("warning", warnings)):
        lines += [f"{kind} {f.get('code')}: {f.get('message')}" for f in items]
    clean = head == "ok" and not errors and not warnings
    return "\n".join(lines), 0 if clean else 1


def run_report(body: dict[str, Any]) -> tuple[str, int]:
    """POST /rooms/from-template-document's answer as lines, and the exit code (1 on any
    refusal, failed attachment or group error)."""
    if "code" in body and "created_agents" not in body:
        lines = [f"refused: {body.get('code')} ({body.get('detail')})"]
        for p in body.get("problems") or []:
            lines.append(f"{p.get('slot')} {p.get('code')}: {p.get('message')}")
        if body.get("left_behind"):
            lines.append("left behind: " + ", ".join(map(str, body["left_behind"])))
        return "\n".join(lines), 1
    lines, failed = [], False
    for a in body.get("created_agents") or []:
        heard = "listening" if a.get("listening") else "NOT listening (the kickoff may miss it)"
        machine = a.get("machine")
        if isinstance(machine, dict):  # the live server answers {id, name}
            machine = machine.get("name") or machine.get("id")
        lines.append(
            f"agent {a.get('name')} ({a.get('slot')}): id={a.get('agent_id')} "
            f"machine={machine} {heard}"
        )
    room = body.get("room") or {}
    rooms = room.get("rooms") if "rooms" in room else ([room] if room else [])
    if "group_id" in room:
        lines.append(f"group {room.get('group_name')}: id={room.get('group_id')}")
    for r in rooms:
        roles = ",".join(r.get("role_names") or [])
        docs = len(r.get("created_document_ids") or [])
        lines.append(f"room {r.get('room_name')}: id={r.get('room_id')} roles={roles} docs={docs}")
        for f in r.get("failed_attachments") or []:
            failed = True
            lines.append(f"room {r.get('room_name')} failed attachment: {f}")
    for e in room.get("errors") or []:
        failed = True
        lines.append(f"group error: {e}")
    return "\n".join(lines), 1 if failed else 0


def members_to_add(existing: set[str], wanted: list[str]) -> list[str]:
    out: list[str] = []
    for member in wanted:
        if member not in existing and member not in out:
            out.append(member)
    return out


def _package_id(c: httpx.Client, name: str) -> str | None:
    found = [p for p in c.get("/packages").json() if p.get("name") == name]
    if len(found) > 1:
        raise SystemExit(f"{len(found)} packages are named {name!r}; keep one")
    return str(found[0]["id"]) if found else None


@app.command()
def reference(
    name: str = typer.Option("leasedesk-repo"),
    url: str = typer.Option("https://github.com/singha105/leasedesk"),
    description: str = typer.Option("The LeaseDesk repository: design, code and runbooks"),
    instructions: str = typer.Option(
        "How LeaseDesk works. Read docs/SPEC.md before explaining the process."
    ),
    prompt: bool = PROMPT,
) -> None:
    """Create the GitHub reference to the repo (POST /gateway/references), once."""
    with admin_session(prompt) as c:
        found = [r for r in c.get("/references").json() if r.get("name") == name]
        if found:
            typer.echo(f"reference {name!r} exists: id={found[0]['id']}")
            return
        body = {
            "type": "github",
            "name": name,
            "description": description,
            "instructions": instructions,
            "value": {"urls": [url]},
            "read_visibility": "public",
            "write_visibility": "private",
        }
        r = call(c, "create_reference", "POST", "/references", body)
        if r.status_code != 201:
            raise SystemExit(f"create failed: HTTP {r.status_code} {r.text[:300]}")
        typer.echo(f"reference {name!r} id={r.json()['id']}")


@app.command()
def package(
    name: str = typer.Option("leasedesk-governance"),
    description: str = typer.Option(
        "LeaseDesk rules: the access-policy, the resource-catalog and the repo"
    ),
    instructions: str = typer.Option(
        "People write these documents; agents read them and never edit them. keymaster "
        "enforces access-policy; scribe and sentinel use resource-catalog."
    ),
    doc: list[str] = typer.Option([], help="Library document id(s) to include"),  # noqa: B008
    ref: list[str] = typer.Option([], help="Reference id(s) to include"),  # noqa: B008
    prompt: bool = PROMPT,
) -> None:
    """Create the package (or reuse the one with this name) and add documents and references."""
    with admin_session(prompt) as c:
        pkg_id = _package_id(c, name)
        if pkg_id is None:
            body = {
                "name": name,
                "description": description,
                "instructions": instructions,
                "read_visibility": "public",
                "write_visibility": "private",
            }
            r = call(c, "create_package", "POST", "/packages", body)
            if r.status_code != 201:
                raise SystemExit(f"create failed: HTTP {r.status_code} {r.text[:300]}")
            pkg_id = str(r.json()["id"])
            typer.echo(f"package {name!r} created id={pkg_id}")
        else:
            typer.echo(f"package {name!r} exists id={pkg_id}")
        for kind, wanted in (("documents", doc), ("references", ref)):
            have = {str(m["id"]) for m in c.get(f"/packages/{pkg_id}/{kind}").json()}
            for member in members_to_add(have, wanted):
                r = call(c, f"add_{kind}", "POST", f"/packages/{pkg_id}/{kind}/{member}")
                typer.echo(f"  add {kind[:-1]} {member}: HTTP {r.status_code}")


@app.command("package-attach")
def package_attach(
    name: str = typer.Option("leasedesk-governance"),
    room: list[str] = typer.Option(..., help="Room id(s)"),  # noqa: B008
    prompt: bool = PROMPT,
) -> None:
    """Attach the package to rooms (POST /gateway/rooms/{room}/packages/{package})."""
    with admin_session(prompt) as c:
        pkg_id = _package_id(c, name)
        if pkg_id is None:
            raise SystemExit(f"no package named {name!r}; run: gateway.py package")
        for room_id in room:
            attached = {str(p["id"]) for p in c.get(f"/rooms/{room_id}/packages").json()}
            if pkg_id in attached:
                typer.echo(f"  {room_id}: already attached")
                continue
            r = call(c, "attach_package", "POST", f"/rooms/{room_id}/packages/{pkg_id}")
            typer.echo(f"  attach to {room_id}: HTTP {r.status_code}")


@app.command("template-validate")
def template_validate(
    path: Path = typer.Argument(..., exists=True, readable=True),  # noqa: B008
    prompt: bool = PROMPT,
) -> None:
    """Check a template without storing it (POST /gateway/templates/validate)."""
    with admin_session(prompt) as c:
        r = c.post("/templates/validate", json={"content": path.read_text()})
    if r.status_code != 200:
        raise SystemExit(f"validate failed: HTTP {r.status_code} {r.text[:300]}")
    text, code = validation_report(r.json())
    typer.echo(text)
    raise typer.Exit(code)


@app.command("template-run")
def template_run(
    path: Path = typer.Argument(..., exists=True, readable=True),  # noqa: B008
    name: str = typer.Option("LeaseDesk", "--name", help="What the run is listed under"),
    prompt: bool = PROMPT,
) -> None:
    """Run a template with its defaults as the admin person (POST /rooms/from-template-document).

    The server creates the agents on your machines, then the rooms, or nothing. Console's
    Use form does the same but also wants every provider signed in (docs/DECISIONS.md).
    """
    body = {"template": path.read_text(), "template_name": name, "inputs": {}}
    with admin_session(prompt) as c:
        r = c.post("/rooms/from-template-document", json=body, timeout=180)
        record("gateway:template_run", r, {"template_name": name, "inputs": {}})
    if r.status_code not in (201, 409):
        raise SystemExit(f"run failed: HTTP {r.status_code} {r.text[:500]}")
    text, code = run_report(r.json())
    typer.echo(text)
    raise typer.Exit(code)


if __name__ == "__main__":
    app()
