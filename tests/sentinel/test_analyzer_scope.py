"""scope: wildcards, allowed prefixes, other accounts, crown jewels, ListBucket prefixes."""

from __future__ import annotations

from leasedesk.sentinel.analyzers import scope
from leasedesk.sentinel.catalog import Catalog
from leasedesk.sentinel.context import ReviewContext

from .conftest import BUCKET, DECOY, LOGS, make_ctx, rules, stmt, view


def test_clean_proposal(ctx: ReviewContext) -> None:
    assert scope.analyze(view(), ctx) == []


def test_star_resource_is_high_not_outside_prefixes(ctx: ReviewContext) -> None:
    found = scope.analyze(view(stmt("S", ["s3:GetObject"], ["*"])), ctx)
    # * also reaches the crown-jewel bucket, so both HIGH findings are right
    assert rules(found) == {"resource-star", "crown-jewel-read"}
    assert all(f.severity == "HIGH" and f.fix for f in found)


def test_outside_prefixes_blocks(ctx: ReviewContext) -> None:
    found = scope.analyze(view(stmt("S", ["s3:GetObject"], ["arn:aws:s3:::other-team/a"])), ctx)
    assert rules(found) == {"resource-outside-prefixes"}
    assert found[0].severity == "BLOCK"


def test_other_account_blocks(ctx: ReviewContext) -> None:
    other = "arn:aws:s3:us-east-1:999999999999:accesspoint/leasedesk-demo-billing"
    found = scope.analyze(view(stmt("S", ["s3:GetObject"], [other])), ctx)
    assert "resource-other-account" in rules(found)
    own_logs = scope.analyze(view(stmt("L", ["logs:FilterLogEvents"], [LOGS])), ctx)
    assert own_logs == []


def test_crown_jewel_read_high_write_block(ctx: ReviewContext) -> None:
    read = scope.analyze(view(stmt("R", ["s3:GetObject"], [f"{DECOY}/*"])), ctx)
    assert rules(read) == {"crown-jewel-read"} and read[0].severity == "HIGH"
    write = scope.analyze(view(stmt("W", ["s3:PutObject"], [f"{DECOY}/*"])), ctx)
    assert rules(write) == {"crown-jewel-write"} and write[0].severity == "BLOCK"


def test_bucket_wildcard_covering_the_decoy_is_caught(ctx: ReviewContext) -> None:
    found = scope.analyze(view(stmt("R", ["s3:GetObject"], ["arn:aws:s3:::leasedesk-demo-*"])), ctx)
    assert "crown-jewel-read" in rules(found)


def test_forbidden_action_blocks(ctx: ReviewContext) -> None:
    found = scope.analyze(view(stmt("P", ["iam:PassRole"], [f"{BUCKET}/a"])), ctx)
    assert rules(found) == {"forbidden-action"}


def test_list_bucket_without_prefix_is_low_with_a_derived_fix(ctx: ReviewContext) -> None:
    p = view(
        stmt("Read", ["s3:GetObject"], [f"{BUCKET}/exports/2026-10/*"]),
        stmt("List", ["s3:ListBucket"], [BUCKET]),
    )
    found = scope.analyze(p, ctx)
    assert rules(found) == {"list-bucket-no-prefix"}
    assert found[0].severity == "LOW"
    assert found[0].fix is not None and "exports/2026-10/*" in found[0].fix


def test_crown_jewel_listed_by_exact_bucket_arn_covers_its_objects() -> None:
    exact = {
        "version": 1,
        "resources": [{"arn": DECOY, "owner": "security", "sensitivity": "crown-jewel"}],
    }
    c = make_ctx(catalog=Catalog.model_validate(exact))
    write = scope.analyze(view(stmt("W", ["s3:PutObject"], [f"{DECOY}/*"])), c)
    assert "crown-jewel-write" in rules(write)
    read = scope.analyze(view(stmt("R", ["s3:GetObject"], [f"{DECOY}/reports/*"])), c)
    assert "crown-jewel-read" in rules(read)
