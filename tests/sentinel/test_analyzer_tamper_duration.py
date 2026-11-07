"""tamper_and_exposure and duration."""

from __future__ import annotations

import pytest

from leasedesk.sentinel.analyzers import duration, tamper
from leasedesk.sentinel.context import ReviewContext

from .conftest import BUCKET, rules, stmt, view


@pytest.mark.parametrize(
    "action",
    [
        "cloudtrail:StopLogging",
        "cloudtrail:DeleteTrail",
        "config:StopConfigurationRecorder",
        "guardduty:DeleteDetector",
        "s3:PutBucketPolicy",
        "s3:PutBucketAcl",
        "s3:PutObjectAcl",
        "s3:DeletePublicAccessBlock",
        "s3:PutBucketPublicAccessBlock",
        "kms:ScheduleKeyDeletion",
        "S3:PUTBUCKETPOLICY",
    ],
)
def test_named_tamper_actions_block(ctx: ReviewContext, action: str) -> None:
    found = tamper.analyze(view(stmt("T", [action], [BUCKET])), ctx)
    assert rules(found) == {"tamper-exposure"} and found[0].severity == "BLOCK"


def test_wildcard_covering_tamper_is_high(ctx: ReviewContext) -> None:
    found = tamper.analyze(view(stmt("T", ["s3:Put*"], [BUCKET])), ctx)
    assert rules(found) == {"wildcard-covers-tamper"}
    assert found[0].severity == "HIGH" and "s3:PutBucketPolicy" in found[0].detail
    assert tamper.analyze(view(stmt("T", ["s3:Get*"], [BUCKET])), ctx) == []


@pytest.mark.parametrize(
    ("minutes", "actions", "want"),
    [
        (60, ["s3:GetObject"], set()),
        (120, ["s3:GetObject"], set()),
        (121, ["s3:GetObject"], {"duration-long"}),
        (239, ["s3:GetObject"], {"duration-long"}),
        (241, ["s3:GetObject"], {"duration-over-max"}),
        (60, ["s3:PutObject"], set()),
        (61, ["s3:PutObject"], {"write-long"}),
        (180, ["s3:PutObject"], {"duration-long", "write-long"}),
    ],
)
def test_duration_rules(
    ctx: ReviewContext, minutes: int, actions: list[str], want: set[str]
) -> None:
    p = view(stmt("S", actions, [f"{BUCKET}/a/*"]), duration_minutes=minutes)
    found = duration.analyze(p, ctx)
    assert rules(found) == want
    assert all(f.fix for f in found)
