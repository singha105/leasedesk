"""access_level: one finding per statement at its highest AWS access level, plus wildcards."""

from __future__ import annotations

from leasedesk.sentinel.actions import access_level, expand
from leasedesk.sentinel.analyzers import access_level as al
from leasedesk.sentinel.context import ReviewContext

from .conftest import BUCKET, rules, stmt, view


def test_levels_from_parliament_data() -> None:
    assert access_level("s3:GetObject") == "Read"
    assert access_level("S3:LISTBUCKET") == "List"
    assert access_level("iam:CreatePolicyVersion") == "Permissions management"
    assert access_level("s3:PutBucketTagging") == "Tagging"
    assert access_level("s3:GetObjects") is None


def test_expand_handles_wildcards() -> None:
    assert "s3:getobject" in expand("s3:Get*")
    assert len(expand("*")) > 10000
    assert expand("s3:GetObjects") == set()


def test_read_proposal_is_two_lows(ctx: ReviewContext) -> None:
    found = al.analyze(view(), ctx)
    assert [(f.rule, f.severity, f.statement_sid) for f in found] == [
        ("access-read-list", "LOW", "ReadInvoiceExports"),
        ("access-read-list", "LOW", "ListInvoiceExports"),
    ]


def test_write_is_medium_with_a_read_alternative(ctx: ReviewContext) -> None:
    found = al.analyze(view(stmt("Put", ["s3:PutObject"], [f"{BUCKET}/a/*"])), ctx)
    assert rules(found) == {"access-write"}
    assert found[0].fix is not None and "s3:GetObject" in found[0].fix


def test_permissions_management_is_high(ctx: ReviewContext) -> None:
    found = al.analyze(view(stmt("P", ["s3:PutBucketPolicy"], [BUCKET])), ctx)
    assert rules(found) == {"access-permissions-management"}
    assert found[0].severity == "HIGH" and found[0].fix


def test_service_wildcard_is_high_and_names_narrower_actions(ctx: ReviewContext) -> None:
    found = al.analyze(view(stmt("All", ["s3:*"], [BUCKET, f"{BUCKET}/*"])), ctx)
    w = [f for f in found if f.rule == "wildcard-service"]
    assert w and w[0].severity == "HIGH"
    assert w[0].fix is not None and "s3:GetObject" in w[0].fix and "s3:ListBucket" in w[0].fix


def test_full_wildcard_is_high(ctx: ReviewContext) -> None:
    found = al.analyze(view(stmt("All", ["*"], [BUCKET])), ctx)
    assert "wildcard-all" in rules(found)
