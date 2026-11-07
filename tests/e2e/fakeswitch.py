"""An in-process Switch for end-to-end runs of the real keymaster and sentinel.

Agents reach it the way they reach the live server: ops over HTTP (here an
``httpx.MockTransport``) and one session per room from ``connection_factory``. Events reach
each session through a queue, so an agent that posts never runs another agent's handler inside
its own call, as on the live server. Shapes follow docs/PROTOCOL_NOTES.md:

- A plain post reaches every other agent in the room; it is addressed to an agent when it
  mentions ``@name``.
- ``send_targeted_message`` reaches the holders of ``target_roles`` (first) or the agents in
  ``target_names``; each sees the body as ``@name <body>``, addressed.
- One live role lease per agent across all rooms.
- Person-written documents come in a package (``created_by_agent_id`` null); documents an
  agent creates carry its id.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from leasedesk.switch.connection import ExitReason
from leasedesk.switch.credentials import AgentCredentials
from leasedesk.switch.models import DomainEvent

PACKAGE = "leasedesk-governance"


def person_id(name: str) -> str:
    return f"@switch-discord-b1-{name.lower()}:localhost"


def agent_participant_id(name: str) -> str:
    return f"@switch-agent-{name}:localhost"


@dataclass
class Posted:
    room_id: str
    sender: str
    sender_name: str
    body: str
    thread_id: str | None
    message_id: str
    targets: list[str] = field(default_factory=list)  # agent names (targeted sends only)

    @property
    def thread_root(self) -> str:
        return self.thread_id or self.message_id


@dataclass
class _Agent:
    name: str
    uuid: str
    participant: str
    key: str


@dataclass
class _Room:
    id: str
    name: str
    agents: list[str]
    people: list[str]
    roles: dict[str, list[str]]  # role -> holder agent names
    documents: list[dict[str, Any]] = field(default_factory=list)


class FakeSession:
    """What ``SwitchAgent.start_room`` expects from a connection factory."""

    def __init__(self, sw: FakeSwitch, agent: str, room_id: str, on_event: Any) -> None:
        self.sw, self.agent, self.room_id = sw, agent, room_id
        self.connection_id = f"conn-{agent}-{room_id}"
        self.ready = asyncio.Event()
        self.queue: asyncio.Queue[DomainEvent] = asyncio.Queue()
        self._on_event = on_event
        self._stopped = asyncio.Event()

    async def run(self) -> ExitReason:
        self.sw.sessions[(self.agent, self.room_id)] = self
        self.ready.set()
        stop = asyncio.ensure_future(self._stopped.wait())
        try:
            while True:
                get = asyncio.ensure_future(self.queue.get())
                done, _ = await asyncio.wait({get, stop}, return_when=asyncio.FIRST_COMPLETED)
                if stop in done:
                    get.cancel()
                    return ExitReason.STOPPED
                try:
                    await self._on_event(get.result())
                except Exception as exc:  # recorded; the test fails on any
                    self.sw.errors.append(f"{self.agent} in {self.room_id}: {exc!r}")
                finally:
                    self.queue.task_done()
        finally:
            stop.cancel()
            self.sw.sessions.pop((self.agent, self.room_id), None)

    async def stop(self) -> None:
        self._stopped.set()


class FakeSwitch:
    def __init__(self) -> None:
        self.agents: dict[str, _Agent] = {}
        self.rooms: dict[str, _Room] = {}
        self.sessions: dict[tuple[str, str], FakeSession] = {}
        self.packages: list[dict[str, Any]] = []
        self.documents: dict[str, dict[str, Any]] = {}
        self.posted: list[Posted] = []
        self.errors: list[str] = []
        self._ids = itertools.count(1)
        self._seq = itertools.count(1)

    # Setup ------------------------------------------------------------------------------------

    def agent(self, name: str) -> AgentCredentials:
        a = _Agent(name, f"{name}-uuid", agent_participant_id(name), f"key-{name}-" + "k" * 30)
        self.agents[name] = a
        return AgentCredentials(name=name, id=a.uuid, api_key=a.key)  # type: ignore[arg-type]

    def room(
        self,
        name: str,
        agents: list[str],
        people: list[str],
        roles: list[str] | None = None,
    ) -> str:
        room_id = f"room-{name}"
        self.rooms[room_id] = _Room(room_id, name, agents, people, {r: [] for r in roles or []})
        return room_id

    def package(self, room_ids: list[str], docs: dict[str, str]) -> None:
        """Person-written documents in the governance package, attached to ``room_ids``."""
        metas = []
        for name, content in docs.items():
            doc_id = f"doc-{next(self._ids)}"
            self.documents[doc_id] = {"id": doc_id, "name": name, "content": content}
            metas.append({"id": doc_id, "name": name, "created_by_agent_id": None})
        self.packages.append(
            {"id": f"pkg-{next(self._ids)}", "name": PACKAGE, "rooms": room_ids, "documents": metas}
        )

    def holders(self, room_id: str, role: str) -> list[str]:
        return list(self.rooms[room_id].roles.get(role, []))

    # What people do ---------------------------------------------------------------------------

    async def say(self, room_id: str, person: str, body: str, thread_id: str | None = None) -> str:
        return self._publish(room_id, person_id(person), person.lower(), body, thread_id)

    async def post_as(
        self, agent: str, room_id: str, body: str, thread_id: str | None = None
    ) -> str:
        """An agent without a session (scribe's stand-in) posts a plain message."""
        a = self.agents[agent]
        return self._publish(room_id, a.participant, a.name, body, thread_id)

    def posts(self, room_id: str | None = None) -> list[Posted]:
        return [p for p in self.posted if room_id is None or p.room_id == room_id]

    def texts(self, room_id: str | None = None, *, by: str | None = None) -> list[str]:
        return [p.body for p in self.posts(room_id) if by is None or p.sender_name == by]

    async def wait_for(self, pred: Callable[[], Any], timeout: float = 5.0) -> Any:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = pred()
            if result:
                return result
            await asyncio.sleep(0.01)
        raise TimeoutError(f"condition not met in {timeout} s; last posts: {self.texts()[-4:]}")

    async def settle(self) -> None:
        """Wait until every queued event has been handled (and those it caused)."""
        for _ in range(50):
            await asyncio.sleep(0.01)
            if all(s.queue.empty() for s in self.sessions.values()):
                await asyncio.sleep(0.02)
                if all(s.queue.empty() for s in self.sessions.values()):
                    return

    # Delivery ---------------------------------------------------------------------------------

    def _event(self, room_id: str, msg: Posted, body: str, addressed: bool) -> DomainEvent:
        seq = next(self._seq)
        return DomainEvent.model_validate(
            {
                "event": "message",
                "id": seq,
                "type": "message",
                "room_id": room_id,
                "sequence": seq,
                "payload": {
                    "addressed": addressed,
                    "sender": msg.sender,
                    "sender_name": msg.sender_name,
                    "message_id": msg.message_id,
                    "body": body,
                    "thread_id": msg.thread_id,
                    "attachments": [],
                },
            }
        )

    def _publish(
        self,
        room_id: str,
        sender: str,
        sender_name: str,
        body: str,
        thread_id: str | None,
        targets: list[str] | None = None,
    ) -> str:
        msg = Posted(room_id, sender, sender_name, body, thread_id, f"sw_{next(self._ids)}")
        msg.targets = list(targets or [])
        self.posted.append(msg)
        for agent in self.rooms[room_id].agents:
            session = self.sessions.get((agent, room_id))
            if session is None or agent == sender_name:
                continue
            if targets is not None:
                if agent in targets:
                    session.queue.put_nowait(self._event(room_id, msg, f"@{agent} {body}", True))
                continue
            addressed = f"@{agent}" in body.lower()
            session.queue.put_nowait(self._event(room_id, msg, body, addressed))
        return msg.message_id

    # Ops --------------------------------------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def connection_factory(
        self,
        creds: AgentCredentials,
        room_id: str,
        ops: Any,
        *,
        on_event: Any,
        **_: Any,
    ) -> FakeSession:
        return FakeSession(self, creds.name, room_id, on_event)

    def _caller(self, request: httpx.Request) -> _Agent:
        key = request.headers.get("authorization", "").removeprefix("Bearer ")
        for a in self.agents.values():
            if a.key == key:
                return a
        raise PermissionError("unknown key")

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        try:
            agent = self._caller(request)
        except PermissionError:
            return httpx.Response(401, json={"detail": "bad key"})
        parts = request.url.path.strip("/").split("/")
        body = json.loads(request.content or b"{}")
        if parts[-1] == "message":  # POST /agents/{id}/message (post_direct)
            mid = self._publish(
                body["room_id"], agent.participant, agent.name, body["content"], None
            )
            return httpx.Response(200, json={"event_id": mid})
        op = parts[-1]
        conn = request.headers.get("x-switch-connection-id")
        room_id = next((r for (a, r), s in self.sessions.items() if s.connection_id == conn), None)
        try:
            result = self._op(agent, op, room_id, body)
        except _OpError as exc:
            return httpx.Response(exc.status, json={"detail": exc.detail})
        return httpx.Response(200, json={"result": result})

    def _op(self, agent: _Agent, op: str, room_id: str | None, args: dict[str, Any]) -> Any:
        if op == "list_rooms":
            return [
                {"room_id": r.id, "name": r.name, "archived": False}
                for r in self.rooms.values()
                if agent.name in r.agents
            ]
        if op == "read_context":
            return {"threads": []}
        if room_id is None:
            raise _OpError(400, f"{op} needs a connection")
        room = self.rooms[room_id]
        if op == "list_participants":
            return [
                *(
                    {"id": self.agents[a].participant, "name": a, "type": "agent"}
                    for a in room.agents
                ),
                *({"id": person_id(p), "name": p.lower(), "type": "user"} for p in room.people),
            ]
        if op == "post_message":
            mid = self._publish(
                room_id, agent.participant, agent.name, args["body"], args.get("thread_id")
            )
            return {"event_id": mid}
        if op == "send_targeted_message":
            return {"event_id": self._targeted(agent, room, args)}
        if op == "assume_role":
            return self._assume(agent, room, args["role"])
        if op == "list_references":
            packages = [
                {"id": p["id"], "name": p["name"], "documents": p["documents"]}
                for p in self.packages
                if room_id in p["rooms"]
            ]
            return {"documents": room.documents, "packages": packages, "linked_rooms": []}
        if op == "load_internal_documents":
            return [self.documents[i] for i in args["ids"] if i in self.documents]
        if op == "create_room_document":
            doc_id = f"doc-{next(self._ids)}"
            meta = {"id": doc_id, "name": args["name"], "created_by_agent_id": agent.uuid}
            room.documents.append(meta)
            self.documents[doc_id] = {
                "id": doc_id,
                "name": args["name"],
                "content": args["content"],
            }
            return {"document_id": doc_id}
        if op == "update_room_document":
            self.documents[args["document_id"]]["content"] = args["content"]
            return {}
        raise _OpError(404, f"op {op} not in the fake")

    def _targeted(self, agent: _Agent, room: _Room, args: dict[str, Any]) -> str:
        roles = args.get("target_roles") or []
        names = args.get("target_names") or []
        if roles:
            undefined = [r for r in roles if r not in room.roles]
            if undefined:
                raise _OpError(400, f"Roles not defined in room: {', '.join(undefined)}")
            targets = sorted({h for r in roles for h in room.roles[r]})
        else:
            targets = [n for n in names if n in room.agents]
        if not targets:
            raise _OpError(409, "no participant matches")
        return self._publish(
            room.id, agent.participant, agent.name, args["body"], args.get("thread_id"), targets
        )

    def _assume(self, agent: _Agent, room: _Room, role: str) -> dict[str, Any]:
        if role not in room.roles:
            raise _OpError(400, f"Roles not defined in room: {role}")
        for other in self.rooms.values():
            if other.id != room.id and any(agent.name in h for h in other.roles.values()):
                raise _OpError(400, "You already hold a role lease; release it first")
        if agent.name not in room.roles[role]:
            room.roles[role].append(agent.name)
        return {"role": role, "room_id": room.id}


class _OpError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status, self.detail = status, detail
