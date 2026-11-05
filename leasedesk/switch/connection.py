"""One WebSocket connection to one room.

Why one per room: ``connect_to_room`` replaces a connection's subscription and sets the room
its ops act on (post_message, list_participants, documents, roles). Two connections for the
same agent coexist, so an agent that serves several rooms opens one ``RoomConnection`` each
(docs/PROTOCOL_NOTES.md, docs/DECISIONS.md).

Heartbeat: the reader task answers every ping the moment it reads it. Domain events go on a
queue that a separate worker task drains through the handler, so a slow handler can never
delay a pong. The pong carries the highest event id the worker has fully processed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
import time
import uuid
from collections.abc import Awaitable, Callable
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from websockets.asyncio.client import ClientConnection
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from leasedesk.common.logging import get_logger
from leasedesk.switch.credentials import AgentCredentials
from leasedesk.switch.errors import SwitchError
from leasedesk.switch.models import (
    ConnectionState,
    ControlFrame,
    DomainEvent,
    Evicted,
    Gap,
    Ping,
    Refused,
    RoomReleased,
    SubscriptionChanged,
    UnknownFrame,
    parse_frame,
)
from leasedesk.switch.ops import OpsClient

log = get_logger(__name__)

SAVE_INTERVAL_S = 2.0
BACKOFF_MIN_S = 0.5
BACKOFF_MAX_S = 10.0
MAX_CLAIM_FAILURES = 3
RESUMABLE_EVICTIONS = frozenset({"heartbeat_lapsed", "no_stream", "unfenced", "connection_error"})


class ExitReason(StrEnum):
    STOPPED = "stopped"
    TAKEN_OVER = "taken_over"
    CLOSED = "closed"
    AUTH_FAILED = "auth_failed"
    REFUSED = "refused"
    ROOM_LOST = "room_lost"


class TakenOver(Exception):
    """Another stream took this connection over. Final: the process exits with code 3."""


def backoff_delay(attempt: int, rand: Callable[[], float] = random.random) -> float:
    """Jittered exponential backoff between 0.5 s and 10 s."""
    base = min(BACKOFF_MAX_S, BACKOFF_MIN_S * float(2 ** min(attempt, 10)))
    return float(max(BACKOFF_MIN_S, base * (0.5 + rand() / 2)))


class CursorStore:
    """``.state/<agent>.<room>.cursor.json``; written at most every 2 s, flushed on exit."""

    def __init__(self, path: Path, clock: Callable[[], float] = time.monotonic) -> None:
        self.path = path
        self._clock = clock
        self._last_write: float | None = None
        self._pending: tuple[str, int | None, int | None] | None = None

    def load(self) -> tuple[str | None, int | None, int | None]:
        try:
            data = json.loads(self.path.read_text())
            return data.get("connection_id"), data.get("cursor"), data.get("generation")
        except (OSError, ValueError, AttributeError):
            return None, None, None

    def update(self, connection_id: str, cursor: int | None, generation: int | None) -> None:
        self._pending = (connection_id, cursor, generation)
        now = self._clock()
        if self._last_write is None or now - self._last_write >= SAVE_INTERVAL_S:
            self.flush()

    def flush(self) -> None:
        if self._pending is None:
            return
        connection_id, cursor, generation = self._pending
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.touch(mode=0o600, exist_ok=True)
        tmp.chmod(0o600)
        tmp.write_text(
            json.dumps({"connection_id": connection_id, "cursor": cursor, "generation": generation})
        )
        tmp.replace(self.path)
        self._last_write = self._clock()
        self._pending = None


EventHandler = Callable[[DomainEvent], Awaitable[None]]
ControlHandler = Callable[[ControlFrame], Awaitable[None]]


class RoomConnection:
    def __init__(
        self,
        creds: AgentCredentials,
        room_id: str,
        ops: OpsClient,
        *,
        base_url: str,
        state_dir: Path,
        on_event: EventHandler,
        on_control: ControlHandler | None = None,
        connect: Callable[..., Any] = ws_connect,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rand: Callable[[], float] = random.random,
    ) -> None:
        self.creds = creds
        self.room_id = room_id
        self.ops = ops
        self._ws_base = (
            base_url.rstrip("/").replace("http://", "ws://").replace("https://", "wss://")
        )
        self._on_event = on_event
        self._on_control = on_control
        self._connect = connect
        self._sleep = sleep
        self._rand = rand
        self.store = CursorStore(state_dir / f"{creds.name}.{room_id}.cursor.json")
        saved_id, self.cursor, self.generation = self.store.load()
        self.connection_id: str = saved_id or str(uuid.uuid4())
        self.ready = asyncio.Event()
        self._stopping = asyncio.Event()
        self._queue: asyncio.Queue[DomainEvent] = asyncio.Queue()
        self._ws: ClientConnection | None = None
        self._after_gap = False
        self._claim_failures = 0
        self._tasks: set[asyncio.Task[None]] = set()
        self.log = log.bind(agent=creds.name, room=room_id)

    # Public ---------------------------------------------------------------------------

    async def run(self) -> ExitReason:
        worker = asyncio.create_task(self._worker())
        attempt = 0
        try:
            while not self._stopping.is_set():
                reason, healthy = await self._session()
                if reason is not None:
                    return reason
                if self._stopping.is_set():
                    break
                attempt = 0 if healthy else attempt + 1
                delay = backoff_delay(attempt, self._rand)
                self.log.info("reconnecting", delay_s=round(delay, 2))
                await self._sleep(delay)
            return ExitReason.STOPPED
        finally:
            self.ready.clear()
            worker.cancel()
            for t in list(self._tasks):
                t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
            self._save(force=True)

    async def stop(self) -> None:
        self._stopping.set()
        if self._ws is not None:
            await self._ws.close()

    # One socket lifetime ----------------------------------------------------------------

    def _url(self) -> str:
        params: dict[str, str] = {
            "connection_id": self.connection_id,
            "scope": "all",
            "filter": "all",
            "rooms": self.room_id,
        }
        if self.cursor is not None:
            params["start_from"] = str(self.cursor)
        if self.generation is not None:
            params["expected_generation"] = str(self.generation)
        return f"{self._ws_base}/agents/{self.creds.id}/connection/ws?{urlencode(params)}"

    async def _session(self) -> tuple[ExitReason | None, bool]:
        """Run one socket. Returns (final reason or None to reconnect, whether it got going)."""
        headers = {"Authorization": f"Bearer {self.creds.api_key.get_secret_value()}"}
        healthy = False
        try:
            async with self._connect(
                self._url(), additional_headers=headers, ping_interval=None, max_size=2**22
            ) as ws:
                self._ws = ws
                async for raw in ws:
                    frame = parse_frame(raw)
                    if isinstance(frame, ConnectionState):
                        healthy = True
                    outcome = await self._handle(ws, frame)
                    if outcome is not None:
                        return outcome, healthy
            return self._close_reason(ws.close_code), healthy
        except InvalidStatus as exc:
            status = exc.response.status_code
            self.log.error("ws_handshake_refused", status=status)
            return (ExitReason.AUTH_FAILED if status in (401, 403) else None), healthy
        except ConnectionClosed as exc:
            code = exc.rcvd.code if exc.rcvd else None
            return self._close_reason(code), healthy
        except OSError as exc:
            self.log.warning("ws_connect_failed", error=type(exc).__name__)
            return None, healthy
        finally:
            self._ws = None
            self.ready.clear()

    def _close_reason(self, code: int | None) -> ExitReason | None:
        if self._stopping.is_set():
            return ExitReason.STOPPED
        if self._claim_failures >= MAX_CLAIM_FAILURES:
            self.log.error("room_lost", claim_failures=self._claim_failures)
            return ExitReason.ROOM_LOST
        if code == 4401:
            self.log.error("ws_closed_bad_auth", code=code)
            return ExitReason.AUTH_FAILED
        if code == 4409:
            self.log.error("ws_closed_refused_or_taken_over", code=code)
            return ExitReason.REFUSED
        self.log.info("ws_closed", code=code)
        return None

    async def _handle(self, ws: ClientConnection, frame: Any) -> ExitReason | None:
        if isinstance(frame, Ping):
            await ws.send(json.dumps({"type": "pong", "cursor": self.cursor or 0}))
            return None
        if isinstance(frame, DomainEvent):
            if self._after_gap:
                frame.after_gap = True
                self._after_gap = False
            self._queue.put_nowait(frame)
            return None
        if isinstance(frame, ConnectionState):
            self.generation = frame.generation
            self.log.info("connected", generation=frame.generation)
            self._save(force=True)
            self._spawn(self._claim())
        elif isinstance(frame, Gap):
            self.log.warning("gap", resumed_at=frame.resumed_at, reason=frame.reason[:120])
            self.cursor = frame.resumed_at
            self._after_gap = True
            self._save(force=True)
        elif isinstance(frame, Evicted):
            return self._evicted(frame)
        elif isinstance(frame, Refused):
            self.log.error("refused", code=frame.code, reason=frame.reason)
            return ExitReason.REFUSED
        elif isinstance(frame, RoomReleased):
            self.log.warning("room_released", released=frame.room_id, reason=frame.reason)
            if frame.room_id in (None, self.room_id):
                return ExitReason.ROOM_LOST
        elif isinstance(frame, SubscriptionChanged):
            self.log.info("subscription_changed", rooms=frame.rooms)
        elif isinstance(frame, UnknownFrame):
            self.log.warning("unknown_frame", kind=frame.event)
            return None
        if self._on_control is not None and not isinstance(frame, UnknownFrame):
            self._spawn(self._on_control(frame))
        return None

    def _evicted(self, frame: Evicted) -> ExitReason | None:
        if frame.code == "taken_over":
            self.log.error("evicted_taken_over", reason=frame.reason)
            return ExitReason.TAKEN_OVER
        if frame.code == "closed":
            self.log.error("evicted_closed", reason=frame.reason)
            return ExitReason.CLOSED
        if frame.code not in RESUMABLE_EVICTIONS:
            self.log.warning("evicted_unknown_code", code=frame.code, reason=frame.reason)
        else:
            self.log.info("evicted_resuming", code=frame.code)
        return None

    async def _claim(self) -> None:
        try:
            await self.ops.connect_to_room(self.room_id, connection_id=self.connection_id)
        except SwitchError as exc:
            self._claim_failures += 1
            self.log.error("claim_failed", status=exc.status, detail=exc.detail)
            if self._ws is not None:
                await self._ws.close()
            return
        self._claim_failures = 0
        self.ready.set()

    def _spawn(self, coro: Awaitable[None]) -> None:
        task: asyncio.Task[None] = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # Worker -------------------------------------------------------------------------------

    async def _worker(self) -> None:
        while True:
            ev = await self._queue.get()
            if self.cursor is not None and ev.id <= self.cursor:
                self.log.debug("duplicate_event_dropped", id=ev.id)
                continue
            try:
                await self._on_event(ev)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.log.exception("handler_failed", event_id=ev.id, type=ev.type)
            self.cursor = ev.id
            self._save()

    def _save(self, *, force: bool = False) -> None:
        self.store.update(self.connection_id, self.cursor, self.generation)
        if force:
            self.store.flush()
