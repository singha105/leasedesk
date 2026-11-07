"""OpsClient: unwrapping, connection header, typed errors, read-only retries, no key leaks."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx
from structlog.testing import capture_logs

from leasedesk.switch.credentials import AgentCredentials, load_credentials, save_credentials
from leasedesk.switch.errors import BadRequest, Conflict, Forbidden, NotFound, Unavailable
from leasedesk.switch.gateway_admin import GatewayAdmin
from leasedesk.switch.ops import OpsClient

from .conftest import BASE, KEY

OPS = f"{BASE}/agents/agent-1/ops"


async def _nosleep(_: float) -> None:
    return None


@respx.mock
async def test_call_unwraps_result_and_sends_connection_header(creds: AgentCredentials) -> None:
    route = respx.post(f"{OPS}/post_message").mock(
        return_value=httpx.Response(200, json={"result": {"event_id": "sw_1"}})
    )
    async with OpsClient(BASE, creds) as ops:
        event_id = await ops.post_message("hi", connection_id="conn-1", thread_id="sw_0")
    assert event_id == "sw_1"
    req = route.calls.last.request
    assert req.headers["x-switch-connection-id"] == "conn-1"
    assert req.headers["authorization"] == f"Bearer {KEY}"
    assert json.loads(req.content) == {"body": "hi", "thread_id": "sw_0"}


@respx.mock
async def test_errors_map_to_types(creds: AgentCredentials) -> None:
    detail = "Room-scoped document 7bc2 not found in room a458"
    respx.post(f"{OPS}/update_room_document").mock(
        return_value=httpx.Response(400, json={"detail": detail})
    )
    respx.post(f"{OPS}/update_agent_detail").mock(return_value=httpx.Response(403, json={}))
    respx.post(f"{OPS}/nope").mock(return_value=httpx.Response(404, json={"detail": "no op"}))
    respx.post(f"{OPS}/assume_role").mock(return_value=httpx.Response(409, json={"detail": "x"}))
    async with OpsClient(BASE, creds) as ops:
        with pytest.raises(BadRequest) as bad:
            await ops.update_room_document("7bc2", connection_id="c", content="x")
        assert bad.value.detail == detail
        assert bad.value.status == 400
        with pytest.raises(Forbidden):
            await ops.call("update_agent_detail", connection_id="c")
        with pytest.raises(NotFound):
            await ops.call("nope")
        with pytest.raises(Conflict):
            await ops.assume_role("executor", connection_id="c")


@respx.mock
async def test_reads_retry_writes_do_not(creds: AgentCredentials) -> None:
    rooms = respx.post(f"{OPS}/list_rooms").mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(502),
            httpx.Response(
                200, json={"result": [{"room_id": "r1", "name": "Discord: access-desk"}]}
            ),
        ]
    )
    post = respx.post(f"{OPS}/post_message").mock(return_value=httpx.Response(503))
    async with OpsClient(BASE, creds, sleep=_nosleep) as ops:
        got = await ops.list_rooms()
        assert [r.room_id for r in got] == ["r1"]
        assert rooms.call_count == 3
        with pytest.raises(Unavailable):
            await ops.post_message("x", connection_id="c")
    assert post.call_count == 1


@respx.mock
async def test_read_context_and_references_typed(creds: AgentCredentials) -> None:
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
                                "sender": "@switch-agent-0000a1b2:localhost",
                                "sender_name": "keymaster",
                                "body": "keymaster joined the room",
                            },
                            "replies": [],
                        }
                    ]
                }
            },
        )
    )
    respx.post(f"{OPS}/list_references").mock(
        return_value=httpx.Response(
            200,
            json={
                "result": {
                    "documents": [
                        {"id": "d1", "name": "access-policy", "scope": "global"},
                    ],
                    "linked_rooms": [],
                }
            },
        )
    )
    async with OpsClient(BASE, creds) as ops:
        threads = await ops.read_context("r1", limit=10)
        refs = await ops.list_references(connection_id="c")
    assert threads[0].root.kind == "room_join"
    assert refs.documents[0].created_by_agent_id is None


@respx.mock
async def test_post_direct_uses_content_key(creds: AgentCredentials) -> None:
    route = respx.post(f"{BASE}/agents/agent-1/message").mock(
        return_value=httpx.Response(200, json={"ok": True, "event_id": "sw_9"})
    )
    async with OpsClient(BASE, creds) as ops:
        assert await ops.post_direct("room-1", "hello") == "sw_9"
    assert json.loads(route.calls.last.request.content) == {"room_id": "room-1", "content": "hello"}


@respx.mock
async def test_api_key_never_in_repr_or_logs(creds: AgentCredentials) -> None:
    respx.post(f"{OPS}/list_rooms").mock(return_value=httpx.Response(500, text=f"echo {KEY}"))
    assert KEY not in repr(creds)
    with capture_logs() as logs:
        async with OpsClient(BASE, creds, sleep=_nosleep) as ops:
            with pytest.raises(Unavailable) as exc:
                await ops.list_rooms()
    assert KEY not in str(logs)
    assert KEY not in str(exc.value)


def test_credentials_round_trip_mode_600(tmp_path: Path, creds: AgentCredentials) -> None:
    save_credentials(creds, tmp_path)
    path = tmp_path / "agents" / "keymaster.json"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert oct((tmp_path / "agents").stat().st_mode & 0o777) == "0o700"
    loaded = load_credentials("keymaster", tmp_path)
    assert loaded.api_key.get_secret_value() == KEY
    assert loaded.id == "agent-1"


@respx.mock
async def test_gateway_admin_sets_addressing_anyone() -> None:
    respx.post(f"{BASE}/gateway/auth/login").mock(return_value=httpx.Response(200, json={}))
    respx.post(f"{BASE}/gateway/auth/logout").mock(return_value=httpx.Response(200, json={}))
    put = respx.put(f"{BASE}/gateway/agents/agent-1/addressing-policy").mock(
        return_value=httpx.Response(200, json={"addressing_policy": None})
    )
    async with GatewayAdmin(BASE, email="a@b", password="pw") as gw:
        await gw.set_addressing("agent-1", "anyone")
    assert json.loads(put.calls.last.request.content) == {"policy": None}
