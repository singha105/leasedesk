"""Golden cases: one fixture per proposal in tests/sentinel/fixtures/, one test for all of them.

Each fixture says the exact risk and the rule ids that must be among the findings. Every HIGH and
MEDIUM finding must carry a fix, every finding a reference, and each review must take under
2 seconds (mock mode). ``same_as`` cases (prompt injection in the text) must produce exactly the
findings and score of the clean proposal they name.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from leasedesk.common.policy import parse_policy_text
from leasedesk.sentinel.catalog import parse_catalog_text
from leasedesk.sentinel.context import ReviewContext
from leasedesk.sentinel.review import Review, review

FIXTURES = Path(__file__).parent / "fixtures"
CASES = sorted(FIXTURES.glob("*.json"))
MAX_MS = 2000


def context() -> ReviewContext:
    return ReviewContext(
        policy=parse_policy_text((FIXTURES / "context" / "access-policy.yaml").read_text()),
        catalog=parse_catalog_text((FIXTURES / "context" / "resource-catalog.md").read_text()),
        account_id="123456789012",
        aws_mode="mock",
    )


def load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text())
    return data


def outcome(r: Review) -> tuple[Any, ...]:
    return (r.findings, r.scored, r.aws_validation, r.corrected_risk)


def test_there_are_at_least_24_cases() -> None:
    assert len(CASES) >= 24


@pytest.mark.parametrize("path", CASES, ids=lambda p: p.stem)
async def test_golden(path: Path) -> None:
    case = load(path.stem)
    expect = case["expect"]
    r = await review(case["proposal"], context())
    found = {f.rule for f in r.findings}
    assert r.scored.risk == expect["risk"], (r.scored.arithmetic, sorted(found))
    assert set(expect["rules"]) <= found, sorted(found)
    for f in r.findings:
        assert f.reference, f.rule
        if f.severity in ("HIGH", "MEDIUM"):
            assert f.fix, f.rule
    for rule, text in expect.get("fix_contains", {}).items():
        assert any(text in (f.fix or "") for f in r.findings if f.rule == rule), rule
    for rule, text in expect.get("detail_contains", {}).items():
        assert any(text in f.detail for f in r.findings if f.rule == rule), rule
    if "corrected_risk" in expect:
        assert r.corrected_risk == expect["corrected_risk"]
    if "same_as" in expect:
        clean = await review(load(expect["same_as"])["proposal"], context())
        assert outcome(r) == outcome(clean)
    assert r.elapsed_ms < MAX_MS
