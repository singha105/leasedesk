"""A scripted fake of the Switch agent WebSocket, for connection tests.

Each open of the socket runs the next script in ``scripts``. A script gets a ``FakeConn`` and
sends frames in the recorded shapes (docs/PROTOCOL_NOTES.md). Every pong is timestamped.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import parse_qs, urlparse

from websockets.asyncio.server import Server, ServerConnection, serve

ROOM = "room-1"


class FakeConn:
    def __init__(self, ws: ServerConnection, server: FakeSwitch) -> None:
        self.ws = ws
        self.server = server

    async def send(self, event: str, data: dict[str, Any], frame_id: int | None = None) -> None:
        frame: dict[str, Any] = {"event": event, "data": data}
        if frame_id is not None:
            frame["id"] = frame_id
        await self.ws.send(json.dumps(frame))

    async def state(self, generation: int = 10) -> None:
        await self.send(
            "connection_state",
            {
                "connection_id": "server-side",
                "agent_id": "agent-1",
                "generation": generation,
                "scope": "all",
                "filter": "all",
                "rooms": [ROOM],
                "cursor": 0,
                "heartbeat_interval_seconds": 2.0,
            },
        )

    async def ping(self) -> float:
        sent = time.monotonic()
        await self.send("ping", {})
        return sent

    async def message(
        self, seq: int, body: str = "@keymaster hi", *, message_id: str | None = None
    ) -> None:
        await self.send(
            "message",
            {
                "type": "message",
                "room_id": ROOM,
                "bridge_id": "b1",
                "channel_type": "channel_public",
                "payload": {
                    "addressed": True,
                    "sender": "@switch-discord-b1-arnab:localhost",
                    "sender_name": "arnab.",
                    "message_id": message_id or f"sw_{seq}",
                    "body": body,
                    "thread_id": None,
                    "attachments": [],
                },
                "sequence": seq,
            },
            frame_id=seq,
        )

    async def evict(self, code: str) -> None:
        await self.send("evicted", {"code": code, "reason": f"test {code}", "room_id": None})

    async def gap(self, resumed_at: int) -> None:
        await self.send(
            "gap",
            {
                "from_sequence": 1,
                "resumed_at": resumed_at,
                "rooms": [],
                "all_rooms": True,
                "reason": "test",
            },
        )

    async def close(self, code: int) -> None:
        await self.ws.close(code, "test")

    async def wait_pong(self, cursor: int, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if any(c == cursor for _, c in self.server.pongs):
                return
            await asyncio.sleep(0.01)
        raise TimeoutError(f"no pong with cursor {cursor}; got {self.server.pongs}")

    async def hold(self) -> None:
        await self.ws.wait_closed()


Script = Callable[[FakeConn], Awaitable[None]]


class FakeSwitch:
    def __init__(self, scripts: list[Script]) -> None:
        self.scripts = scripts
        self.opens: list[dict[str, str]] = []
        self.pongs: list[tuple[float, int]] = []
        self._server: Server | None = None
        self.port = 0

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def _reader(self, ws: ServerConnection) -> None:
        try:
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("type") == "pong":
                    self.pongs.append((time.monotonic(), int(msg.get("cursor", 0))))
        except Exception:  # noqa: S110 - socket closed under us
            pass

    async def _handler(self, ws: ServerConnection) -> None:
        assert ws.request is not None
        query = parse_qs(urlparse(ws.request.path).query)
        self.opens.append({k: v[0] for k, v in query.items()})
        index = len(self.opens) - 1
        reader = asyncio.create_task(self._reader(ws))
        try:
            if index < len(self.scripts):
                await self.scripts[index](FakeConn(ws, self))
            else:
                await FakeConn(ws, self).evict("taken_over")
            await asyncio.sleep(0.05)
        finally:
            reader.cancel()
            await ws.close()

    async def __aenter__(self) -> FakeSwitch:
        self._server = await serve(self._handler, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1] if self._server.sockets else 0
        sock = next(iter(self._server.sockets))
        self.port = sock.getsockname()[1]
        return self

    async def __aexit__(self, *_: object) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()
