"""The e2e world: FakeSwitch, the real keymaster and sentinel, AWS on moto, an injected clock.

keymaster and sentinel are built as their ``serve()`` builds them (person-document fetchers,
the boto executor), with FakeSwitch's transport and sessions in place of the live server. Each
test gets a fresh world; the whole e2e run must finish in under 60 s.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import boto3
import pytest
import yaml
from moto import mock_aws

from leasedesk.common.policy import PolicyLoader
from leasedesk.config import Settings
from leasedesk.keymaster.executor import BotoIamExecutor
from leasedesk.keymaster.mockaws import MockSandbox, seed
from leasedesk.keymaster.service import Keymaster
from leasedesk.keymaster.store import Lease, LeaseState, LeaseStore
from leasedesk.sentinel.agent import Sentinel
from leasedesk.sentinel.catalog import CatalogLoader
from leasedesk.sentinel.store import ReviewStore
from leasedesk.switch.documents import person_document_fetcher
from leasedesk.switch.ops import OpsClient

from ..keymaster.conftest import Clock, policy_dict
from .fakeswitch import FakeSwitch, agent_participant_id, person_id
from .scribe import ScribeStandIn

ROOT = Path(__file__).resolve().parents[2]
ACCOUNT = "123456789012"
BASE = "http://switch.test"
BUDGET_S = 60.0
PEOPLE = ["Arnab", "Priya", "Sam"]


def e2e_policy() -> dict[str, Any]:
    """The demo policy: Arnab asks, Priya and Sam (security) approve, one HIGH approval."""
    return policy_dict(
        principals={person_id("Arnab"): "lease-demo-dev", person_id("Priya"): "lease-demo-dev"},
        approvers=[
            {"id": person_id("Priya"), "name": "Priya", "groups": ["security"]},
            {"id": person_id("Sam"), "name": "Sam", "groups": ["security"]},
            {"id": person_id("Arnab"), "name": "Arnab", "groups": ["eng-lead"]},
        ],
        high_risk={"approvals": 1, "require_group": "security", "approval_room": "security-review"},
    )


def e2e_catalog() -> str:
    return (ROOT / "switch/docs/resource-catalog.md").read_text().replace("<ACCOUNT_ID>", ACCOUNT)


@dataclass
class World:
    sw: FakeSwitch
    km: Keymaster
    se: Sentinel
    scribe: ScribeStandIn
    clock: Clock
    box: MockSandbox
    executor: BotoIamExecutor
    access: str
    review: str

    def lease(self, lease_id: str) -> Lease:
        return self.km.store.require(lease_id)

    async def state_is(self, lease_id: str, state: LeaseState, timeout: float = 5.0) -> None:
        await self.sw.wait_for(lambda: self.lease(lease_id).state is state, timeout)

    async def request(self, asker: str = "Arnab", text: str = "") -> str:
        """A person asks scribe in access-desk; the thread is that message."""
        body = text or "@scribe I need to read the October invoice exports for an hour (BILL-142)"
        return await self.sw.say(self.access, asker, body)

    async def drafted(self, thread: str, asker: str, proposal: dict[str, Any]) -> str:
        """scribe submits ``proposal`` in ``thread``; returns the new lease id."""
        before = {lease.id for lease in self.km.store.recent(50)}
        await self.scribe.draft(self.access, thread, asker, proposal)
        await self.sw.settle()
        new = [lease.id for lease in self.km.store.recent(50) if lease.id not in before]
        assert len(new) == 1, self.sw.texts()[-3:]
        return new[0]

    async def confirmed(self, lease_id: str, by: str = "Arnab") -> None:
        thread = self.lease(lease_id).thread_id
        await self.sw.say(self.access, by, f"@keymaster confirm {lease_id}", thread)
        await self.sw.settle()

    async def to_approval(self, proposal: dict[str, Any], asker: str = "Arnab") -> str:
        thread = await self.request(asker)
        lease_id = await self.drafted(thread, asker, proposal)
        await self.confirmed(lease_id, asker)
        await self.state_is(lease_id, LeaseState.AWAITING_APPROVAL)
        return lease_id

    async def approved(self, lease_id: str, by: str = "Priya") -> None:
        thread = self.lease(lease_id).thread_id
        await self.sw.say(self.access, by, f"@keymaster approve {lease_id}", thread)
        await self.sw.settle()

    def proposal(self, **over: Any) -> dict[str, Any]:
        """The scribe brief's example lease, on the moto bucket."""
        arn = f"arn:aws:s3:::{self.box.bucket}"
        base: dict[str, Any] = {
            "kind": "lease_proposal",
            "version": 1,
            "requester_hint": "Arnab",
            "summary": "Read October invoice exports to debug the billing report",
            "justification": "Debugging ticket BILL-142; report totals are off",
            "duration_minutes": 60,
            "statements": [
                {
                    "sid": "ReadInvoiceExports",
                    "actions": ["s3:GetObject"],
                    "resources": [f"{arn}/exports/2026-10/*"],
                },
                {
                    "sid": "ListInvoiceExports",
                    "actions": ["s3:ListBucket"],
                    "resources": [arn],
                    "conditions": {"StringLike": {"s3:prefix": ["exports/2026-10/*"]}},
                },
            ],
        }
        return {**base, **over}


@pytest.fixture(scope="package", autouse=True)
def _e2e_budget() -> Iterator[None]:
    start = time.monotonic()
    yield
    took = time.monotonic() - start
    assert took < BUDGET_S, f"the e2e run took {took:.1f} s (budget {BUDGET_S:.0f} s)"


def _settings(tmp: Path, name: str) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        SWITCH_BASE_URL=BASE,
        LEASEDESK_AWS_MODE="mock",
        LEASEDESK_TZ="America/New_York",
        LEASEDESK_STATE_DIR=tmp / f".state-{name}",
        LEASEDESK_KEYMASTER_ID=agent_participant_id("keymaster"),
        LEASEDESK_SENTINEL_ID=agent_participant_id("sentinel"),
    )


@pytest.fixture
async def world(tmp_path: Path) -> AsyncIterator[World]:
    with mock_aws():
        session = boto3.Session(
            aws_access_key_id="testing", aws_secret_access_key="testing", region_name="us-east-1"
        )
        box = seed(session)
        executor = BotoIamExecutor(session, boundary_doc=box.boundary_doc, poll_interval=0.01)
        clock = Clock()
        sw = FakeSwitch()
        km_creds, se_creds = sw.agent("keymaster"), sw.agent("sentinel")
        sw.agent("scribe")
        sw.agent("skeptic")
        everyone = ["scribe", "skeptic", "keymaster", "sentinel"]
        access = sw.room("access-desk", everyone, PEOPLE, ["drafter", "reviewer", "executor"])
        review = sw.room("security-review", ["keymaster", "sentinel"], PEOPLE)
        sw.rooms[access].roles["drafter"].append("scribe")
        sw.package(
            [access, review],
            {"access-policy": yaml.safe_dump(e2e_policy()), "resource-catalog": e2e_catalog()},
        )

        holder: list[Any] = []
        km_settings = _settings(tmp_path, "km")
        km_ops = OpsClient(BASE, km_creds, transport=sw.transport())
        km = Keymaster(
            km_settings,
            km_creds,
            km_ops,
            store=LeaseStore(tmp_path / "keymaster.db", clock=clock),
            executor=executor,
            loader=PolicyLoader(person_document_fetcher(lambda: km, "access-policy")),
            clock=clock,
            own_id=km_settings.keymaster_id,
            jsonl=tmp_path / "ledger.jsonl",
            connection_factory=sw.connection_factory,
        )
        se_settings = _settings(tmp_path, "se")
        se_ops = OpsClient(BASE, se_creds, transport=sw.transport())
        se = Sentinel(
            se_settings,
            se_creds,
            se_ops,
            policy_loader=PolicyLoader(person_document_fetcher(lambda: holder[0], "access-policy")),
            catalog_loader=CatalogLoader(
                person_document_fetcher(lambda: holder[0], "resource-catalog")
            ),
            store=ReviewStore(tmp_path / "sentinel.db", clock=clock),
            clock=clock,
            own_id=se_settings.sentinel_id,
            connection_factory=sw.connection_factory,
        )
        holder.append(se)
        runs = [
            asyncio.ensure_future(km.run_until_stopped()),
            asyncio.ensure_future(se.run_until_stopped()),
        ]
        try:
            await sw.wait_for(lambda: len(sw.sessions) == 4)
            await km.reload_policy()
            await se.reload()
            assert km.policy is not None and se.policy_loader.current is not None
            assert se.catalog_loader.current is not None, se.catalog_loader.last_error
            await sw.wait_for(
                lambda: (
                    sw.holders(access, "executor") == ["keymaster"]
                    and "sentinel" in sw.holders(access, "reviewer")
                )
            )
            yield World(sw, km, se, ScribeStandIn(sw), clock, box, executor, access, review)
            assert sw.errors == []
        finally:
            km.request_stop()
            se.request_stop()
            await asyncio.gather(*runs, return_exceptions=True)
            await km_ops.aclose()
            await se_ops.aclose()
            km.store.close()
            se.store.close()


def lease_block(proposal: dict[str, Any]) -> str:
    return f"```lease\n{json.dumps(proposal, indent=2)}\n```"
