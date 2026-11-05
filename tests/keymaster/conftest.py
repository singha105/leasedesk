from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from leasedesk.common.schemas import AccessPolicy, LeaseProposal
from leasedesk.keymaster.store import LeaseStore

from ..common.conftest import proposal_dict

ARNAB = "@switch-discord-b1-arnab:localhost"
PRIYA = "@switch-discord-b1-priya:localhost"
SAM = "@switch-discord-b1-sam:localhost"
EVE = "@switch-discord-b1-eve:localhost"
SCRIBE = "@switch-agent-5c41be00:localhost"
KEYMASTER = "@switch-agent-aaaa0000:localhost"
SENTINEL = "@switch-agent-bbbb0000:localhost"
T0 = datetime(2026, 11, 4, 19, 0, tzinfo=UTC)


def policy_dict(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "version": 1,
        "solo_mode": False,
        "max_duration_minutes": 240,
        "default_duration_minutes": 60,
        "approval_timeout_minutes": 30,
        "confirm_timeout_minutes": 15,
        "principals": {ARNAB: "lease-demo-dev", PRIYA: "lease-demo-dev"},
        "approvers": [
            {"id": PRIYA, "name": "Priya", "groups": ["security"]},
            {"id": SAM, "name": "Sam", "groups": ["security"]},
            {"id": ARNAB, "name": "Arnab", "groups": ["eng-lead"]},
            {"id": SCRIBE, "name": "scribe", "groups": ["security"]},
        ],
        "high_risk": {
            "approvals": 2,
            "require_group": "security",
            "approval_room": "security-review",
        },
        "allowed_resource_prefixes": [
            "arn:aws:s3:::leasedesk-demo-",
            "arn:aws:logs:us-east-1:123456789012:log-group:/leasedesk/",
        ],
        "forbidden_actions": [
            "iam:*",
            "sts:AssumeRole",
            "organizations:*",
            "kms:Decrypt",
            "secretsmanager:GetSecretValue",
        ],
    }
    base.update(over)
    return base


@pytest.fixture
def policy() -> AccessPolicy:
    return AccessPolicy.model_validate(policy_dict())


@pytest.fixture
def proposal() -> LeaseProposal:
    return LeaseProposal.model_validate(proposal_dict())


class Clock:
    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(tmp_path: Path, clock: Clock) -> LeaseStore:
    return LeaseStore(tmp_path / "keymaster.db", clock=clock)
