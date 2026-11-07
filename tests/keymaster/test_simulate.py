from __future__ import annotations

from datetime import timedelta

from leasedesk.common.render import render_policy, serialize
from leasedesk.common.schemas import LeaseProposal
from leasedesk.keymaster.simulate import default_request, simulate_lease
from leasedesk.keymaster.store import LeaseState, LeaseStore

from .conftest import ARNAB, T0

S = LeaseState


def active_lease(store: LeaseStore, proposal: LeaseProposal) -> str:
    lease = store.create_lease(proposal, submitted_by=ARNAB, submitted_by_name="a", room_id="r")
    end = T0 + timedelta(minutes=3)
    doc = serialize(render_policy(proposal, lease.id, T0, end))
    store.transition(
        lease.id,
        [S.AWAITING_CONFIRM],
        S.UNDER_REVIEW,
        "c",
        requester=ARNAB,
        iam_user="lease-demo-dev",
    )
    store.transition(lease.id, [S.UNDER_REVIEW], S.AWAITING_APPROVAL, "v", risk="LOW")
    store.transition(
        lease.id,
        [S.AWAITING_APPROVAL],
        S.ACTIVE,
        "a",
        final_policy_json=doc,
        starts_at=T0,
        expires_at=end,
    )
    return lease.id


def test_default_request_reads_the_first_object(proposal: LeaseProposal) -> None:
    action, resource, prefix = default_request(proposal)
    assert action == "s3:GetObject"
    assert resource.endswith("/exports/2026-10/invoices.csv")
    assert prefix is None


def test_simulate_allowed_then_denied(store: LeaseStore, proposal: LeaseProposal) -> None:
    lease_id = active_lease(store, proposal)
    assert simulate_lease(store, lease_id, at=T0 + timedelta(minutes=1)).decision == "ALLOWED"
    late = simulate_lease(store, lease_id, at=T0 + timedelta(minutes=3))
    assert late.decision == "DENIED"
    listing = simulate_lease(
        store,
        lease_id,
        at=T0,
        action="s3:ListBucket",
        resource="arn:aws:s3:::leasedesk-demo-billing-abc123",
        prefix="exports/2026-10/",
    )
    assert listing.decision == "ALLOWED"


def test_simulate_unknown_or_ungranted_lease_is_denied(
    store: LeaseStore, proposal: LeaseProposal
) -> None:
    assert simulate_lease(store, "LS-0404", at=T0).decision == "DENIED"
    lease = store.create_lease(proposal, submitted_by=ARNAB, submitted_by_name="a", room_id="r")
    result = simulate_lease(store, lease.id, at=T0)
    assert result.decision == "DENIED" and "no policy" in result.note
