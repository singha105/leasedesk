"""lint: parliament on the rendered preview of each statement."""

from __future__ import annotations

from leasedesk.sentinel.analyzers import lint
from leasedesk.sentinel.context import ReviewContext

from .conftest import BUCKET, LOGS, rules, stmt, view


def test_clean_s3_and_logs_proposals_pass(ctx: ReviewContext) -> None:
    assert lint.analyze(view(), ctx) == []
    logs = view(stmt("Logs", ["logs:FilterLogEvents"], [f"{LOGS}:*"]), duration_minutes=30)
    assert lint.analyze(logs, ctx) == []


def test_misspelled_action_blocks_with_parliaments_message(ctx: ReviewContext) -> None:
    found = lint.analyze(view(stmt("Read", ["s3:GetObjects"], [f"{BUCKET}/a/*"])), ctx)
    assert rules(found) == {"lint-unknown-action"}
    f = found[0]
    assert f.severity == "BLOCK" and f.statement_sid == "Read"
    assert "Unknown action s3:GetObjects" in f.detail
    assert f.fix is not None and "s3:GetObject" in f.fix


def test_unknown_service_blocks(ctx: ReviewContext) -> None:
    found = lint.analyze(view(stmt("X", ["foo:Bar"], [f"{BUCKET}/a/*"])), ctx)
    assert rules(found) == {"lint-unknown-prefix"}


def test_resource_mismatch_is_medium_with_parliaments_format(ctx: ReviewContext) -> None:
    found = lint.analyze(view(stmt("List", ["s3:ListBucket"], [f"{BUCKET}/exports/*"])), ctx)
    assert rules(found) == {"lint-resource-mismatch"}
    assert found[0].severity == "MEDIUM"
    assert found[0].fix is not None and "arn:*:s3:::*" in found[0].fix


def test_star_resource_left_to_scope(ctx: ReviewContext) -> None:
    assert lint.analyze(view(stmt("S", ["s3:GetObject"], ["*"])), ctx) == []


def test_wildcards_dont_raise_resource_mismatch_noise(ctx: ReviewContext) -> None:
    found = lint.analyze(view(stmt("All", ["s3:*"], [BUCKET, f"{BUCKET}/*"])), ctx)
    assert "lint-resource-mismatch" not in rules(found)


def test_one_parliament_remark_is_one_finding_without_its_title_twice(ctx: ReviewContext) -> None:
    # parliament reports RESOURCE_POLICY_PRIVILEGE_ESCALATION once per object action the
    # statement lacks (three here), each detail starting with the title again.
    found = lint.analyze(view(stmt("Pol", ["s3:PutBucketPolicy"], [BUCKET])), ctx)
    others = [f for f in found if f.rule == "lint-other"]
    assert len(others) == 1
    detail = others[0].detail
    assert detail.count("Possible resource policy privilege escalation") == 1
    assert detail.endswith("(+2 similar)")


def test_condition_mismatch_is_one_finding_per_statement(ctx: ReviewContext) -> None:
    # s3:prefix doesn't apply to most of the 160-odd actions s3:* covers; one finding is enough.
    cond = {"StringLike": {"s3:prefix": ["exports/*"]}}
    found = lint.analyze(view(stmt("All", ["s3:*"], [BUCKET, f"{BUCKET}/*"], conditions=cond)), ctx)
    assert [f.rule for f in found].count("lint-condition-mismatch") == 1
