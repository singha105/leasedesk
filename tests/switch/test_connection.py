"""RoomConnection against the fake server: heartbeat, resume, eviction codes, gap, dedupe."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest
import respx

from leasedesk.switch.connection import CursorStore, ExitReason, RoomConnection
from leasedesk.switch.credentials import AgentCredentials
from leasedesk.switch.models import DomainEvent
from leasedesk.switch.ops import OpsClient

from .fakeswitch import ROOM, FakeConn, FakeSwitch


async def _nosleep(_: float) -> None:
    await asyncio.sleep(0)


class Harness:
    def __init__(self, server: FakeSwitch, creds: AgentCredentials, tmp: Path) -> None:
        self.events: list[DomainEvent] = []
        self.handler_delay = 0.0
        self.claim_requests: list[httpx.Request] = []
        self.router = respx.mock(assert_all_called=False)
        self.claim = self.router.post(f"{server.base_url}/agents/agent-1/ops/connect_to_room").mock(
            return_value=httpx.Response(200, json={"result": {"room_id": ROOM}})
        )
        self.router.route(host="127.0.0.1").pass_through()
        self.ops = OpsClient(server.base_url, creds)
        self.conn = RoomConnection(
            creds,
            ROOM,
            self.ops,
            base_url=server.base_url,
            state_dir=tmp,
            on_event=self.on_event,
            sleep=_nosleep,
        )

    async def on_event(self, ev: DomainEvent) -> None:
        self.events.append(ev)
        if self.handler_delay and len(self.events) == 1:
            await asyncio.sleep(self.handler_delay)

    async def run(self, timeout: float = 5.0) -> ExitReason:
        with self.router:
            try:
                return await asyncio.wait_for(self.conn.run(), timeout)
            finally:
                self.claim_requests = [call.request for call in self.claim.calls]
                await self.ops.aclose()


async def test_pong_within_200ms_while_handler_sleeps_5s(
    creds: AgentCredentials, tmp_path: Path
) -> None:
    latencies: list[float] = []

    async def script(c: FakeConn) -> None:
        await c.state()
        await c.message(1)
        await asyncio.sleep(0.05)
        sent = await c.ping()
        await c.wait_pong(0, timeout=1.0)
        latencies.append(c.server.pongs[-1][0] - sent)
        await c.evict("taken_over")

    async with FakeSwitch([script]) as server:
        h = Harness(server, creds, tmp_path)
        h.handler_delay = 5.0
        started = time.monotonic()
        assert await h.run() is ExitReason.TAKEN_OVER
    assert latencies and latencies[0] < 0.2
    assert time.monotonic() - started < 2.0  # stop() cancels the sleeping handler


async def test_pong_cursor_is_highest_processed_id(creds: AgentCredentials, tmp_path: Path) -> None:
    async def script(c: FakeConn) -> None:
        await c.state()
        await c.message(3)
        await c.message(4)
        await asyncio.sleep(0.1)
        await c.ping()
        await c.wait_pong(4)
        await c.evict("taken_over")

    async with FakeSwitch([script]) as server:
        h = Harness(server, creds, tmp_path)
        await h.run()
    assert [e.id for e in h.events] == [3, 4]


async def test_resume_sends_start_from_and_expected_generation(
    creds: AgentCredentials, tmp_path: Path
) -> None:
    async def first(c: FakeConn) -> None:
        await c.state(generation=10)
        await c.message(5)
        await asyncio.sleep(0.1)
        await c.ping()
        await c.wait_pong(5)
        await c.evict("heartbeat_lapsed")

    async def second(c: FakeConn) -> None:
        await c.state(generation=11)
        await c.evict("taken_over")

    async with FakeSwitch([first, second]) as server:
        h = Harness(server, creds, tmp_path)
        assert await h.run() is ExitReason.TAKEN_OVER
    first_open, second_open = server.opens
    assert "start_from" not in first_open
    assert first_open["rooms"] == ROOM
    assert first_open["scope"] == "all" and first_open["filter"] == "all"
    assert second_open["connection_id"] == first_open["connection_id"]
    assert second_open["start_from"] == "5"
    assert second_open["expected_generation"] == "10"


async def test_taken_over_stops_without_reconnect(creds: AgentCredentials, tmp_path: Path) -> None:
    async def script(c: FakeConn) -> None:
        await c.state()
        await c.evict("taken_over")

    async with FakeSwitch([script, script]) as server:
        h = Harness(server, creds, tmp_path)
        assert await h.run() is ExitReason.TAKEN_OVER
        await asyncio.sleep(0.2)
    assert len(server.opens) == 1


async def test_closed_does_not_retry(creds: AgentCredentials, tmp_path: Path) -> None:
    async def script(c: FakeConn) -> None:
        await c.state()
        await c.evict("closed")

    async with FakeSwitch([script, script]) as server:
        h = Harness(server, creds, tmp_path)
        assert await h.run() is ExitReason.CLOSED
    assert len(server.opens) == 1


async def test_gap_moves_cursor_and_flags_next_event(
    creds: AgentCredentials, tmp_path: Path
) -> None:
    async def script(c: FakeConn) -> None:
        await c.state()
        await c.gap(100)
        await c.ping()
        await c.wait_pong(100)
        await c.message(101)
        await c.message(102)
        await asyncio.sleep(0.1)
        await c.ping()
        await c.wait_pong(102)
        await c.evict("taken_over")

    async with FakeSwitch([script]) as server:
        h = Harness(server, creds, tmp_path)
        await h.run()
    assert [(e.id, e.after_gap) for e in h.events] == [(101, True), (102, False)]


async def test_duplicate_event_ids_dropped(creds: AgentCredentials, tmp_path: Path) -> None:
    async def script(c: FakeConn) -> None:
        await c.state()
        await c.message(7)
        await c.message(7)
        await c.message(6)
        await c.message(8)
        await asyncio.sleep(0.1)
        await c.evict("taken_over")

    async with FakeSwitch([script]) as server:
        h = Harness(server, creds, tmp_path)
        await h.run()
    assert [e.id for e in h.events] == [7, 8]


async def test_4401_returns_auth_failed(creds: AgentCredentials, tmp_path: Path) -> None:
    async def script(c: FakeConn) -> None:
        await c.close(4401)

    async with FakeSwitch([script, script]) as server:
        h = Harness(server, creds, tmp_path)
        assert await h.run() is ExitReason.AUTH_FAILED
    assert len(server.opens) == 1


async def test_4409_returns_refused(creds: AgentCredentials, tmp_path: Path) -> None:
    async def script(c: FakeConn) -> None:
        await c.close(4409)

    async with FakeSwitch([script, script]) as server:
        h = Harness(server, creds, tmp_path)
        assert await h.run() is ExitReason.REFUSED
    assert len(server.opens) == 1


async def test_1012_reconnects_and_claims_again(creds: AgentCredentials, tmp_path: Path) -> None:
    async def restart(c: FakeConn) -> None:
        await c.state()
        await asyncio.sleep(0.1)
        await c.close(1012)

    async def final(c: FakeConn) -> None:
        await c.state()
        await asyncio.sleep(0.1)
        await c.evict("taken_over")

    async with FakeSwitch([restart, final]) as server:
        h = Harness(server, creds, tmp_path)
        assert await h.run() is ExitReason.TAKEN_OVER
    assert len(server.opens) == 2
    assert len(h.claim_requests) == 2
    claim_req = h.claim_requests[-1]
    assert claim_req.headers["x-switch-connection-id"] == server.opens[0]["connection_id"]
    assert json.loads(claim_req.content)["room_id"] == ROOM


async def test_stop_returns_stopped(creds: AgentCredentials, tmp_path: Path) -> None:
    async def script(c: FakeConn) -> None:
        await c.state()
        await c.hold()

    async with FakeSwitch([script]) as server:
        h = Harness(server, creds, tmp_path)

        async def stopper() -> None:
            await asyncio.wait_for(h.conn.ready.wait(), 2)
            await h.conn.stop()

        task = asyncio.create_task(stopper())
        assert await h.run() is ExitReason.STOPPED
        await task
    state = json.loads((tmp_path / f"keymaster.{ROOM}.cursor.json").read_text())
    assert state["connection_id"] == server.opens[0]["connection_id"]
    assert state["generation"] == 10


def test_cursor_file_written_at_most_every_2s(tmp_path: Path) -> None:
    now = [100.0]
    store = CursorStore(tmp_path / "a.cursor.json", clock=lambda: now[0])
    store.update("c1", 1, 10)
    assert json.loads(store.path.read_text())["cursor"] == 1
    now[0] = 101.0
    store.update("c1", 2, 10)
    assert json.loads(store.path.read_text())["cursor"] == 1
    now[0] = 102.1
    store.update("c1", 3, 10)
    assert json.loads(store.path.read_text())["cursor"] == 3
    now[0] = 102.5
    store.update("c1", 4, 10)
    store.flush()
    assert json.loads(store.path.read_text())["cursor"] == 4
    assert oct(store.path.stat().st_mode & 0o777) == "0o600"
    assert CursorStore(store.path).load() == ("c1", 4, 10)


@pytest.mark.parametrize("missing", [True, False])
def test_cursor_store_load_missing_or_bad(tmp_path: Path, missing: bool) -> None:
    path = tmp_path / "x.cursor.json"
    if not missing:
        path.write_text("{not json")
    assert CursorStore(path).load() == (None, None, None)


async def test_claim_failing_three_times_gives_room_lost(
    creds: AgentCredentials, tmp_path: Path
) -> None:
    async def script(c: FakeConn) -> None:
        await c.state()
        await c.hold()

    async with FakeSwitch([script] * 5) as server:
        h = Harness(server, creds, tmp_path)
        h.claim.mock(
            return_value=httpx.Response(400, json={"detail": "agent not assigned to room"})
        )
        assert await h.run() is ExitReason.ROOM_LOST
    assert len(server.opens) == 3


def test_backoff_stays_between_half_and_ten_seconds() -> None:
    from leasedesk.switch.connection import backoff_delay

    for attempt in range(0, 30):
        for r in (0.0, 0.5, 1.0):
            assert 0.5 <= backoff_delay(attempt, lambda r=r: r) <= 10.0
    assert backoff_delay(20, lambda: 1.0) == 10.0
