"""SwitchAgent: the base every LeaseDesk agent builds on.

It keeps one RoomConnection per room it is assigned to (found by polling ``list_rooms``,
because an invitation sends no frame), learns its own sender id from its room_join in
``read_context``, ignores its own and duplicate messages, and hands addressed messages to the
handler registered for the first word after its mention.

An agent with a ``role`` claims it in every room: when the room is ready, then every
``role_retry_s`` until it sticks, and again on each room_join and subscription_changed (a room
gets its roles from a person, possibly after the agent joined). Without the role the agent
still works; targeted delivery falls back through ``deliver``.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from leasedesk.common.format import split_for_platform
from leasedesk.common.logging import get_logger
from leasedesk.config import Settings
from leasedesk.switch.connection import ExitReason, RoomConnection
from leasedesk.switch.credentials import AgentCredentials
from leasedesk.switch.errors import SwitchError
from leasedesk.switch.models import (
    ControlFrame,
    DomainEvent,
    MessagePayload,
    Participant,
    SubscriptionChanged,
)
from leasedesk.switch.ops import OpsClient

log = get_logger(__name__)

DEDUPE_SIZE = 2000
READY_TIMEOUT_S = 10.0
ROLE_RETRY_S = 60.0
# LeaseDesk's own deterministic agents: they read every message, so nobody addresses them back.
DESK_AGENTS = frozenset({"keymaster", "sentinel"})
ROLE_MAX_TRIES = 10_000  # about a week of minutes; a room without roles is fine
FINAL_FOR_AGENT = frozenset({ExitReason.TAKEN_OVER, ExitReason.AUTH_FAILED})


@dataclass
class CommandContext:
    room_id: str
    event: DomainEvent
    msg: MessagePayload
    word: str
    args: str
    thread_root: str
    room_name: str = ""


Handler = Callable[[CommandContext], Awaitable[None]]
_TRIM = ".,:;!?"


def parse_command(body: str, name: str) -> tuple[str, str]:
    """The command word is the first word after ``@name``; the rest is its argument text.

    With no mention (or nothing after it) the first word of the body is the command.
    """
    text = body.strip()
    mention = rf"@{re.escape(name)}\b[{re.escape(_TRIM)}]?"
    m = re.search(mention, text, re.IGNORECASE)
    if m is not None:
        after = text[m.end() :].strip()
        # Switch prefixes "@name " to a targeted message, so it can arrive twice.
        while (again := re.match(mention, after, re.IGNORECASE)) is not None:
            after = after[again.end() :].strip()
        text = after or text[: m.start()].strip()
    parts = text.split(None, 1)
    if not parts:
        return "", ""
    word = parts[0].strip(_TRIM).lower()
    return word, parts[1].strip() if len(parts) > 1 else ""


class SwitchAgent:
    def __init__(
        self,
        settings: Settings,
        creds: AgentCredentials,
        ops: OpsClient,
        *,
        connection_factory: Callable[..., Any] = RoomConnection,
        own_id: str | None = None,
        room_poll_s: float = 30.0,
    ) -> None:
        self.settings = settings
        self.creds = creds
        self.name = creds.name
        self.ops = ops
        self.own_id = own_id
        self.handlers: dict[str, Handler] = {}
        self.sessions: dict[str, Any] = {}
        self.room_names: dict[str, str] = {}
        self.participants: dict[str, list[Participant]] = {}
        self.known_agent_ids: set[str] = set()
        self.agent_names: set[str] = {creds.name}
        self._factory = connection_factory
        self._room_poll_s = room_poll_s
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._closed_rooms: set[str] = set()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._background: set[asyncio.Task[None]] = set()
        self._exit: asyncio.Future[ExitReason] | None = None
        self.role: str | None = None
        self.role_retry_s = ROLE_RETRY_S
        self._role_conns: set[str] = set()  # connection ids holding self.role
        self._claiming: set[str] = set()
        self.log = log.bind(agent=self.name)
        if own_id:
            self.known_agent_ids.add(own_id)

    # Hooks for subclasses ---------------------------------------------------------------

    async def on_unknown(self, ctx: CommandContext) -> None:
        """An addressed message whose first word has no handler."""

    async def on_unaddressed(self, room_id: str, ev: DomainEvent, msg: MessagePayload) -> None:
        """A message not addressed to this agent. Default: ignore."""

    async def on_room_ready(self, room_id: str) -> None:
        """Called once a room's connection has claimed the room."""

    async def on_control(self, room_id: str, frame: ControlFrame) -> None:
        """Control frames (subscription_changed, gap, ...) after the connection handled them."""

    # Identity -----------------------------------------------------------------------------

    def is_agent(self, sender: str, sender_name: str | None = None) -> bool:
        return sender in self.known_agent_ids or (
            sender_name is not None and sender_name in self.agent_names
        )

    async def refresh_agents(self, room_id: str) -> None:
        """Re-read the room's participants and remember which are agents. An invitation sends
        no frame, so an agent added after this one connected is unknown until this runs."""
        session = self.sessions.get(room_id)
        if session is None:
            return
        try:
            people = await self.ops.list_participants(connection_id=session.connection_id)
        except SwitchError as exc:
            self.log.warning("list_participants_failed", room=room_id, detail=exc.detail)
            return
        self.participants[room_id] = people
        self.agent_names |= {p.name for p in people if p.type == "agent"}

    async def learn_room(self, room_id: str) -> None:
        """Refresh participants (agent names) and learn agent sender ids from history."""
        await self.refresh_agents(room_id)
        try:
            threads = await self.ops.read_context(room_id, limit=100)
        except SwitchError as exc:
            self.log.warning("read_context_failed", room=room_id, detail=exc.detail)
            return
        for thread in threads:
            for entry in [thread.root, *thread.replies]:
                if not entry.sender or not entry.sender_name:
                    continue
                if entry.sender_name == self.name and entry.kind == "room_join":
                    if self.own_id is None:
                        self.own_id = entry.sender
                        self.log.info("own_id_learned", own_id=entry.sender)
                    self.known_agent_ids.add(entry.sender)
                elif entry.sender_name in self.agent_names:
                    self.known_agent_ids.add(entry.sender)

    # Events -------------------------------------------------------------------------------

    def _remember(self, message_id: str) -> bool:
        if message_id in self._seen:
            return False
        self._seen[message_id] = None
        if len(self._seen) > DEDUPE_SIZE:
            self._seen.popitem(last=False)
        return True

    async def handle_event(self, room_id: str, ev: DomainEvent) -> None:
        if ev.type == "room_join":
            self._spawn(self.learn_room(room_id))
            if self.role:
                self._spawn(self._claim_role(room_id))
            return
        msg = ev.message()
        if msg is None:
            return
        if self.own_id is not None and msg.sender == self.own_id:
            return
        if not self._remember(msg.message_id):
            return
        if msg.sender_name in self.agent_names and msg.sender not in self.known_agent_ids:
            self.known_agent_ids.add(msg.sender)
        if not msg.addressed:
            await self.on_unaddressed(room_id, ev, msg)
            return
        word, args = parse_command(msg.body or "", self.name)
        ctx = CommandContext(
            room_id=room_id,
            event=ev,
            msg=msg,
            word=word,
            args=args,
            thread_root=msg.thread_id or msg.message_id,
            room_name=self.room_names.get(room_id, ""),
        )
        handler = self.handlers.get(word)
        if handler is None:
            await self.on_unknown(ctx)
        else:
            await handler(ctx)

    # Posting ------------------------------------------------------------------------------

    async def _ready_session(self, room_id: str) -> Any:
        session = self.sessions.get(room_id)
        if session is None:
            return None
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(session.ready.wait(), READY_TIMEOUT_S)
        return session if session.ready.is_set() else None

    async def post(self, room_id: str, text: str, *, thread_id: str | None = None) -> list[str]:
        session = await self._ready_session(room_id)
        ids: list[str] = []
        for chunk in split_for_platform(text):
            if session is not None:
                ids.append(
                    await self.ops.post_message(
                        chunk, connection_id=session.connection_id, thread_id=thread_id
                    )
                )
            else:
                self.log.warning("post_without_connection", room=room_id, threaded=bool(thread_id))
                ids.append(await self.ops.post_direct(room_id, chunk))
        return ids

    def asking_agent(self, ctx: CommandContext) -> str | None:
        """The asker's name when it is an agent that wakes only when a message addresses it
        (scribe, skeptic). None for people, and for keymaster and sentinel, which read every
        message in their rooms and must never be addressed back (they would answer each other).
        """
        msg = ctx.msg
        name = msg.sender_name
        if not name or name == self.name or name in DESK_AGENTS:
            return None
        if msg.sender in {self.own_id, self.settings.keymaster_id, self.settings.sentinel_id}:
            return None
        return name if self.is_agent(msg.sender, name) else None

    async def reply(self, ctx: CommandContext, text: str) -> list[str]:
        """People read the thread. An asking agent gets the answer by name in the same thread;
        over the platform limit the first parts are plain posts, so it wakes once, at the end."""
        name = self.asking_agent(ctx)
        if name is None:
            return await self.post(ctx.room_id, text, thread_id=ctx.thread_root)
        *first, last = split_for_platform(text)
        ids: list[str] = []
        for chunk in first:
            ids += await self.post(ctx.room_id, chunk, thread_id=ctx.thread_root)
        await self.deliver(ctx.room_id, last, names=[name], thread_id=ctx.thread_root)
        return ids

    async def targeted(
        self,
        room_id: str,
        text: str,
        *,
        roles: list[str] | None = None,
        names: list[str] | None = None,
        thread_id: str | None = None,
    ) -> str:
        session = await self._ready_session(room_id)
        if session is None:
            raise SwitchError(0, f"no ready connection for room {room_id}", "targeted")
        return await self.ops.send_targeted_message(
            text,
            connection_id=session.connection_id,
            target_roles=roles,
            target_names=names,
            thread_id=thread_id,
        )

    async def deliver(
        self,
        room_id: str,
        text: str,
        *,
        roles: list[str] | None = None,
        names: list[str] | None = None,
        thread_id: str | None = None,
    ) -> str:
        """Send to another agent: by role, then by name, then as a plain ``@name`` post.

        Returns which path worked (``roles``, ``names`` or ``post``); a failed plain post
        raises. The live server decides which path works (docs/PROTOCOL_NOTES.md).
        """
        attempts: list[tuple[str, dict[str, list[str]]]] = []
        if roles:
            attempts.append(("roles", {"roles": roles}))
        if names:
            attempts.append(("names", {"names": names}))
        for how, target in attempts:
            try:
                await self.targeted(room_id, text, thread_id=thread_id, **target)
            except SwitchError as exc:
                self.log.info("delivery_fallback", room=room_id, path=how, detail=exc.detail)
                continue
            self.log.info("delivered", room=room_id, path=how)
            return how
        mention = f"@{names[0]} " if names else (f"@{roles[0]} " if roles else "")
        await self.post(room_id, mention + text, thread_id=thread_id)
        self.log.info("delivered", room=room_id, path="post")
        return "post"

    def session(self, room_id: str) -> Any:
        return self.sessions[room_id]

    def room_ids_named(self, name: str) -> list[str]:
        """Rooms called ``name`` or ``"<bridge>: name"`` (Switch prefixes bridged rooms)."""
        want = name.lower().lstrip("#")
        return [
            rid
            for rid, rname in self.room_names.items()
            if rname.lower() == want
            or rname.lower().endswith(f": {want}")
            or rname.lower().endswith(f": #{want}")
        ]

    # Role -------------------------------------------------------------------------------

    def has_role(self, room_id: str) -> bool:
        session = self.sessions.get(room_id)
        return session is not None and session.connection_id in self._role_conns

    async def _try_role(self, room_id: str) -> bool:
        session = self.sessions.get(room_id)
        if session is None or not self.role:
            return False
        if session.connection_id in self._role_conns:
            return True
        try:
            await self.ops.assume_role(self.role, connection_id=session.connection_id)
        except SwitchError as exc:
            self.log.info("role_not_assumed", room=room_id, role=self.role, detail=exc.detail)
            return False
        self._role_conns.add(session.connection_id)
        self.log.info("role_assumed", room=room_id, role=self.role)
        return True

    async def _claim_role(self, room_id: str) -> None:
        if room_id in self._claiming:
            return
        self._claiming.add(room_id)
        try:
            for _ in range(ROLE_MAX_TRIES):
                if room_id not in self.sessions or await self._try_role(room_id):
                    return
                await asyncio.sleep(self.role_retry_s)
        finally:
            self._claiming.discard(room_id)

    # Rooms --------------------------------------------------------------------------------

    def _spawn(self, coro: Awaitable[None]) -> None:
        task: asyncio.Task[None] = asyncio.ensure_future(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def start_room(self, room_id: str) -> None:
        if room_id in self.sessions or room_id in self._closed_rooms:
            return

        async def on_event(ev: DomainEvent) -> None:
            await self.handle_event(room_id, ev)

        async def on_control(frame: ControlFrame) -> None:
            if self.role and isinstance(frame, SubscriptionChanged):
                await self._try_role(room_id)
            await self.on_control(room_id, frame)

        session = self._factory(
            self.creds,
            room_id,
            self.ops,
            base_url=self.settings.switch_base_url or "",
            state_dir=self.settings.state_dir,
            on_event=on_event,
            on_control=on_control,
        )
        self.sessions[room_id] = session
        self._tasks[room_id] = asyncio.ensure_future(self._run_room(room_id, session))

    async def _run_room(self, room_id: str, session: Any) -> None:
        async def when_ready() -> None:
            await session.ready.wait()
            await self.learn_room(room_id)
            if self.role:
                self._spawn(self._claim_role(room_id))
            await self.on_room_ready(room_id)

        ready_task = asyncio.ensure_future(when_ready())
        try:
            reason: ExitReason = await session.run()
        finally:
            ready_task.cancel()
            self.sessions.pop(room_id, None)
            self._tasks.pop(room_id, None)
        self.log.info("room_connection_ended", room=room_id, reason=str(reason))
        if reason is ExitReason.CLOSED:
            self._closed_rooms.add(room_id)
        if reason in FINAL_FOR_AGENT and self._exit is not None and not self._exit.done():
            self._exit.set_result(reason)

    async def poll_rooms(self) -> None:
        try:
            rooms = await self.ops.list_rooms()
        except SwitchError as exc:
            self.log.warning("list_rooms_failed", detail=exc.detail)
            return
        listed = set()
        for room in rooms:
            if room.archived:
                continue
            listed.add(room.room_id)
            self.room_names[room.room_id] = room.name
            if room.room_id not in self.sessions:
                self.log.info("room_found", room=room.room_id, name=room.name)
                self.start_room(room.room_id)
        for room_id in list(self.sessions):
            if room_id not in listed:
                self.log.info("room_gone", room=room_id)
                await self.sessions[room_id].stop()

    async def _watch_rooms(self) -> None:
        while True:
            await self.poll_rooms()
            await asyncio.sleep(self._room_poll_s)

    async def run_until_stopped(self) -> ExitReason:
        self._exit = asyncio.get_running_loop().create_future()
        watcher = asyncio.ensure_future(self._watch_rooms())
        try:
            return await self._exit
        finally:
            watcher.cancel()
            await self.stop()

    def request_stop(self, reason: ExitReason = ExitReason.STOPPED) -> None:
        if self._exit is not None and not self._exit.done():
            self._exit.set_result(reason)

    async def stop(self) -> None:
        for session in list(self.sessions.values()):
            await session.stop()
        tasks = list(self._tasks.values()) + list(self._background)
        for task in self._background:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
