"""The lease store enforces the SPEC state graph and logs every change."""

from __future__ import annotations

import itertools
import sqlite3
from pathlib import Path

import pytest

from leasedesk.common.schemas import LeaseProposal
from leasedesk.keymaster.store import (
    TRANSITIONS,
    IllegalTransition,
    LeaseState,
    LeaseStore,
    StaleState,
)

from .conftest import ARNAB, PRIYA, Clock

S = LeaseState


def make(store: LeaseStore, proposal: LeaseProposal) -> str:
    return store.create_lease(
        proposal, submitted_by=ARNAB, submitted_by_name="arnab.", room_id="r1"
    ).id


def force_state(store: LeaseStore, lease_id: str, state: LeaseState) -> None:
    store._conn.execute("UPDATE leases SET state = ? WHERE id = ?", (state.value, lease_id))


def test_create_starts_awaiting_confirm(store: LeaseStore, proposal: LeaseProposal) -> None:
    lease = store.create_lease(
        proposal, submitted_by=ARNAB, submitted_by_name="arnab.", room_id="r1"
    )
    assert lease.id == "LS-0001"
    assert lease.state is S.AWAITING_CONFIRM
    kinds = [(e.from_state, e.to_state) for e in store.events(lease.id)]
    assert kinds == [(None, S.DRAFTED), (S.DRAFTED, S.AWAITING_CONFIRM)]


ILLEGAL = [(a, b) for a, b in itertools.product(list(S), list(S)) if b not in TRANSITIONS[a]]


@pytest.mark.parametrize(("current", "target"), ILLEGAL)
def test_every_illegal_transition_rejected(
    store: LeaseStore, proposal: LeaseProposal, current: LeaseState, target: LeaseState
) -> None:
    lease_id = make(store, proposal)
    force_state(store, lease_id, current)
    with pytest.raises(IllegalTransition):
        store.transition(lease_id, [current], target, "test")
    lease = store.get(lease_id)
    assert lease is not None and lease.state is current


def test_spec_path_is_legal(store: LeaseStore, proposal: LeaseProposal) -> None:
    lease_id = make(store, proposal)
    for a, b in [
        (S.AWAITING_CONFIRM, S.UNDER_REVIEW),
        (S.UNDER_REVIEW, S.ESCALATED),
        (S.ESCALATED, S.ACTIVE),
        (S.ACTIVE, S.EXPIRED),
    ]:
        assert store.transition(lease_id, [a], b, "ok").state is b
    assert TRANSITIONS[S.EXPIRED] == frozenset()


def test_stale_from_state_rejected(store: LeaseStore, proposal: LeaseProposal) -> None:
    lease_id = make(store, proposal)
    with pytest.raises(StaleState):
        store.transition(lease_id, [S.UNDER_REVIEW], S.CANCELLED, "x")


def test_transition_writes_event_and_fields(
    store: LeaseStore, proposal: LeaseProposal, clock: Clock
) -> None:
    lease_id = make(store, proposal)
    clock.advance(minutes=1)
    lease = store.transition(
        lease_id,
        [S.AWAITING_CONFIRM],
        S.UNDER_REVIEW,
        "confirmed",
        actor=ARNAB,
        requester=ARNAB,
        iam_user="lease-demo-dev",
    )
    assert lease.requester == ARNAB and lease.iam_user == "lease-demo-dev"
    last = store.events(lease_id)[-1]
    assert (last.from_state, last.to_state, last.actor, last.reason) == (
        S.AWAITING_CONFIRM,
        S.UNDER_REVIEW,
        ARNAB,
        "confirmed",
    )
    assert last.at == clock.now


def test_update_cannot_change_state(store: LeaseStore, proposal: LeaseProposal) -> None:
    lease_id = make(store, proposal)
    with pytest.raises(ValueError, match="transition"):
        store.update(lease_id, state=S.ACTIVE)


def test_duplicate_approval_returns_false(store: LeaseStore, proposal: LeaseProposal) -> None:
    lease_id = make(store, proposal)
    kw = {"round": 1, "approver_name": "Priya", "decision": "approve", "room_id": "r1"}
    assert store.add_approval(lease_id, approver_id=PRIYA, message_id="m1", **kw) is True
    assert store.add_approval(lease_id, approver_id=PRIYA, message_id="m2", **kw) is False
    assert len(store.approvals(lease_id, 1)) == 1
    assert store.add_approval(lease_id, approver_id=PRIYA, message_id="m3", **{**kw, "round": 2})


def test_events_are_append_only(store: LeaseStore, proposal: LeaseProposal) -> None:
    lease_id = make(store, proposal)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        store._conn.execute("UPDATE events SET reason = 'x' WHERE lease_id = ?", (lease_id,))
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        store._conn.execute("DELETE FROM events")


def test_state_survives_reopen(tmp_path: Path, proposal: LeaseProposal, clock: Clock) -> None:
    db = tmp_path / "k.db"
    s1 = LeaseStore(db, clock=clock)
    lease_id = make(s1, proposal)
    s1.transition(lease_id, [S.AWAITING_CONFIRM], S.UNDER_REVIEW, "x")
    s1.close()
    s2 = LeaseStore(db, clock=clock)
    lease = s2.get(lease_id)
    assert lease is not None and lease.state is S.UNDER_REVIEW
    assert lease.proposal == proposal
    assert [x.id for x in s2.in_states(S.UNDER_REVIEW)] == [lease_id]


def test_wal_mode_on(store: LeaseStore) -> None:
    assert store._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_flags(store: LeaseStore) -> None:
    assert store.get_flag("frozen") is False
    store.set_flag("frozen", True)
    assert store.get_flag("frozen") is True
