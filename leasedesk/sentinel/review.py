"""One review: parse leniently, run every analyzer, ask AWS in real mode, score, and offer a
corrected lease when the fixes are mechanical. No model, no randomness: the same proposal and
context always give the same findings, risk and corrected lease.

Failing closed: an unreadable proposal, a missing access-policy or resource-catalog, or an
analyzer that raises each adds a BLOCK finding. An AWS error only marks the AWS check
``skipped``; it never removes a finding or lowers a risk.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pydantic import ValidationError

from leasedesk.common.logging import get_logger
from leasedesk.common.schemas import Finding, LeaseProposal, Risk, proposal_hash
from leasedesk.sentinel.analyzers import (
    access_level,
    duration,
    escalation,
    lint,
    scope,
    structure,
    tamper,
)
from leasedesk.sentinel.analyzers.aws import AwsResult
from leasedesk.sentinel.context import ReviewContext
from leasedesk.sentinel.findings import by_severity, make
from leasedesk.sentinel.fixes import corrected_lease, describe_changes
from leasedesk.sentinel.model import ProposalView
from leasedesk.sentinel.preview import preview
from leasedesk.sentinel.scoring import Scored, score

log = get_logger(__name__)
Analyzer = Callable[[ProposalView, ReviewContext], list[Finding]]
ANALYZERS: dict[str, Analyzer] = {
    "structure": structure.analyze,
    "lint": lint.analyze,
    "access_level": access_level.analyze,
    "escalation": escalation.analyze,
    "scope": scope.analyze,
    "tamper_and_exposure": tamper.analyze,
    "duration": duration.analyze,
}
RISK_ORDER: dict[Risk, int] = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "BLOCK": 3}
MOCK_NOTE = "mock mode: Access Analyzer runs in real mode only"


class AwsLike(Protocol):
    async def validate(self, doc: dict[str, Any], sids: list[str]) -> AwsResult: ...
    async def crown_jewels(self, doc: dict[str, Any], ctx: ReviewContext) -> list[Finding]: ...


@dataclass(frozen=True)
class Review:
    findings: list[Finding]
    scored: Scored
    aws_validation: Literal["pass", "findings", "skipped"]
    aws_note: str
    corrected: dict[str, Any] | None
    corrected_risk: Risk | None
    proposal_hash: str | None
    elapsed_ms: float
    corrected_changes: list[str] = field(default_factory=list)


def run_analyzers(view: ProposalView, ctx: ReviewContext) -> list[Finding]:
    out: list[Finding] = []
    if ctx.policy is None:
        out.append(
            make(
                "policy-unavailable",
                None,
                "access-policy couldn't be loaded, so nothing can be checked against it.",
                "Fix the access-policy document; sentinel reviews again once it loads.",
            )
        )
    if ctx.catalog is None:
        out.append(
            make(
                "catalog-unavailable",
                None,
                "resource-catalog couldn't be loaded, so crown jewels can't be checked.",
                "Fix the resource-catalog document; sentinel reviews again once it loads.",
            )
        )
    for name, analyze in ANALYZERS.items():
        try:
            out += analyze(view, ctx)
        except Exception as exc:
            log.error("analyzer_error", analyzer=name, error=type(exc).__name__)
            out.append(
                make(
                    "analyzer-error",
                    None,
                    f"The {name} analyzer failed ({type(exc).__name__}), so the lease can't "
                    "be cleared.",
                    "Report it to the LeaseDesk maintainers; resubmitting won't help.",
                )
            )
    return out


def assess(view: ProposalView, ctx: ReviewContext) -> tuple[list[Finding], Scored]:
    findings = by_severity(run_analyzers(view, ctx))
    return findings, score(findings, view.duration_minutes)


def _hash(raw: Any, view: ProposalView) -> str:
    """keymaster's hash when the proposal is one keymaster accepts, else the lenient one."""
    try:
        return proposal_hash(LeaseProposal.model_validate(raw))
    except ValidationError:
        return view.hash()


def _offer(
    view: ProposalView, findings: list[Finding], scored: Scored, ctx: ReviewContext
) -> tuple[dict[str, Any] | None, Risk | None, list[str]]:
    none: tuple[None, None, list[str]] = (None, None, [])
    if scored.risk == "LOW":
        return none
    try:
        fixed = corrected_lease(view, findings, ctx)
        if fixed is None:
            return none
        changes = describe_changes(view, fixed)
    except Exception as exc:  # a fixer bug costs the suggestion, never the verdict
        log.error("corrected_lease_error", error=type(exc).__name__)
        return none
    again, _ = ProposalView.parse(fixed)
    if again is None:
        return none
    new_findings, new_scored = assess(again, ctx)
    if any(f.severity in ("BLOCK", "HIGH") for f in new_findings):
        return none
    if RISK_ORDER[new_scored.risk] >= RISK_ORDER[scored.risk]:
        return none
    return fixed, new_scored.risk, changes


async def _aws(view: ProposalView, ctx: ReviewContext, aws: AwsLike | None) -> AwsResult:
    if ctx.aws_mode != "real":
        return AwsResult("skipped", note=MOCK_NOTE)
    if aws is None:
        return AwsResult("skipped", note="no Access Analyzer client")
    try:
        doc = preview(view)
    except Exception as exc:
        return AwsResult("skipped", note=f"preview failed ({type(exc).__name__})")
    result = await aws.validate(doc, [s.sid for s in view.statements])
    jewels = await aws.crown_jewels(doc, ctx)
    return AwsResult(result.status, [*result.findings, *jewels], result.note)


async def review(raw: Any, ctx: ReviewContext, *, aws: AwsLike | None = None) -> Review:
    started = time.perf_counter()
    view, problems = ProposalView.parse(raw)
    if view is None:
        findings = problems
        scored = score(findings, 0)
        result = AwsResult("skipped", note="unreadable proposal")
        corrected, corrected_risk, phash = None, None, None
        changes: list[str] = []
    else:
        findings, _ = assess(view, ctx)
        result = await _aws(view, ctx, aws)
        findings = by_severity([*findings, *result.findings])
        scored = score(findings, view.duration_minutes)
        corrected, corrected_risk, changes = _offer(view, findings, scored, ctx)
        phash = _hash(raw, view)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    log.info(
        "review_done",
        risk=scored.risk,
        findings=len(findings),
        aws_validation=result.status,
        corrected=corrected is not None,
        elapsed_ms=elapsed_ms,
    )
    return Review(
        findings=findings,
        scored=scored,
        aws_validation=result.status,
        aws_note=result.note,
        corrected=corrected,
        corrected_risk=corrected_risk,
        proposal_hash=phash,
        elapsed_ms=elapsed_ms,
        corrected_changes=changes,
    )
