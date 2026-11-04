"""Spike: post messages and call any operation, recording request and response shapes.

    uv run python scripts/spikes/post.py message --room <room_id> --text "hello"
    uv run python scripts/spikes/post.py op list_rooms
    uv run python scripts/spikes/post.py op post_message --args '{"content": "hi"}'
    uv run python scripts/spikes/post.py op create_room_document --args-file /tmp/args.json

`op` binds to the listener's connection (X-Switch-Connection-Id from .state/spike-<name>.json)
unless --no-bind. Every exchange is appended, redacted, to logs/http.jsonl.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer
from _common import client, load_agent, load_state, record, show

app = typer.Typer(add_completion=False)


def call_op(name: str, op: str, args: dict[str, Any], bind: bool) -> Any:
    agent = load_agent(name)
    headers = {}
    if bind:
        conn = load_state(name).get("connection_id")
        if not conn:
            raise SystemExit("no saved connection_id; run listen.py first or pass --no-bind")
        headers["X-Switch-Connection-Id"] = conn
    with client(agent.api_key) as c:
        r = c.post(f"/agents/{agent.id}/ops/{op}", json=args, headers=headers)
    record(f"op:{op}", r, args)
    typer.echo(f"{op} (bound={bind}): {show(r)}")
    return r


@app.command()
def message(
    room: str = typer.Option(..., help="room_id"),
    text: str = typer.Option(...),
    name: str = typer.Option("leasedesk-probe"),
) -> None:
    """POST /agents/{id}/message {room_id, content}: needs no connection."""
    agent = load_agent(name)
    body = {"room_id": room, "content": text}
    with client(agent.api_key) as c:
        r = c.post(f"/agents/{agent.id}/message", json=body)
    record("message", r, body)
    typer.echo(f"message: {show(r)}")


@app.command()
def media(
    room: str = typer.Option(..., help="room_id"),
    mxc: str = typer.Option(..., help="the attachment's mxc (switch-media://...)"),
    out: Path = typer.Option(Path("logs/media.bin"), help="where to save the bytes"),  # noqa: B008
    name: str = typer.Option("leasedesk-probe"),
) -> None:
    """GET /agents/{id}/rooms/{room_id}/media?mxc=...: an attachment's bytes."""
    agent = load_agent(name)
    with client(agent.api_key) as c:
        r = c.get(f"/agents/{agent.id}/rooms/{room}/media", params={"mxc": mxc})
    record("media", r)
    if r.status_code == 200:
        out.write_bytes(r.content)
        typer.echo(
            f"media: 200 {r.headers.get('content-type')} {len(r.content)} bytes, "
            f"disposition={r.headers.get('content-disposition')!r} -> {out}"
        )
    else:
        typer.echo(f"media: {show(r)}")


@app.command()
def op(
    operation: str = typer.Argument(...),
    args: str = typer.Option("{}", help="JSON arguments object (unwrapped)"),
    args_file: Path | None = typer.Option(None, help="Read the arguments JSON from a file"),  # noqa: B008
    bind: bool = typer.Option(True, help="Send X-Switch-Connection-Id from the listener state"),
    name: str = typer.Option("leasedesk-probe"),
) -> None:
    payload = json.loads(args_file.read_text() if args_file else args)
    call_op(name, operation, payload, bind)


if __name__ == "__main__":
    app()
