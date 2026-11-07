"""The rule registry: every rule sentinel can raise, with its severity and its source.

Analyzers build findings through ``make`` so a rule's severity and reference live in one
place and ``@sentinel rules`` can list them. Escalation and AWS rules are added at import time
from their data (``escalation_paths.yaml``) or from AWS's own issue codes.
"""

from __future__ import annotations

from dataclasses import dataclass

from leasedesk.common.schemas import Finding, Severity

IAM_DOCS = "https://docs.aws.amazon.com/IAM/latest/UserGuide"
LEAST_PRIVILEGE = f"{IAM_DOCS}/best-practices.html#grant-least-privilege"
ACCESS_LEVELS = f"{IAM_DOCS}/access_policies_understand-policy-summary-access-level-summaries.html"
PARLIAMENT = "https://github.com/duo-labs/parliament"
SCORING = "https://github.com/singha105/leasedesk/blob/main/docs/SCORING.md"
SEVERITY_ORDER: dict[str, int] = {"BLOCK": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}
MAX_TEXT = 300


@dataclass(frozen=True)
class Rule:
    id: str
    severity: Severity
    title: str
    reference: str
    analyzer: str


RULES: dict[str, Rule] = {}


def register(rule_id: str, severity: Severity, title: str, reference: str, analyzer: str) -> Rule:
    rule = Rule(rule_id, severity, title, reference, analyzer)
    RULES[rule_id] = rule
    return rule


def _clip(text: str | None) -> str | None:
    if text is None:
        return None
    text = " ".join(text.split())
    return text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 1] + "…"


def make(
    rule_id: str,
    sid: str | None,
    detail: str,
    fix: str | None = None,
    *,
    severity: Severity | None = None,
    reference: str | None = None,
) -> Finding:
    rule = RULES[rule_id]
    return Finding(
        rule=rule_id,
        severity=severity or rule.severity,
        statement_sid=sid,
        detail=_clip(detail) or "",
        fix=_clip(fix),
        reference=reference or rule.reference,
    )


def by_severity(findings: list[Finding]) -> list[Finding]:
    """Most severe first; stable within a severity, so output order is deterministic."""
    return sorted(findings, key=lambda f: SEVERITY_ORDER[f.severity])


register(
    "analyzer-error", "BLOCK", "An analyzer failed, so the lease can't be judged", SCORING, "review"
)
register("policy-unavailable", "BLOCK", "No access-policy is loaded", SCORING, "review")
register("catalog-unavailable", "BLOCK", "No resource-catalog is loaded", SCORING, "review")
register(
    "policy-hash-mismatch",
    "INFO",
    "keymaster and sentinel read different access-policy versions",
    SCORING,
    "review",
)
register("more-findings", "INFO", "More findings than fit in one message", SCORING, "review")
