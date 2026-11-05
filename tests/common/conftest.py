from __future__ import annotations

from typing import Any

import pytest

BUCKET = "arn:aws:s3:::leasedesk-demo-billing-abc123"


def proposal_dict(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "kind": "lease_proposal",
        "version": 1,
        "requester_hint": "Arnab",
        "summary": "Read October invoice exports to debug the billing report",
        "justification": "Debugging ticket BILL-142; report totals are off",
        "duration_minutes": 120,
        "statements": [
            {
                "sid": "ReadInvoiceExports",
                "actions": ["s3:GetObject"],
                "resources": [f"{BUCKET}/exports/2026-10/*"],
            },
            {
                "sid": "ListInvoiceExports",
                "actions": ["s3:ListBucket"],
                "resources": [BUCKET],
                "conditions": {"StringLike": {"s3:prefix": ["exports/2026-10/*"]}},
            },
        ],
    }
    base.update(over)
    return base


@pytest.fixture
def proposal() -> dict[str, Any]:
    return proposal_dict()
