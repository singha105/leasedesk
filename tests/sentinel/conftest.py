"""Shared fixtures for sentinel: a review context and proposal builders."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from leasedesk.common.schemas import AccessPolicy
from leasedesk.sentinel.catalog import Catalog
from leasedesk.sentinel.context import ReviewContext
from leasedesk.sentinel.model import ProposalView

from ..common.conftest import BUCKET, proposal_dict
from ..keymaster.conftest import clock, policy_dict  # noqa: F401 - clock is a fixture

ACCOUNT = "123456789012"
DECOY = "arn:aws:s3:::leasedesk-demo-prod-secrets-abc123"
LOGS = f"arn:aws:logs:us-east-1:{ACCOUNT}:log-group:/leasedesk/app"
CATALOG = {
    "version": 1,
    "resources": [
        {
            "arn": "arn:aws:s3:::leasedesk-demo-billing-*",
            "owner": "billing team",
            "sensitivity": "internal",
        },
        {
            "arn": "arn:aws:s3:::leasedesk-demo-prod-secrets-*",
            "owner": "security",
            "sensitivity": "crown-jewel",
            "sensitive_actions": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
        },
    ],
}


def make_ctx(**over: Any) -> ReviewContext:
    base: dict[str, Any] = {
        "policy": AccessPolicy.model_validate(policy_dict()),
        "catalog": Catalog.model_validate(CATALOG),
        "account_id": ACCOUNT,
        "aws_mode": "mock",
        "paid_checks": False,
    }
    base.update(over)
    return ReviewContext(**base)


@pytest.fixture
def ctx() -> ReviewContext:
    return make_ctx()


def stmt(sid: str, actions: list[str], resources: list[str], **extra: Any) -> dict[str, Any]:
    return {"sid": sid, "actions": actions, "resources": resources, **extra}


def raw(*statements: dict[str, Any], **over: Any) -> dict[str, Any]:
    base = proposal_dict(**over)
    if statements:
        base["statements"] = [copy.deepcopy(s) for s in statements]
    return base


def view(*statements: dict[str, Any], **over: Any) -> ProposalView:
    v, problems = ProposalView.parse(raw(*statements, **over))
    assert v is not None, problems
    return v


def rules(findings: list[Any]) -> set[str]:
    return {f.rule for f in findings}


__all__ = ["BUCKET", "DECOY", "LOGS", "make_ctx", "raw", "rules", "stmt", "view"]
