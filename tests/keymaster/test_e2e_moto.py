"""End to end on moto: apply, simulate ALLOWED, advance the clock, expire, simulate DENIED."""

from __future__ import annotations

from datetime import timedelta

import boto3
import pytest
from moto import mock_aws

from leasedesk.common.schemas import AccessPolicy, LeaseProposal
from leasedesk.keymaster.executor import BotoIamExecutor
from leasedesk.keymaster.mockaws import seed
from leasedesk.keymaster.render import policy_name, render_policy, serialize
from leasedesk.keymaster.scheduler import Scheduler
from leasedesk.keymaster.store import LeaseState, LeaseStore

from ..common.conftest import proposal_dict
from .conftest import ARNAB, Clock
from .fakes import FakeNotifier

pytestmark = pytest.mark.e2e
S = LeaseState
USER = "lease-demo-dev"


async def test_e2e_apply_simulate_expire(
    store: LeaseStore, policy: AccessPolicy, clock: Clock
) -> None:
    with mock_aws():
        session = boto3.Session(
            aws_access_key_id="testing", aws_secret_access_key="testing", region_name="us-east-1"
        )
        box = seed(session)
        ex = BotoIamExecutor(session, boundary_doc=box.boundary_doc, poll_interval=0.01)
        raw = proposal_dict(duration_minutes=3)
        raw["statements"][0]["resources"] = [f"arn:aws:s3:::{box.bucket}/exports/2026-10/*"]
        raw["statements"][1]["resources"] = [f"arn:aws:s3:::{box.bucket}"]
        proposal = LeaseProposal.model_validate(raw)
        lease = store.create_lease(proposal, submitted_by=ARNAB, submitted_by_name="a", room_id="r")
        store.transition(lease.id, [S.AWAITING_CONFIRM], S.UNDER_REVIEW, "c", iam_user=USER)
        store.transition(lease.id, [S.UNDER_REVIEW], S.AWAITING_APPROVAL, "v", risk="LOW")
        end = clock() + timedelta(minutes=3)
        doc = serialize(render_policy(proposal, lease.id, clock(), end))
        await ex.apply(USER, policy_name(lease.id), doc)
        store.transition(
            lease.id,
            [S.AWAITING_APPROVAL],
            S.ACTIVE,
            "approved",
            final_policy_json=doc,
            starts_at=clock(),
            expires_at=end,
        )
        obj = f"arn:aws:s3:::{box.bucket}/exports/2026-10/invoices.csv"
        assert await ex.simulate(USER, "s3:GetObject", obj, clock()) == "ALLOWED"
        clock.advance(minutes=3)
        assert await ex.simulate(USER, "s3:GetObject", obj, clock()) == "DENIED"
        await Scheduler(store, ex, FakeNotifier(), lambda: policy, clock, "UTC").tick()
        assert store.require(lease.id).state is S.EXPIRED
        assert (await ex.list_leasedesk_policies([USER]))[USER] == {}
