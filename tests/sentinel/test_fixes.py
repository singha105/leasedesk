"""Least-privilege fixes and the corrected lease: mechanical, only ever narrower, re-reviewed."""

from __future__ import annotations

from fnmatch import fnmatchcase
from typing import Any

import pytest

from leasedesk.common.schemas import LeaseProposal
from leasedesk.sentinel.actions import expand
from leasedesk.sentinel.review import review

from .conftest import BUCKET, DECOY, make_ctx, raw, stmt

OBJ = f"{BUCKET}/exports/2026-10/*"


def narrower_than(new: dict[str, Any], old: dict[str, Any]) -> None:
    old_actions = set().union(*(expand(a) for s in old["statements"] for a in s["actions"]))
    old_resources = {r for s in old["statements"] for r in s["resources"]}
    by_sid = {s["sid"]: s for s in old["statements"]}
    for s in new["statements"]:
        assert set().union(*(expand(a) for a in s["actions"])) <= old_actions
        assert all(any(fnmatchcase(r, o) for o in old_resources) for r in s["resources"])
        # every condition the original statement had is still there, unchanged
        src = by_sid.get(s["sid"]) or next(
            o for sid, o in by_sid.items() if s["sid"].startswith(sid)
        )
        for op, keys in (src.get("conditions") or {}).items():
            for key, values in keys.items():
                assert (s.get("conditions") or {}).get(op, {}).get(key) == values, (s, op, key)
    assert new["duration_minutes"] <= old["duration_minutes"]
    LeaseProposal.model_validate(new)


CASES = [
    raw(stmt("All", ["s3:*"], [BUCKET, f"{BUCKET}/*"])),
    raw(stmt("Read", ["s3:GetObject", "s3:PutBucketPolicy"], [OBJ, BUCKET])),
    raw(
        stmt("Read", ["s3:GetObject"], [OBJ]),
        stmt("List", ["s3:ListBucket"], [BUCKET]),
        duration_minutes=180,
    ),
    raw(stmt("Put", ["s3:PutObject"], [OBJ]), duration_minutes=90, summary="Upload fixed invoices"),
    raw(stmt("All", ["s3:*"], [OBJ]), summary="Upload fixed invoices to exports/2026-10/"),
]


@pytest.mark.parametrize("p", CASES)
async def test_every_high_or_medium_finding_has_a_fix(p: dict[str, Any]) -> None:
    r = await review(p, make_ctx())
    assert all(f.fix for f in r.findings if f.severity in ("HIGH", "MEDIUM"))


async def test_s3_star_becomes_a_low_read_lease() -> None:
    p = CASES[0]
    r = await review(p, make_ctx())
    assert r.scored.risk == "HIGH" and r.corrected is not None
    narrower_than(r.corrected, p)
    by_res = {tuple(s["resources"]): s["actions"] for s in r.corrected["statements"]}
    assert by_res == {(f"{BUCKET}/*",): ["s3:GetObject"], (BUCKET,): ["s3:ListBucket"]}
    assert r.corrected_risk == "LOW"


async def test_named_tamper_action_is_dropped() -> None:
    r = await review(CASES[1], make_ctx())
    assert r.scored.risk == "BLOCK" and r.corrected is not None
    assert [s["actions"] for s in r.corrected["statements"]] == [["s3:GetObject"]]
    narrower_than(r.corrected, CASES[1])


async def test_prefix_condition_and_shorter_duration() -> None:
    r = await review(CASES[2], make_ctx())
    assert r.scored.risk == "MEDIUM" and r.corrected is not None
    assert r.corrected["duration_minutes"] == 60
    lst = next(s for s in r.corrected["statements"] if s["sid"] == "List")
    assert lst["conditions"] == {"StringLike": {"s3:prefix": ["exports/2026-10/*"]}}
    assert r.corrected_risk == "LOW"


async def test_write_purpose_keeps_writes_but_narrows() -> None:
    r = await review(CASES[4], make_ctx())
    assert r.corrected is not None
    assert r.corrected["statements"][0]["actions"] == ["s3:GetObject", "s3:PutObject"]
    narrower_than(r.corrected, CASES[4])


async def test_no_correction_when_it_wouldnt_be_lower() -> None:
    r = await review(CASES[3], make_ctx())
    assert r.scored.risk == "MEDIUM" and r.corrected is None


@pytest.mark.parametrize(
    "p",
    [
        raw(stmt("S", ["s3:GetObject"], ["*"])),
        raw(stmt("W", ["s3:PutObject"], [f"{DECOY}/*"])),
        raw(stmt("O", ["s3:GetObject"], ["arn:aws:s3:::other-team/a"])),
        raw(stmt("N", ["s3:GetObject"], [OBJ], NotAction=["iam:*"])),
    ],
)
async def test_non_mechanical_fixes_give_no_corrected_lease(p: dict[str, Any]) -> None:
    r = await review(p, make_ctx())
    assert r.scored.risk in ("HIGH", "BLOCK") and r.corrected is None


async def test_malformed_condition_never_crashes_the_review() -> None:
    p = raw(
        stmt("List", ["s3:ListBucket"], [BUCKET], conditions={"StringLike": "oops"}),
        duration_minutes=180,
    )
    r = await review(p, make_ctx())
    assert r.corrected is None and r.scored.risk in ("MEDIUM", "HIGH", "BLOCK")


async def test_corrected_lease_says_what_it_changed() -> None:
    star = await review(CASES[0], make_ctx())
    assert star.corrected_changes == [
        "replaces s3:* with s3:GetObject, s3:ListBucket",
        "shortens 120 to 60 min",
    ]
    dropped = await review(CASES[1], make_ctx())
    assert dropped.corrected_changes == ["drops s3:PutBucketPolicy", "shortens 120 to 60 min"]
    longer = await review(CASES[2], make_ctx())
    assert longer.corrected_changes == [
        "adds an s3:prefix condition (exports/2026-10/*)",
        "shortens 180 to 60 min",
    ]


async def test_bucket_wide_objects_narrow_to_the_folder_the_summary_names() -> None:
    p = raw(
        stmt("All", ["s3:*"], [BUCKET, f"{BUCKET}/*"]),
        summary="Debug the billing report from exports/2026-10/",
        duration_minutes=60,
    )
    r = await review(p, make_ctx())
    assert r.corrected is not None and r.corrected_risk == "LOW"
    by_sid = {s["sid"]: s for s in r.corrected["statements"]}
    assert by_sid["All"]["resources"] == [OBJ]
    assert by_sid["AllList"]["conditions"] == {"StringLike": {"s3:prefix": ["exports/2026-10/*"]}}
    name = BUCKET.removeprefix("arn:aws:s3:::")
    assert r.corrected_changes == [
        "replaces s3:* with s3:GetObject, s3:ListBucket",
        f"narrows {name}/* to exports/2026-10/*",
        "adds an s3:prefix condition (exports/2026-10/*)",
    ]
    narrower_than(r.corrected, p)


async def test_split_keeps_the_original_conditions() -> None:
    cond = {
        "IpAddress": {"aws:SourceIp": ["10.0.0.0/8"]},
        "StringLike": {"s3:prefix": ["exports/"]},
    }
    p = raw(stmt("All", ["s3:*"], [BUCKET, f"{BUCKET}/*"], conditions=cond), duration_minutes=60)
    r = await review(p, make_ctx())
    assert r.corrected is not None
    narrower_than(r.corrected, p)
