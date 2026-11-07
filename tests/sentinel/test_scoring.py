"""Scoring: deterministic, explained, never lowered by an error."""

from __future__ import annotations

import pytest

from leasedesk.common.schemas import Finding
from leasedesk.sentinel.scoring import score


def f(sev: str, n: int = 1) -> list[Finding]:
    return [Finding(rule=f"r{i}", severity=sev) for i in range(n)]  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("findings", "minutes", "risk", "points"),
    [
        ([], 60, "LOW", 2),
        (f("LOW", 2), 60, "LOW", 8),
        (f("LOW", 3), 60, "LOW", 11),
        (f("LOW", 3), 89, "LOW", 11),
        (f("LOW", 3), 90, "MEDIUM", 12),
        (f("MEDIUM"), 1, "MEDIUM", 12),
        (f("MEDIUM", 3), 29, "MEDIUM", 36),
        (f("MEDIUM", 3) + f("LOW"), 30, "HIGH", 40),
        (f("HIGH"), 5, "HIGH", 40),
        (f("INFO", 5), 60, "LOW", 2),
    ],
)
def test_table(findings: list[Finding], minutes: int, risk: str, points: int) -> None:
    s = score(findings, minutes)
    assert (s.risk, s.score, s.raw) == (risk, points, points)


def test_any_block_is_block_with_score_100() -> None:
    s = score(f("LOW") + f("BLOCK"), 30)
    assert (s.risk, s.score) == ("BLOCK", 100)
    assert "BLOCK" in s.arithmetic and "n/a" in s.arithmetic


def test_score_capped_at_100_raw_kept() -> None:
    s = score(f("HIGH", 3), 120)
    assert (s.risk, s.score, s.raw) == ("HIGH", 100, 124)


def test_arithmetic_reads_as_a_sum() -> None:
    s = score(f("HIGH") + f("MEDIUM", 2) + f("LOW"), 120)
    assert s.arithmetic == (
        "40x1 HIGH + 12x2 MEDIUM + 3x1 LOW + 4 (120 min / 30) = 71 → HIGH (a HIGH finding)"
    )
    assert score(f("LOW"), 30).arithmetic == "3x1 LOW + 1 (30 min / 30) = 4 → LOW (under 12)"
    assert score(f("MEDIUM"), 30).arithmetic.endswith("= 13 → MEDIUM (12 or more)")


def test_scoring_doc_lists_every_fixed_rule() -> None:
    from pathlib import Path

    from leasedesk.sentinel.analyzers import (  # noqa: F401 - importing registers the rules
        access_level,
        aws,
        duration,
        escalation,
        lint,
        scope,
        structure,
        tamper,
    )
    from leasedesk.sentinel.findings import RULES

    doc = Path("docs/SCORING.md").read_text()
    fixed = [
        r for r in RULES if not r.startswith(("escalation-", "aws-")) or r == "escalation-near-miss"
    ]
    missing = [r for r in fixed if f"`{r}`" not in doc]
    assert not missing
