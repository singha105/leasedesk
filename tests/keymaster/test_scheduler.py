"""Scheduler and reconciler with an injectable clock: no real sleeps."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from leasedesk.common.schemas import AccessPolicy, LeaseProposal
from leasedesk.keymaster.reconciler import Reconciler
from leasedesk.keymaster.render import policy_name, render_policy, serialize
from leasedesk.keymaster.scheduler import Scheduler
from leasedesk.keymaster.store import Lease, LeaseState, LeaseStore

from .conftest import ARNAB, Clock
from .fakes import FakeExecutor, FakeNotifier

S = LeaseState
USER = "lease-demo-dev"
TZ = "America/New_York"


def activate(
    store: LeaseStore, ex: FakeExecutor, proposal: LeaseProposal, clock: Clock, minutes: int = 3
) -> Lease:
    lease = store.create_lease(proposal, submitted_by=ARNAB, submitted_by_name="a", room_id="r")
    store.transition(
        lease.id,
        [S.AWAITING_CONFIRM],
        S.UNDER_REVIEW,
        "c",
        requester=ARNAB,
        iam_user=USER,
        review_requested_at=clock(),
    )
    store.transition(
        lease.id,
        [S.UNDER_REVIEW],
        S.AWAITING_APPROVAL,
        "v",
        risk="LOW",
        approval_requested_at=clock(),
    )
    end = clock() + timedelta(minutes=minutes)
    doc = serialize(render_policy(proposal, lease.id, clock(), end))
    ex.policies.setdefault(USER, {})[policy_name(lease.id)] = doc
    return store.transition(
        lease.id,
        [S.AWAITING_APPROVAL],
        S.ACTIVE,
        "approved",
        final_policy_json=doc,
        starts_at=clock(),
        expires_at=end,
    )


@pytest.fixture
def parts(
    store: LeaseStore, policy: AccessPolicy, clock: Clock
) -> tuple[Scheduler, FakeExecutor, FakeNotifier]:
    ex, note = FakeExecutor(), FakeNotifier()
    return Scheduler(store, ex, note, lambda: policy, clock, TZ), ex, note


async def test_expiry_revokes_with_injected_clock(
    parts: tuple[Scheduler, FakeExecutor, FakeNotifier],
    store: LeaseStore,
    proposal: LeaseProposal,
    clock: Clock,
) -> None:
    sched, ex, note = parts
    lease = activate(store, ex, proposal, clock)
    clock.advance(minutes=2, seconds=59)
    assert await sched.tick() == []
    clock.advance(seconds=1)
    assert await sched.tick() == [lease.id]
    assert store.require(lease.id).state is S.EXPIRED
    assert ex.policies[USER] == {}
    assert any("EXPIRED at" in t for t in note.texts())
    assert note.ledger_rows == [(lease.id, "EXPIRED")]
    assert await sched.tick() == []


async def test_confirm_timeout_cancels(
    parts: tuple[Scheduler, FakeExecutor, FakeNotifier],
    store: LeaseStore,
    proposal: LeaseProposal,
    clock: Clock,
) -> None:
    sched, _, note = parts
    lease = store.create_lease(proposal, submitted_by=ARNAB, submitted_by_name="a", room_id="r")
    clock.advance(minutes=14)
    await sched.tick()
    assert store.require(lease.id).state is S.AWAITING_CONFIRM
    clock.advance(minutes=1)
    await sched.tick()
    assert store.require(lease.id).state is S.CANCELLED
    assert "CANCELLED" in note.texts()[-1]


async def test_approval_timeout_cancels(
    parts: tuple[Scheduler, FakeExecutor, FakeNotifier],
    store: LeaseStore,
    proposal: LeaseProposal,
    clock: Clock,
) -> None:
    sched, _, _ = parts
    lease = store.create_lease(proposal, submitted_by=ARNAB, submitted_by_name="a", room_id="r")
    store.transition(lease.id, [S.AWAITING_CONFIRM], S.UNDER_REVIEW, "c")
    store.transition(
        lease.id, [S.UNDER_REVIEW], S.ESCALATED, "v", risk="HIGH", approval_requested_at=clock()
    )
    clock.advance(minutes=30)
    await sched.tick()
    assert store.require(lease.id).state is S.CANCELLED


async def test_single_verdict_reminder(
    parts: tuple[Scheduler, FakeExecutor, FakeNotifier],
    store: LeaseStore,
    proposal: LeaseProposal,
    clock: Clock,
) -> None:
    sched, _, note = parts
    lease = store.create_lease(proposal, submitted_by=ARNAB, submitted_by_name="a", room_id="r")
    store.transition(
        lease.id, [S.AWAITING_CONFIRM], S.UNDER_REVIEW, "c", review_requested_at=clock()
    )
    clock.advance(seconds=59)
    await sched.tick()
    assert note.texts() == []
    clock.advance(seconds=1)
    await sched.tick()
    clock.advance(minutes=5)
    await sched.tick()
    reminders = [t for t in note.texts() if "waiting for a review verdict" in t]
    assert len(reminders) == 1
    assert store.require(lease.id).state is S.UNDER_REVIEW


async def test_restart_then_expire(
    tmp_path: Path, policy: AccessPolicy, proposal: LeaseProposal, clock: Clock
) -> None:
    db = tmp_path / "k.db"
    ex = FakeExecutor()
    first = LeaseStore(db, clock=clock)
    lease = activate(first, ex, proposal, clock)
    first.close()
    clock.advance(minutes=4)
    second = LeaseStore(db, clock=clock)
    note = FakeNotifier()
    await Scheduler(second, ex, note, lambda: policy, clock, TZ).tick()
    assert second.require(lease.id).state is S.EXPIRED
    assert ex.policies[USER] == {}


async def test_orphan_removed(store: LeaseStore, clock: Clock) -> None:
    ex, note = FakeExecutor(), FakeNotifier()
    ex.policies[USER] = {"leasedesk-LS-0042": "{}", "hand-made": "{}"}
    report = await Reconciler(store, ex, note, lambda: {USER}, clock).reconcile()
    assert report.removed == [(USER, "leasedesk-LS-0042")]
    assert ex.policies[USER] == {"hand-made": "{}"}
    assert note.texts()[0].startswith("reconciled: removed orphaned policy leasedesk-LS-0042")


async def test_expired_policy_removed(
    store: LeaseStore, proposal: LeaseProposal, clock: Clock
) -> None:
    ex, note = FakeExecutor(), FakeNotifier()
    lease = activate(store, ex, proposal, clock)
    clock.advance(minutes=10)
    report = await Reconciler(store, ex, note, lambda: {USER}, clock).reconcile()
    assert report.removed == [(USER, policy_name(lease.id))]


async def test_policy_of_finished_lease_removed(
    store: LeaseStore, proposal: LeaseProposal, clock: Clock
) -> None:
    ex, note = FakeExecutor(), FakeNotifier()
    lease = activate(store, ex, proposal, clock)
    store.transition(lease.id, [S.ACTIVE], S.REVOKED, "revoked, but AWS delete was lost")
    ex.policies[USER][policy_name(lease.id)] = "{}"
    report = await Reconciler(store, ex, note, lambda: {USER}, clock).reconcile()
    assert report.removed == [(USER, policy_name(lease.id))]


async def test_vanished_policy_fails_lease(
    store: LeaseStore, proposal: LeaseProposal, clock: Clock
) -> None:
    ex, note = FakeExecutor(), FakeNotifier()
    lease = activate(store, ex, proposal, clock)
    ex.policies[USER].clear()
    report = await Reconciler(store, ex, note, lambda: {USER}, clock).reconcile()
    assert report.failed == [lease.id]
    assert store.require(lease.id).state is S.FAILED
    assert any(t.startswith("ALERT") for t in note.texts())
    assert note.ledger_rows == [(lease.id, "FAILED")]


async def test_live_lease_left_alone(
    store: LeaseStore, proposal: LeaseProposal, clock: Clock
) -> None:
    ex, note = FakeExecutor(), FakeNotifier()
    activate(store, ex, proposal, clock)
    report = await Reconciler(store, ex, note, lambda: {USER}, clock).reconcile()
    assert report.removed == [] and report.failed == [] and note.texts() == []


async def test_reconciler_waits_for_an_activation_in_progress(
    store: LeaseStore, proposal: LeaseProposal, clock: Clock
) -> None:
    import asyncio

    ex, note = FakeExecutor(), FakeNotifier()
    lease = store.create_lease(proposal, submitted_by=ARNAB, submitted_by_name="a", room_id="r")
    store.transition(lease.id, [S.AWAITING_CONFIRM], S.UNDER_REVIEW, "c", iam_user=USER)
    store.transition(lease.id, [S.UNDER_REVIEW], S.AWAITING_APPROVAL, "v", risk="LOW")
    lock = asyncio.Lock()
    rec = Reconciler(store, ex, note, lambda: {USER}, clock, lock=lambda _id: lock)
    async with lock:
        ex.policies[USER] = {policy_name(lease.id): "{}"}
        task = asyncio.create_task(rec.reconcile())
        await asyncio.sleep(0.01)
        store.transition(
            lease.id,
            [S.AWAITING_APPROVAL],
            S.ACTIVE,
            "approved",
            starts_at=clock(),
            expires_at=clock() + timedelta(minutes=5),
        )
    report = await task
    assert report.removed == []
    assert policy_name(lease.id) in ex.policies[USER]
