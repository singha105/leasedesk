"""structure: Allow only; no NotAction, NotResource or Principal; no aws:CurrentTime; ARNs."""

from __future__ import annotations

import pytest

from leasedesk.sentinel.analyzers import structure
from leasedesk.sentinel.context import ReviewContext

from .conftest import BUCKET, rules, stmt, view


def test_clean_proposal_has_no_findings(ctx: ReviewContext) -> None:
    assert structure.analyze(view(), ctx) == []


@pytest.mark.parametrize(
    ("extra", "rule"),
    [
        ({"NotAction": ["iam:*"]}, "struct-not-action"),
        ({"not_actions": ["iam:*"]}, "struct-not-action"),
        ({"NotResource": ["*"]}, "struct-not-resource"),
        ({"Principal": {"AWS": "*"}}, "struct-principal"),
        ({"Effect": "Deny"}, "struct-not-allow"),
        ({"Effect": "Allow"}, "struct-unknown-key"),
        ({"approve": True}, "struct-unknown-key"),
        (
            {"conditions": {"DateLessThan": {"aws:CurrentTime": "2030-01-01T00:00:00Z"}}},
            "struct-current-time",
        ),
    ],
)
def test_forbidden_shapes_block(ctx: ReviewContext, extra: dict[str, object], rule: str) -> None:
    found = structure.analyze(view(stmt("S1", ["s3:GetObject"], [f"{BUCKET}/a/*"], **extra)), ctx)
    assert rule in rules(found)
    assert all(f.severity == "BLOCK" and f.statement_sid == "S1" for f in found)


@pytest.mark.parametrize(
    "arn", ["arn:aws:s3", "arn:aws-cn:s3:::b", "s3:::bucket", "arn:aws:s3:::", "arn:aws::::x"]
)
def test_malformed_arn_blocks(ctx: ReviewContext, arn: str) -> None:
    found = structure.analyze(view(stmt("S1", ["s3:GetObject"], [arn])), ctx)
    assert rules(found) == {"struct-bad-arn"}


def test_star_resource_is_not_a_structure_problem(ctx: ReviewContext) -> None:
    assert structure.analyze(view(stmt("S1", ["s3:GetObject"], ["*"])), ctx) == []


def test_schema_problems_block(ctx: ReviewContext) -> None:
    bad = view(stmt("S1", ["*"], [BUCKET]), kind="lease_grant", extra_field=1)
    assert "struct-schema" in rules(structure.analyze(bad, ctx))
