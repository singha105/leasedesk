"""SwitchAgent: addressed-only dispatch, own/duplicate filtering, threading, room discovery."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest
import respx

from leasedesk.config import Settings
from leasedesk.switch.agent import CommandContext, SwitchAgent, parse_command
from leasedesk.switch.connection import ExitReason
from leasedesk.switch.credentials import AgentCredentials
from leasedesk.switch.models import DomainEvent, SubscriptionChanged
from leasedesk.switch.ops import OpsClient

from .conftest import BASE

OPS = f"{BASE}/agents/agent-1/ops"
OWN = "@switch-agent-aaaa:localhost"
SENTINEL = "@switch-agent-bbbb:localhost"
ARNAB = "@switch-discord-b1-arnab:localhost"


class FakeConnection:
    def __init__(self, creds: Any, room_id: str, ops: Any, **kw: Any) -> None:
        self.room_id = room_id
        self.connection_id = f"conn-{room_id}"
        self.ready = asyncio.Event()
        self.ready.set()
        self._stop = asyncio.Event()

    async def run(self) -> ExitReason:
        await self._stop.wait()
        return ExitReason.STOPPED

    async def stop(self) -> None:
        self._stop.set()


def event(
    body: str,
    *,
    addressed: bool = True,
    sender: str = ARNAB,
    message_id: str = "sw_1",
    thread_id: str | None = None,
    seq: int = 1,
) -> DomainEvent:
    return DomainEvent.model_validate(
        {
            "event": "message",
            "id": seq,
            "type": "message",
            "room_id": "r1",
            "sequence": seq,
            "payload": {
                "addressed": addressed,
                "sender": sender,
                "sender_name": "arnab.",
                "message_id": message_id,
                "body": body,
                "thread_id": thread_id,
                "attachments": [],
            },
        }
    )


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(_env_file=None, LEASEDESK_STATE_DIR=tmp_path)  # type: ignore[call-arg]


def make_agent(settings: Settings, creds: AgentCredentials) -> tuple[SwitchAgent, list[str]]:
    agent = SwitchAgent(
        settings, creds, OpsClient(BASE, creds), connection_factory=FakeConnection, own_id=OWN
    )
    calls: list[str] = []

    async def whoami(ctx: CommandContext) -> None:
        calls.append(f"{ctx.word}|{ctx.args}")

    agent.handlers["whoami"] = whoami
    agent.handlers["approve"] = whoami
    return agent, calls


@pytest.mark.parametrize(
    ("body", "word", "args"),
    [
        ("@keymaster whoami", "whoami", ""),
        ("thanks! @keymaster approve ls-7,", "approve", "ls-7,"),
        ("@KeyMaster  Deny LS-0007 too broad", "deny", "LS-0007 too broad"),
        ("approve LS-0007 @keymaster", "approve", "LS-0007"),
        ("@keymaster: submit\n```lease\n{}\n```", "submit", "```lease\n{}\n```"),
        ("no mention status", "no", "mention status"),
        # A targeted message reads back with "@name " prefixed by Switch; an agent that also
        # wrote the mention (scribe's "@sentinel check") arrives with it twice.
        ("@keymaster @keymaster submit\n```lease\n{}\n```", "submit", "```lease\n{}\n```"),
        ("@keymaster  @KeyMaster: status", "status", ""),
    ],
)
def test_parse_command(body: str, word: str, args: str) -> None:
    assert parse_command(body, "keymaster") == (word, args)


async def test_dispatch_addressed_only(settings: Settings, creds: AgentCredentials) -> None:
    agent, calls = make_agent(settings, creds)
    await agent.handle_event("r1", event("@keymaster whoami", addressed=False, message_id="a"))
    assert calls == []
    await agent.handle_event("r1", event("@keymaster whoami", message_id="b"))
    assert calls == ["whoami|"]


async def test_own_messages_ignored(settings: Settings, creds: AgentCredentials) -> None:
    agent, calls = make_agent(settings, creds)
    await agent.handle_event("r1", event("@keymaster whoami", sender=OWN))
    assert calls == []


async def test_duplicate_message_id_dropped(settings: Settings, creds: AgentCredentials) -> None:
    agent, calls = make_agent(settings, creds)
    await agent.handle_event("r1", event("@keymaster whoami", message_id="m1", seq=1))
    await agent.handle_event("r1", event("@keymaster whoami", message_id="m1", seq=2))
    assert len(calls) == 1


async def test_unknown_word_goes_to_on_unknown(settings: Settings, creds: AgentCredentials) -> None:
    agent, _ = make_agent(settings, creds)
    seen: list[str] = []

    async def unknown(ctx: CommandContext) -> None:
        seen.append(ctx.word)

    agent.on_unknown = unknown  # type: ignore[method-assign]
    await agent.handle_event("r1", event("@keymaster dance"))
    assert seen == ["dance"]


@respx.mock
async def test_reply_uses_thread_root(settings: Settings, creds: AgentCredentials) -> None:
    route = respx.post(f"{OPS}/post_message").mock(
        return_value=httpx.Response(200, json={"result": {"event_id": "sw_r"}})
    )
    agent, _ = make_agent(settings, creds)
    agent.start_room("r1")
    replies: list[CommandContext] = []

    async def capture(ctx: CommandContext) -> None:
        replies.append(ctx)
        await agent.reply(ctx, "ok")

    agent.handlers["whoami"] = capture
    await agent.handle_event("r1", event("@keymaster whoami", message_id="sw_top"))
    await agent.handle_event(
        "r1", event("@keymaster whoami", message_id="sw_in", thread_id="sw_root")
    )
    bodies = [json.loads(c.request.content) for c in route.calls]
    assert bodies[0] == {"body": "ok", "thread_id": "sw_top"}
    assert bodies[1] == {"body": "ok", "thread_id": "sw_root"}
    assert route.calls[0].request.headers["x-switch-connection-id"] == "conn-r1"
    await agent.stop()


@respx.mock
async def test_reply_splits_over_2000(settings: Settings, creds: AgentCredentials) -> None:
    route = respx.post(f"{OPS}/post_message").mock(
        return_value=httpx.Response(200, json={"result": {"event_id": "sw_r"}})
    )
    agent, _ = make_agent(settings, creds)
    agent.start_room("r1")
    await agent.post("r1", "\n".join("x" * 99 for _ in range(45)))
    assert route.call_count == 3
    assert all(len(json.loads(c.request.content)["body"]) <= 2000 for c in route.calls)
    await agent.stop()


@respx.mock
async def test_new_room_from_list_rooms_gets_a_connection(
    settings: Settings, creds: AgentCredentials
) -> None:
    respx.post(f"{OPS}/list_rooms").mock(
        side_effect=[
            httpx.Response(200, json={"result": [{"room_id": "r1", "name": "Discord: a"}]}),
            httpx.Response(
                200,
                json={
                    "result": [
                        {"room_id": "r1", "name": "Discord: a"},
                        {"room_id": "r2", "name": "Discord: b"},
                        {"room_id": "r3", "name": "old", "archived": True},
                    ]
                },
            ),
        ]
    )
    respx.post(f"{OPS}/read_context").mock(
        return_value=httpx.Response(200, json={"result": {"threads": []}})
    )
    respx.post(f"{OPS}/list_participants").mock(
        return_value=httpx.Response(200, json={"result": []})
    )
    agent, _ = make_agent(settings, creds)
    await agent.poll_rooms()
    assert set(agent.sessions) == {"r1"}
    await agent.poll_rooms()
    assert set(agent.sessions) == {"r1", "r2"}
    assert agent.room_ids_named("b") == ["r2"]
    await agent.stop()


@respx.mock
async def test_own_id_and_agent_ids_learned(settings: Settings, creds: AgentCredentials) -> None:
    respx.post(f"{OPS}/read_context").mock(
        return_value=httpx.Response(
            200,
            json={
                "result": {
                    "threads": [
                        {
                            "root": {
                                "id": "sw_1",
                                "kind": "room_join",
                                "sender": OWN,
                                "sender_name": "keymaster",
                            }
                        },
                        {
                            "root": {
                                "id": "sw_2",
                                "kind": "message",
                                "sender": SENTINEL,
                                "sender_name": "sentinel",
                            }
                        },
                        {
                            "root": {
                                "id": "sw_3",
                                "kind": "message",
                                "sender": ARNAB,
                                "sender_name": "arnab.",
                            }
                        },
                    ]
                }
            },
        )
    )
    respx.post(f"{OPS}/list_participants").mock(
        return_value=httpx.Response(
            200,
            json={
                "result": [
                    {"id": "u-k", "name": "keymaster", "type": "agent"},
                    {"id": "u-s", "name": "sentinel", "type": "agent"},
                    {"id": "u-a", "name": "arnab.", "type": "user"},
                ]
            },
        )
    )
    agent = SwitchAgent(settings, creds, OpsClient(BASE, creds), connection_factory=FakeConnection)
    agent.start_room("r1")
    await agent.learn_room("r1")
    assert agent.own_id == OWN
    assert agent.known_agent_ids >= {OWN, SENTINEL}
    assert ARNAB not in agent.known_agent_ids
    assert agent.is_agent(SENTINEL, "sentinel")
    assert agent.is_agent("@unknown:localhost", "sentinel")
    assert not agent.is_agent(ARNAB, "arnab.")
    await agent.stop()


# Role claiming and delivery (shared by keymaster and sentinel since Part 3) -----------------


class CapturingConnection(FakeConnection):
    controls: ClassVar[dict[str, Any]] = {}

    def __init__(self, creds: Any, room_id: str, ops: Any, **kw: Any) -> None:
        super().__init__(creds, room_id, ops, **kw)
        CapturingConnection.controls[room_id] = kw["on_control"]


def mock_room_reads() -> None:
    respx.post(f"{OPS}/read_context").mock(
        return_value=httpx.Response(200, json={"result": {"threads": []}})
    )
    respx.post(f"{OPS}/list_participants").mock(
        return_value=httpx.Response(200, json={"result": []})
    )


async def wait_for(cond: Any, timeout: float = 2.0) -> None:
    for _ in range(int(timeout / 0.01)):
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met")


@respx.mock
async def test_role_claimed_with_retry(settings: Settings, creds: AgentCredentials) -> None:
    mock_room_reads()
    route = respx.post(f"{OPS}/assume_role").mock(
        side_effect=[
            httpx.Response(409, json={"detail": "no such role"}),
            httpx.Response(200, json={"result": {}}),
        ]
    )
    agent = SwitchAgent(
        settings, creds, OpsClient(BASE, creds), connection_factory=FakeConnection, own_id=OWN
    )
    agent.role, agent.role_retry_s = "reviewer", 0.01
    agent.start_room("r1")
    await wait_for(lambda: agent.has_role("r1"))
    assert route.call_count == 2
    assert json.loads(route.calls[1].request.content) == {"role": "reviewer"}
    await agent.stop()


@respx.mock
async def test_role_retried_on_subscription_changed(
    settings: Settings, creds: AgentCredentials
) -> None:
    mock_room_reads()
    route = respx.post(f"{OPS}/assume_role").mock(
        side_effect=[
            httpx.Response(409, json={"detail": "no such role"}),
            httpx.Response(200, json={"result": {}}),
        ]
    )
    agent = SwitchAgent(
        settings, creds, OpsClient(BASE, creds), connection_factory=CapturingConnection, own_id=OWN
    )
    agent.role, agent.role_retry_s = "reviewer", 3600
    agent.start_room("r1")
    await wait_for(lambda: route.call_count == 1)
    await CapturingConnection.controls["r1"](SubscriptionChanged(rooms=["r1"], reason="role added"))
    assert agent.has_role("r1") and route.call_count == 2
    await agent.stop()


@respx.mock
async def test_deliver_tries_roles_then_names_then_a_post(
    settings: Settings, creds: AgentCredentials
) -> None:
    targeted = respx.post(f"{OPS}/send_targeted_message").mock(
        side_effect=[
            httpx.Response(409, json={"detail": "no participant has that role"}),
            httpx.Response(200, json={"result": {"event_id": "sw_t"}}),
            httpx.Response(409, json={"detail": "x"}),
            httpx.Response(409, json={"detail": "x"}),
        ]
    )
    posted = respx.post(f"{OPS}/post_message").mock(
        return_value=httpx.Response(200, json={"result": {"event_id": "sw_p"}})
    )
    agent, _ = make_agent(settings, creds)
    agent.start_room("r1")
    how = await agent.deliver(
        "r1", "hello", roles=["reviewer"], names=["sentinel"], thread_id="sw_root"
    )
    assert how == "names"
    sent = [json.loads(c.request.content) for c in targeted.calls]
    assert sent[0]["target_roles"] == ["reviewer"] and sent[1]["target_names"] == ["sentinel"]
    how = await agent.deliver(
        "r1", "hello", roles=["reviewer"], names=["sentinel"], thread_id="sw_root"
    )
    assert how == "post"
    assert json.loads(posted.calls[0].request.content) == {
        "body": "@sentinel hello",
        "thread_id": "sw_root",
    }
    await agent.stop()
