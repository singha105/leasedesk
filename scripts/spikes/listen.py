"""Spike: hold the agent WebSocket open and log every frame to logs/frames.jsonl.

    uv run python scripts/spikes/listen.py [--name leasedesk-probe] [--rooms r1,r2]
                                          [--duration 600] [--fresh]
    # provoke control frames for the notes:
    uv run python scripts/spikes/listen.py --lapse-after 10      # stop ponging -> heartbeat_lapsed
    uv run python scripts/spikes/listen.py --takeover-after 10   # 2nd socket, same id -> taken_over
    uv run python scripts/spikes/listen.py --start-from 1        # replay / gap behaviour

Pings are answered before anything else is done with a frame. taken_over is final (exit);
heartbeat_lapsed and the pong-refusal codes reconnect with start_from=<cursor> and
expected_generation; a gap moves the cursor to resumed_at. Connection id, generation and cursor
persist in .state/spike-<name>.json so a restart resumes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from typing import Any
from urllib.parse import urlencode

import typer
from _common import LOGS, AgentCreds, base_url, load_agent, load_state, save_state
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

app = typer.Typer(add_completion=False)
RECONNECT_CODES = {"heartbeat_lapsed", "no_stream", "unfenced", "connection_error"}
FATAL_CLOSE = {4400, 4401, 4403, 4409}


def kind_of(frame: dict[str, Any]) -> str:
    data = frame.get("data") if isinstance(frame.get("data"), dict) else {}
    return str(frame.get("event") or frame.get("type") or data.get("type") or "?")


def field(frame: dict[str, Any], key: str) -> Any:
    data = frame.get("data") if isinstance(frame.get("data"), dict) else {}
    return data.get(key, frame.get(key))


class Listener:
    def __init__(self, agent: AgentCreds, opts: dict[str, Any]) -> None:
        self.agent = agent
        self.opts = opts
        self.state = {} if opts["fresh"] else load_state(agent.name)
        self.state.setdefault("connection_id", str(uuid.uuid4()))
        self.pings = 0
        self.mute_until = 0.0
        self.started = time.monotonic()
        self.lapse_done = False
        self.takeover_done = False
        LOGS.mkdir(exist_ok=True)
        self.log = (LOGS / "frames.jsonl").open("a")

    def url(self, resume: bool) -> str:
        params: dict[str, Any] = {
            "connection_id": self.state["connection_id"],
            "scope": self.opts["scope"],
            "filter": self.opts["filter"],
        }
        if self.opts["rooms"]:
            params["rooms"] = self.opts["rooms"]
        if self.opts["start_from"] is not None:
            params["start_from"] = self.opts["start_from"]
            self.opts["start_from"] = None  # only for the first open
        elif resume and isinstance(self.state.get("cursor"), int):
            params["start_from"] = self.state["cursor"]
        if resume and self.state.get("generation") is not None:
            params["expected_generation"] = self.state["generation"]
        ws = base_url().replace("http://", "ws://").replace("https://", "wss://")
        return f"{ws}/agents/{self.agent.id}/connection/ws?{urlencode(params)}"

    def write(self, socket: str, raw: str | bytes, frame: Any) -> None:
        entry = {"received_at": time.time(), "socket": socket, "frame": frame}
        if frame is None:
            entry["raw"] = raw if isinstance(raw, str) else raw.decode(errors="replace")
        self.log.write(json.dumps(entry) + "\n")
        self.log.flush()

    def note(self, msg: str) -> None:
        typer.echo(f"{time.strftime('%H:%M:%S')} {msg}")

    async def pong(self, ws: ClientConnection) -> None:
        cursor = self.state.get("cursor")
        await ws.send(
            json.dumps({"type": "pong", "cursor": cursor if isinstance(cursor, int) else 0})
        )

    async def side_takeover(self) -> None:
        """Open a second socket with the same connection_id: the first should get taken_over."""
        url = self.url(resume=False).split("&expected_generation")[0]
        headers = {"Authorization": f"Bearer {self.agent.api_key}"}
        async with connect(url, additional_headers=headers, ping_interval=None) as ws2:
            end = time.monotonic() + 4
            while time.monotonic() < end:
                try:
                    raw = await asyncio.wait_for(ws2.recv(), timeout=1)
                except TimeoutError:
                    continue
                frame = json.loads(raw)
                if kind_of(frame) == "ping":
                    await self.pong(ws2)
                self.write("takeover-socket", raw, frame)
                if kind_of(frame) != "ping":
                    self.note(f"[takeover-socket] {kind_of(frame)} {json.dumps(frame)[:200]}")

    async def run_once(self, resume: bool) -> str:
        headers = {"Authorization": f"Bearer {self.agent.api_key}"}
        url = self.url(resume)
        self.note("open " + url.split("?")[1].replace(self.state["connection_id"], "<conn>"))
        async with connect(
            url, additional_headers=headers, ping_interval=None, max_size=None
        ) as ws:
            async for raw in ws:
                try:
                    frame = json.loads(raw)
                except ValueError:
                    self.write("main", raw, None)
                    self.note(f"non-JSON frame: {raw!r:.120}")
                    continue
                kind = kind_of(frame)
                if kind == "ping":  # answer first, think later
                    if time.monotonic() >= self.mute_until:
                        await self.pong(ws)
                    self.pings += 1
                self.write("main", raw, frame)
                action = await self.handle(ws, kind, frame)
                if action:
                    return action
        return "reconnect"

    async def handle(self, ws: ClientConnection, kind: str, frame: dict[str, Any]) -> str | None:
        elapsed = time.monotonic() - self.started
        if kind == "ping":
            if self.pings % 30 == 0:
                self.note(f"{self.pings} pings answered")
            lapse = self.opts["lapse_after"]
            if lapse and not self.lapse_done and elapsed > lapse:
                self.lapse_done = True
                self.mute_until = time.monotonic() + 9
                self.note("muting pongs for 9 s to provoke heartbeat_lapsed")
            takeover = self.opts["takeover_after"]
            if takeover and not self.takeover_done and elapsed > takeover:
                self.takeover_done = True
                self.note("opening a second socket with the same connection_id")
                asyncio.create_task(self.side_takeover())  # noqa: RUF006
            return None

        if isinstance(frame.get("id"), int):
            self.state["cursor"] = max(frame["id"], self.state.get("cursor") or 0)
            save_state(self.agent.name, self.state)

        if kind == "connection_state":
            self.state["generation"] = field(frame, "generation")
            cursor = field(frame, "cursor")
            if isinstance(cursor, int):
                self.state["cursor"] = cursor
            save_state(self.agent.name, self.state)
            self.note(
                f"connection_state generation={self.state['generation']} cursor={cursor} "
                f"rooms={field(frame, 'rooms')} scope={field(frame, 'scope')}"
            )
        elif kind == "message":
            p = field(frame, "payload") or {}
            self.note(
                f"message #{frame.get('id')} room={field(frame, 'room_id')} "
                f"addressed={p.get('addressed')} from={p.get('sender_name')} "
                f"thread={p.get('thread_id')} files={len(p.get('attachments') or [])} "
                f"body={str(p.get('body'))[:80]!r}"
            )
        elif kind == "gap":
            resumed_at = field(frame, "resumed_at")
            self.note(f"gap: {json.dumps(frame)[:300]}")
            if isinstance(resumed_at, int):
                self.state["cursor"] = resumed_at
                save_state(self.agent.name, self.state)
        elif kind == "evicted":
            code, reason = field(frame, "code"), field(frame, "reason")
            self.note(f"evicted code={code} reason={reason}")
            if code == "taken_over":
                return "stop"
            if code in RECONNECT_CODES:
                return "reconnect"
            return "stop"
        elif kind == "refused":
            self.note(f"refused status={field(frame, 'status')} detail={field(frame, 'detail')}")
            return "stop"
        else:
            self.note(f"{kind}: {json.dumps(frame)[:300]}")
        return None

    async def run(self) -> None:
        resume, backoff = False, 1.0
        while True:
            try:
                action = await self.run_once(resume)
            except ConnectionClosed as exc:
                code = exc.rcvd.code if exc.rcvd else None
                reason = exc.rcvd.reason if exc.rcvd else ""
                self.note(f"socket closed code={code} reason={reason!r}")
                action = "stop" if code in FATAL_CLOSE else "reconnect"
            except InvalidStatus as exc:
                self.note(f"handshake refused: HTTP {exc.response.status_code}")
                action = "stop"
            except OSError as exc:
                self.note(f"connect failed: {exc}")
                action = "reconnect"
            if action == "stop":
                self.note("stopping (final)")
                return
            resume = True
            self.note(f"reconnecting in {backoff:.0f}s from cursor {self.state.get('cursor')}")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 15)


@app.command()
def main(
    name: str = typer.Option("leasedesk-probe"),
    rooms: str = typer.Option("", help="Comma-separated room ids to claim at open"),
    scope: str = typer.Option("all"),
    filter_: str = typer.Option("all", "--filter"),
    duration: int = typer.Option(0, help="Stop after N seconds (0 = run until Ctrl-C)"),
    fresh: bool = typer.Option(False, help="New connection_id, ignore saved cursor"),
    start_from: str | None = typer.Option(None, help="Override start_from on the first open"),
    lapse_after: int = typer.Option(0, help="Mute pongs once, N s after start"),
    takeover_after: int = typer.Option(0, help="Open a duplicate socket N s after start"),
) -> None:
    opts = {
        "rooms": rooms,
        "scope": scope,
        "filter": filter_,
        "fresh": fresh,
        "start_from": start_from,
        "lapse_after": lapse_after,
        "takeover_after": takeover_after,
    }
    listener = Listener(load_agent(name), opts)

    async def go() -> None:
        if duration:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(listener.run(), timeout=duration)
        else:
            await listener.run()

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(go())
    save_state(name, listener.state)
    typer.echo(f"frames in logs/frames.jsonl; state in .state/spike-{name}.json")


if __name__ == "__main__":
    app()
