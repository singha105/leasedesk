"""python -m leasedesk.keymaster: config refusals, exit codes, a clean stop that revokes nothing."""

from __future__ import annotations

import asyncio
import os
import signal
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from leasedesk.common.schemas import LeaseProposal
from leasedesk.config import Settings
from leasedesk.keymaster.__main__ import (
    EXIT_CONFIG,
    EXIT_TAKEN_OVER,
    document_fetcher,
    keymaster_credentials,
    main,
    serve,
)
from leasedesk.keymaster.render import policy_name
from leasedesk.keymaster.store import LeaseState, LeaseStore
from leasedesk.switch.connection import ExitReason
from leasedesk.switch.credentials import AgentCredentials, save_credentials
from leasedesk.switch.models import (
    ContextThread,
    DocumentContent,
    DocumentMeta,
    Participant,
    References,
    RoomSummary,
)

from ..common.conftest import proposal_dict
from .conftest import ARNAB, SCRIBE, Clock, policy_dict
from .fakes import FakeExecutor

S = LeaseState
USER = "lease-demo-dev"
CREDS = AgentCredentials(name="keymaster", id="km-uuid", api_key="k" * 43)  # type: ignore[arg-type]


class StubOps:
    def __init__(self, rooms: list[RoomSummary] | None = None) -> None:
        self.rooms = rooms or []
        self.roles: list[str] = []

    async def list_rooms(self) -> list[RoomSummary]:
        return self.rooms

    async def list_participants(self, *, connection_id: str) -> list[Participant]:
        return []

    async def read_context(self, room_id: str, **kw: Any) -> list[ContextThread]:
        return []

    async def assume_role(self, role: str, *, connection_id: str) -> dict[str, Any]:
        self.roles.append(role)
        return {}


class IdleConnection:
    def __init__(self, creds: Any, room_id: str, ops: Any, **kw: Any) -> None:
        self.connection_id = f"conn-{room_id}"
        self.ready = asyncio.Event()
        self.ready.set()
        self._stop = asyncio.Event()

    async def run(self) -> ExitReason:
        await self._stop.wait()
        return ExitReason.STOPPED

    async def stop(self) -> None:
        self._stop.set()


class TakenOverConnection(IdleConnection):
    async def run(self) -> ExitReason:
        await asyncio.sleep(0.01)
        return ExitReason.TAKEN_OVER


def settings_for(tmp: Path, **env: Any) -> Settings:
    pfile = tmp / "access-policy.yaml"
    pfile.write_text(yaml.safe_dump(policy_dict()))
    base: dict[str, Any] = {
        "SWITCH_BASE_URL": "http://switch.test",
        "LEASEDESK_POLICY_SOURCE": "file",
        "LEASEDESK_POLICY_FILE": pfile,
        "LEASEDESK_STATE_DIR": tmp / ".state",
        "LEASEDESK_SECRETS_DIR": tmp / ".secrets",
        "LEASEDESK_DB": tmp / "k.db",
    }
    return Settings(_env_file=None, **{**base, **env})  # type: ignore[call-arg]


def test_fake_verdict_in_real_mode_exits_2(tmp_path: Path) -> None:
    settings = settings_for(tmp_path, LEASEDESK_AWS_MODE="real", LEASEDESK_DEV_FAKE_VERDICT="LOW")
    assert main(settings) == EXIT_CONFIG


def test_missing_base_url_exits_2(tmp_path: Path) -> None:
    settings = settings_for(tmp_path, SWITCH_BASE_URL=None)
    assert main(settings) == EXIT_CONFIG


def active_lease(store: LeaseStore, executor: FakeExecutor, clock: Clock) -> str:
    lease = store.create_lease(
        LeaseProposal.model_validate(proposal_dict()),
        submitted_by=SCRIBE,
        submitted_by_name="scribe",
        room_id="r1",
    )
    now = clock()
    for frm, to, fields in [
        (S.AWAITING_CONFIRM, S.UNDER_REVIEW, {"requester": ARNAB, "iam_user": USER}),
        (S.UNDER_REVIEW, S.AWAITING_APPROVAL, {"risk": "LOW"}),
        (
            S.AWAITING_APPROVAL,
            S.ACTIVE,
            {"starts_at": now, "expires_at": now + timedelta(minutes=30)},
        ),
    ]:
        store.transition(lease.id, [frm], to, "test", **fields)
    executor.policies[USER] = {policy_name(lease.id): '{"Version":"2012-10-17"}'}
    return lease.id


async def test_sigint_does_not_revoke(tmp_path: Path, clock: Clock) -> None:
    settings = settings_for(tmp_path)
    store = LeaseStore(settings.db_path, clock=clock)
    executor = FakeExecutor()
    lease_id = active_lease(store, executor, clock)
    task = asyncio.ensure_future(
        serve(
            settings,
            creds=CREDS,
            ops=StubOps(),  # type: ignore[arg-type]
            executor=executor,
            store=store,
            clock=clock,
            connection_factory=IdleConnection,
        )
    )
    await asyncio.sleep(0.1)
    os.kill(os.getpid(), signal.SIGINT)
    assert await asyncio.wait_for(task, 5) == 0
    assert executor.revoked == []
    assert executor.policies[USER] == {policy_name(lease_id): '{"Version":"2012-10-17"}'}
    assert store.require(lease_id).state is S.ACTIVE


async def test_taken_over_exits_3(tmp_path: Path, clock: Clock) -> None:
    settings = settings_for(tmp_path)
    ops = StubOps([RoomSummary(room_id="r1", name="Discord: access-desk")])
    code = await asyncio.wait_for(
        serve(
            settings,
            creds=CREDS,
            ops=ops,  # type: ignore[arg-type]
            executor=FakeExecutor(),
            store=LeaseStore(settings.db_path, clock=clock),
            clock=clock,
            connection_factory=TakenOverConnection,
        ),
        5,
    )
    assert code == EXIT_TAKEN_OVER


async def test_executor_role_claimed_when_room_ready(tmp_path: Path, clock: Clock) -> None:
    settings = settings_for(tmp_path)
    ops = StubOps([RoomSummary(room_id="r1", name="Discord: access-desk")])
    task = asyncio.ensure_future(
        serve(
            settings,
            creds=CREDS,
            ops=ops,  # type: ignore[arg-type]
            executor=FakeExecutor(),
            store=LeaseStore(settings.db_path, clock=clock),
            clock=clock,
            connection_factory=IdleConnection,
        )
    )
    for _ in range(50):
        if ops.roles:
            break
        await asyncio.sleep(0.02)
    os.kill(os.getpid(), signal.SIGINT)
    assert await asyncio.wait_for(task, 5) == 0
    assert ops.roles == ["executor"]


class DocOps:
    def __init__(self, docs: list[DocumentMeta]) -> None:
        self.docs = docs
        self.loaded: list[str] = []

    async def list_references(self, *, connection_id: str) -> References:
        return References(documents=self.docs)

    async def load_internal_documents(
        self, ids: list[str], *, connection_id: str
    ) -> list[DocumentContent]:
        self.loaded += ids
        return [DocumentContent(id=i, content=f"content of {i}") for i in ids]


class DocAgent:
    def __init__(self, ops: DocOps) -> None:
        self.ops = ops
        self.sessions = {"r1": IdleConnection(None, "r1", None)}


async def test_policy_document_must_be_person_written() -> None:
    agent_doc = DocumentMeta(id="d-agent", name="access-policy", created_by_agent_id="scribe-id")
    ops = DocOps([agent_doc])
    fetch = document_fetcher(lambda: DocAgent(ops), "access-policy")  # type: ignore[arg-type,return-value]
    with pytest.raises(LookupError, match="person-written"):
        await fetch()
    ops.docs.append(DocumentMeta(id="d-person", name="access-policy", scope="global"))
    assert await fetch() == "content of d-person"
    assert ops.loaded == ["d-person"]


def test_credentials_read_from_secrets_dir(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    save_credentials(CREDS, settings.secrets_dir)
    assert keymaster_credentials(settings).id == CREDS.id


class PerRoomDocOps(DocOps):
    def __init__(self, by_connection: dict[str, list[DocumentMeta]]) -> None:
        super().__init__([])
        self.by_connection = by_connection

    async def list_references(self, *, connection_id: str) -> References:
        return References(documents=self.by_connection.get(connection_id, []))


async def test_policy_document_found_in_any_room() -> None:
    doc = DocumentMeta(id="d-person", name="access-policy", scope="global")
    ops = PerRoomDocOps({"conn-desk": [doc]})
    agent = DocAgent(ops)
    agent.sessions = {
        "town-square": IdleConnection(None, "town-square", None),
        "desk": IdleConnection(None, "desk", None),
    }
    fetch = document_fetcher(lambda: agent, "access-policy")  # type: ignore[arg-type,return-value]
    assert await fetch() == "content of d-person"


async def test_two_different_policy_documents_refused() -> None:
    a = DocumentMeta(id="d-1", name="access-policy")
    b = DocumentMeta(id="d-2", name="access-policy")
    agent = DocAgent(PerRoomDocOps({"conn-desk": [a], "conn-sec": [b]}))
    agent.sessions = {
        "desk": IdleConnection(None, "desk", None),
        "sec": IdleConnection(None, "sec", None),
    }
    fetch = document_fetcher(lambda: agent, "access-policy")  # type: ignore[arg-type,return-value]
    with pytest.raises(LookupError, match="more than one"):
        await fetch()
