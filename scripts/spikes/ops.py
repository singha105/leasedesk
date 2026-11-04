"""Spike: save GET /agents/{id}/ops to docs/switch-ops.json and summarize the schemas we use.

uv run python scripts/spikes/ops.py fetch      [--name leasedesk-probe]
uv run python scripts/spikes/ops.py summarize  # markdown for docs/PROTOCOL_NOTES.md
"""

from __future__ import annotations

import json
from typing import Any

import typer
from _common import ROOT, client, load_agent, record

app = typer.Typer(add_completion=False)
OPS_FILE = ROOT / "docs" / "switch-ops.json"
WANTED = [
    "post_message",
    "send_targeted_message",
    "read_context",
    "list_participants",
    "connect_to_room",
    "list_rooms",
    "create_room_document",
    "update_room_document",
    "load_internal_documents",
    "assume_role",
    "release_role",
    "list_roles",
    "define_role",
    "link_rooms",
    "invite_agent_to_room",
]


@app.command()
def fetch(name: str = typer.Option("leasedesk-probe")) -> None:
    agent = load_agent(name)
    with client(agent.api_key) as c:
        r = c.get(f"/agents/{agent.id}/ops")
    record("list_ops", r)
    r.raise_for_status()
    data = r.json()
    OPS_FILE.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    ops = data.get("operations", data)
    typer.echo(f"saved {len(ops)} operations to {OPS_FILE.relative_to(ROOT)}")
    typer.echo(", ".join(sorted(ops)))
    missing = [w for w in WANTED if w not in ops]
    typer.echo(f"missing from spec list: {missing or 'none'}")


def _type(s: dict[str, Any]) -> str:
    if "anyOf" in s:
        return " or ".join(_type(x) for x in s["anyOf"])
    t = s.get("type", "any")
    if t == "array":
        return f"array[{_type(s.get('items', {}))}]"
    if "enum" in s:
        return "enum(" + ", ".join(map(str, s["enum"])) + ")"
    return str(t)


@app.command()
def summarize(op: list[str] = typer.Option([], help="Ops to show (default: spec list)")) -> None:  # noqa: B008
    data = json.loads(OPS_FILE.read_text())
    ops = data.get("operations", data)
    for name in op or WANTED:
        spec = ops.get(name)
        if spec is None:
            typer.echo(f"#### `{name}`\nNOT ON THIS SERVER\n")
            continue
        schema = spec.get("input_schema", {})
        required = set(schema.get("required", []))
        desc = (spec.get("description") or "").strip().splitlines()
        typer.echo(f"#### `{name}`\n{desc[0] if desc else ''}\n")
        props = schema.get("properties", {})
        if not props:
            typer.echo("(no arguments)\n")
            continue
        typer.echo("| arg | type | required | default |\n| - | - | - | - |")
        for arg, s in props.items():
            default = json.dumps(s["default"]) if "default" in s else ""
            req = "yes" if arg in required else ""
            typer.echo(f"| `{arg}` | {_type(s)} | {req} | {default} |")
        typer.echo("")


if __name__ == "__main__":
    app()
