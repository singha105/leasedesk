"""render_policy: Allow-only, both aws:CurrentTime conditions, the proposal's own kept, size cap."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from leasedesk.common.render import (
    PolicyTooLarge,
    check_user_total,
    iam_size,
    policy_name,
    render_policy,
    serialize,
)
from leasedesk.common.schemas import LeaseProposal

from ..keymaster.conftest import T0
from .conftest import proposal_dict

END = T0 + timedelta(minutes=120)


@pytest.fixture
def proposal() -> LeaseProposal:
    return LeaseProposal.model_validate(proposal_dict())


def test_policy_name() -> None:
    assert policy_name("LS-0007") == "leasedesk-LS-0007"


def test_both_time_conditions_on_every_statement(proposal: LeaseProposal) -> None:
    doc = render_policy(proposal, "LS-0007", T0, END)
    assert doc["Version"] == "2012-10-17"
    assert len(doc["Statement"]) == 2
    for st in doc["Statement"]:
        assert st["Effect"] == "Allow"
        assert st["Condition"]["DateGreaterThan"]["aws:CurrentTime"] == "2026-11-04T18:59:00Z"
        assert st["Condition"]["DateLessThan"]["aws:CurrentTime"] == "2026-11-04T21:00:00Z"


def test_existing_conditions_kept() -> None:
    p = proposal_dict()
    p["statements"][1]["conditions"] = {
        "StringLike": {"s3:prefix": ["exports/2026-10/*"]},
        "DateGreaterThan": {"s3:ExistingObjectTag/created": "2026-01-01T00:00:00Z"},
    }
    doc = render_policy(LeaseProposal.model_validate(p), "LS-0007", T0, END)
    cond = doc["Statement"][1]["Condition"]
    assert cond["StringLike"] == {"s3:prefix": ["exports/2026-10/*"]}
    assert cond["DateGreaterThan"] == {
        "s3:ExistingObjectTag/created": "2026-01-01T00:00:00Z",
        "aws:CurrentTime": "2026-11-04T18:59:00Z",
    }
    assert "Condition" in doc["Statement"][0]
    assert p["statements"][1]["conditions"]["DateGreaterThan"] == {
        "s3:ExistingObjectTag/created": "2026-01-01T00:00:00Z"
    }


def test_sid_prefixed_and_alnum(proposal: LeaseProposal) -> None:
    sids = [s["Sid"] for s in render_policy(proposal, "LS-0007", T0, END)["Statement"]]
    assert sids == ["LS0007ReadInvoiceExports", "LS0007ListInvoiceExports"]
    assert all(s.isalnum() for s in sids)


def test_serialize_is_compact(proposal: LeaseProposal) -> None:
    text = serialize(render_policy(proposal, "LS-0007", T0, END))
    assert " " not in text and "\n" not in text
    assert json.loads(text)["Statement"][0]["Action"] == ["s3:GetObject"]


def test_whitespace_not_counted() -> None:
    assert iam_size('{"a": "b c"}\n') == len('{"a":"bc"}')


def test_size_cap_counts_other_policies() -> None:
    new = "x" * 600
    with pytest.raises(PolicyTooLarge) as exc:
        check_user_total(new, ["y" * 1500], limit=2048)
    assert (exc.value.total, exc.value.limit) == (2100, 2048)
    check_user_total(new, ["y" * 1448], limit=2048)


def test_ten_big_statements_are_too_large() -> None:
    p = proposal_dict()
    p["statements"] = [
        {
            "sid": f"S{i}",
            "actions": [f"s3:GetObjectVersion{i}" for _ in range(1)] + ["s3:GetObject"],
            "resources": [f"arn:aws:s3:::leasedesk-demo-billing-abc123/exports/{i}/{'k' * 60}/*"],
        }
        for i in range(10)
    ]
    doc = serialize(render_policy(LeaseProposal.model_validate(p), "LS-0007", T0, END))
    with pytest.raises(PolicyTooLarge):
        check_user_total(doc, [])
