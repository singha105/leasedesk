"""The simple local IAM evaluator used by simulate (time window, boundary, explicit deny)."""

from __future__ import annotations

from datetime import timedelta

from leasedesk.common.render import render_policy
from leasedesk.common.schemas import LeaseProposal
from leasedesk.keymaster.evaluator import evaluate

from .conftest import T0

BILLING = "arn:aws:s3:::leasedesk-demo-billing-abc123"
BOUNDARY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": ["s3:GetObject", "s3:ListBucket"],
            "Resource": ["arn:aws:s3:::leasedesk-demo-*", "arn:aws:s3:::leasedesk-demo-*/*"],
        }
    ],
}


def ctx(at: object, prefix: str | None = "exports/2026-10/") -> dict[str, str]:
    c = {"aws:CurrentTime": at.isoformat()}  # type: ignore[attr-defined]
    if prefix is not None:
        c["s3:prefix"] = prefix
    return c


def test_evaluator_time_window(proposal: LeaseProposal) -> None:
    end = T0 + timedelta(minutes=120)
    doc = render_policy(proposal, "LS-0007", T0, end)
    obj = f"{BILLING}/exports/2026-10/invoices.csv"
    assert evaluate([doc], BOUNDARY, "s3:GetObject", obj, ctx(T0)) == "ALLOWED"
    assert (
        evaluate([doc], BOUNDARY, "s3:GetObject", obj, ctx(T0 - timedelta(seconds=30))) == "ALLOWED"
    )
    assert (
        evaluate([doc], BOUNDARY, "s3:GetObject", obj, ctx(T0 - timedelta(minutes=2))) == "DENIED"
    )
    assert evaluate([doc], BOUNDARY, "s3:GetObject", obj, ctx(end)) == "DENIED"
    assert evaluate([doc], BOUNDARY, "s3:ListBucket", BILLING, ctx(T0)) == "ALLOWED"
    assert evaluate([doc], BOUNDARY, "s3:ListBucket", BILLING, ctx(T0, "exports/")) == "DENIED"
    assert evaluate([doc], BOUNDARY, "s3:ListBucket", BILLING, ctx(T0, None)) == "DENIED"


def test_evaluator_boundary_caps() -> None:
    wide = {"Statement": [{"Effect": "Allow", "Action": "s3:*", "Resource": "*"}]}
    assert evaluate([wide], BOUNDARY, "s3:GetObject", "arn:aws:s3:::other/x", {}) == "DENIED"
    assert evaluate([wide], BOUNDARY, "s3:PutObject", f"{BILLING}/x", {}) == "DENIED"
    assert evaluate([wide], BOUNDARY, "s3:GetObject", f"{BILLING}/x", {}) == "ALLOWED"
    assert evaluate([wide], None, "s3:PutObject", "arn:aws:s3:::other/x", {}) == "ALLOWED"


def test_explicit_deny_wins() -> None:
    allow = {"Statement": [{"Effect": "Allow", "Action": "s3:*", "Resource": "*"}]}
    deny = {"Statement": [{"Effect": "Deny", "Action": "s3:Get*", "Resource": "*"}]}
    assert evaluate([allow, deny], None, "s3:GetObject", f"{BILLING}/x", {}) == "DENIED"


def test_no_policies_is_denied() -> None:
    assert evaluate([], BOUNDARY, "s3:GetObject", f"{BILLING}/x", {}) == "DENIED"


def test_unknown_condition_operator_fails_closed() -> None:
    doc = {
        "Statement": [
            {
                "Effect": "Allow",
                "Action": "s3:*",
                "Resource": "*",
                "Condition": {"IpAddress": {"aws:SourceIp": "10.0.0.0/8"}},
            }
        ]
    }
    assert evaluate([doc], None, "s3:GetObject", f"{BILLING}/x", {}) == "DENIED"
