"""review(): the whole pipeline, failing closed."""

from __future__ import annotations

from typing import Any

import pytest
from structlog.testing import capture_logs

from leasedesk.common.schemas import LeaseProposal, proposal_hash
from leasedesk.sentinel import review as review_mod
from leasedesk.sentinel.analyzers.aws import AwsResult
from leasedesk.sentinel.findings import make
from leasedesk.sentinel.review import review

from .conftest import BUCKET, make_ctx, raw, rules, stmt


async def test_clean_read_is_low_and_hash_matches_keymaster() -> None:
    r = await review(raw(duration_minutes=60), make_ctx())
    assert r.scored.risk == "LOW"
    assert r.aws_validation == "skipped" and "mock" in r.aws_note
    assert r.proposal_hash == proposal_hash(LeaseProposal.model_validate(raw(duration_minutes=60)))
    assert r.corrected is None


async def test_deterministic() -> None:
    a = await review(raw(stmt("All", ["s3:*"], [BUCKET])), make_ctx())
    b = await review(raw(stmt("All", ["s3:*"], [BUCKET])), make_ctx())
    assert a.findings == b.findings and a.scored == b.scored and a.corrected == b.corrected


async def test_unreadable_blocks() -> None:
    r = await review("nope", make_ctx())
    assert r.scored.risk == "BLOCK" and rules(r.findings) == {"struct-unreadable"}


@pytest.mark.parametrize(
    ("over", "rule"),
    [({"policy": None}, "policy-unavailable"), ({"catalog": None}, "catalog-unavailable")],
)
async def test_missing_context_blocks(over: dict[str, Any], rule: str) -> None:
    r = await review(raw(duration_minutes=60), make_ctx(**over))
    assert r.scored.risk == "BLOCK" and rule in rules(r.findings)


async def test_analyzer_crash_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_: Any) -> list[Any]:
        raise RuntimeError("bug")

    monkeypatch.setitem(review_mod.ANALYZERS, "escalation", boom)
    r = await review(raw(duration_minutes=60), make_ctx())
    assert r.scored.risk == "BLOCK"
    crash = [f for f in r.findings if f.rule == "analyzer-error"]
    assert crash and "escalation" in crash[0].detail


class FakeAws:
    def __init__(self, result: AwsResult) -> None:
        self.result = result
        self.calls = 0

    async def validate(self, doc: dict[str, Any], sids: list[str]) -> AwsResult:
        self.calls += 1
        assert doc["Statement"][0]["Sid"] == "LS0000" + sids[0]
        return self.result

    async def crown_jewels(self, doc: dict[str, Any], ctx: Any) -> list[Any]:
        return []


async def test_real_mode_findings_reach_the_verdict() -> None:
    hit = make("lint-other", "ReadInvoiceExports", "x")
    aws = FakeAws(
        AwsResult("findings", [hit.model_copy(update={"rule": "aws-x", "severity": "HIGH"})])
    )
    r = await review(raw(duration_minutes=60), make_ctx(aws_mode="real"), aws=aws)  # type: ignore[arg-type]
    assert aws.calls == 1 and r.aws_validation == "findings" and r.scored.risk == "HIGH"


async def test_real_mode_aws_error_never_lowers_or_passes() -> None:
    p = raw(stmt("All", ["s3:*"], [BUCKET]))
    mock = await review(p, make_ctx())
    aws = FakeAws(AwsResult("skipped", note="ClientError"))
    real = await review(p, make_ctx(aws_mode="real"), aws=aws)  # type: ignore[arg-type]
    assert real.aws_validation == "skipped" and real.scored == mock.scored


async def test_mock_mode_makes_no_aws_call() -> None:
    aws = FakeAws(AwsResult("pass"))
    await review(raw(), make_ctx(), aws=aws)  # type: ignore[arg-type]
    assert aws.calls == 0


async def test_timing_logged_and_fast() -> None:
    with capture_logs() as logs:
        r = await review(raw(stmt("All", ["*"], ["*"])), make_ctx())
    done = [e for e in logs if e["event"] == "review_done"]
    assert done and done[0]["elapsed_ms"] == r.elapsed_ms
    assert r.elapsed_ms < 2000


async def test_fixer_crash_keeps_the_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_: Any) -> None:
        raise KeyError("bug")

    p = raw(stmt("All", ["s3:*"], [BUCKET]))
    before = await review(p, make_ctx())
    monkeypatch.setattr(review_mod, "corrected_lease", boom)
    after = await review(p, make_ctx())
    assert after.corrected is None and after.scored == before.scored
