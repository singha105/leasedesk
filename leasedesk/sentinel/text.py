"""What sentinel posts. Every message is a short summary (at most 6 lines) plus at most one
fenced block, and stays within Discord's 2,000 characters so keymaster sees one message.

Text that came from a proposal (sids, actions, ARNs, summaries) is data. It is cleaned before it
reaches a message: a backtick could close the fence early and an ``@`` could mention an agent,
so in prose they become ``'`` and ``(at)``. Inside a JSON block they are written as
``\\u0060`` and ``\\u0040``, which parse back to the same text, so a corrected lease can be
resubmitted as is.
"""

from __future__ import annotations

import json
from typing import Any

from leasedesk.common.schemas import Finding, LeaseVerdict
from leasedesk.sentinel.findings import RULES, make
from leasedesk.sentinel.review import Review
from leasedesk.sentinel.store import StoredReview

MESSAGE_LIMIT = 2000
MARGIN = 40  # room for "@keymaster " when delivery falls back to a plain post
SUMMARY_FINDINGS = 4
LINE_MAX = 170
DETAIL_MAX = 160
FIX_MAX = 120
MAX_BLOCK_FINDINGS = 49  # LeaseVerdict holds 50; one slot for the more-findings pointer
EXPLAIN_FINDINGS = 15  # explain stays a few messages, so one request can't flood the room
EXPLAIN_DETAIL = 200
EXPLAIN_FIX = 150


def clean(text: str) -> str:
    return " ".join(text.split()).replace("`", "'").replace("@", "(at)")


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def dumps(data: Any, *, pretty: bool = False) -> str:
    text = json.dumps(
        data,
        ensure_ascii=False,
        indent=2 if pretty else None,
        separators=(",", ": ") if pretty else (",", ":"),
    )
    # Backticks and @ only ever occur inside JSON strings, where these escapes are valid.
    return text.replace("`", "\\u0060").replace("@", "\\u0040")


def _line(f: Finding) -> str:
    where = f" ({clean(f.statement_sid)})" if f.statement_sid else ""
    fix = f" Fix: {clean(f.fix)}" if f.fix else ""
    return _clip(f"[{f.severity}] {f.rule}{where}: {clean(f.detail)}{fix}", LINE_MAX)


def _short(f: Finding) -> Finding:
    return Finding(
        rule=f.rule,
        severity=f.severity,
        statement_sid=clean(f.statement_sid) if f.statement_sid else None,
        detail=_clip(clean(f.detail), DETAIL_MAX),
        fix=_clip(clean(f.fix), FIX_MAX) if f.fix else None,
    )


def _footer(review_id: str, r: Review) -> str:
    parts = []
    hidden = len(r.findings) - SUMMARY_FINDINGS
    if hidden > 0:
        parts.append(f"+{hidden} more")
    parts.append(f"all findings: @sentinel explain {review_id}")
    if r.corrected is not None:
        parts.append(f"corrected lease below ({r.corrected_risk})")
    parts.append(f"AWS validation: {_aws(r)}")
    return " · ".join(parts)


def _aws(r: Review) -> str:
    if r.aws_validation == "skipped" and r.aws_note.startswith("mock mode"):
        return "skipped (mock mode)"
    note = f" ({_clip(clean(r.aws_note), 40)})" if r.aws_note else ""
    return r.aws_validation + note


def _score(r: Review) -> str:
    return "score n/a" if r.scored.risk == "BLOCK" else f"score {r.scored.score}"


def _compose(header: str, review_id: str, r: Review, tag: str, block: Any) -> str:
    """Summary lines plus the block, dropping block findings (least severe first) to fit."""
    lines = [header, *(_line(f) for f in r.findings[:SUMMARY_FINDINGS]), _footer(review_id, r)]
    head = "\n".join(lines)
    n = min(len(r.findings), MAX_BLOCK_FINDINGS)
    while True:
        shown = [_short(f) for f in r.findings[:n]]
        if n < len(r.findings):
            more = len(r.findings) - n
            shown.append(
                make(
                    "more-findings",
                    None,
                    f"{more} more finding(s); ask sentinel: explain {review_id}",
                    reference=None,
                )
            )
        text = f"{head}\n```{tag}\n{dumps(block(shown))}\n```"
        if len(text) <= MESSAGE_LIMIT - MARGIN or n == 0:
            return text
        n -= 1


def verdict_message(lease_id: str, r: Review) -> str:
    """The gating reply to keymaster. Starts with the command word keymaster dispatches on."""
    assert r.proposal_hash is not None  # noqa: S101 - callers only send verdicts for readable leases

    def block(findings: list[Finding]) -> dict[str, Any]:
        v = LeaseVerdict(
            kind="lease_verdict",
            version=1,
            lease_id=lease_id,
            risk=r.scored.risk,
            score=r.scored.score,
            findings=findings,
            aws_validation=r.aws_validation,
            proposal_hash=r.proposal_hash,
        )
        return v.model_dump(mode="json", exclude_none=True)

    header = f"verdict {lease_id} · risk {r.scored.risk} · {_score(r)}"
    return _compose(header, lease_id, r, "verdict", block)


def advisory_message(review_id: str, r: Review, *, lease_id: str | None, why: str) -> str:
    """An answer that decides nothing: a check, or a review request not sent by keymaster."""

    def block(findings: list[Finding]) -> dict[str, Any]:
        return {
            "kind": "lease_advisory",
            "version": 1,
            "review_id": review_id,
            "lease_id": lease_id,
            "risk": r.scored.risk,
            "score": r.scored.score,
            "findings": [f.model_dump(mode="json", exclude_none=True) for f in findings],
            "aws_validation": r.aws_validation,
            "proposal_hash": r.proposal_hash,
        }

    subject = lease_id or "this lease"
    header = (
        f"Advisory only, not a verdict ({why}): {subject} would be {r.scored.risk} · {_score(r)}"
    )
    return _compose(header, review_id, r, "advisory", block)


def corrected_message(review_id: str, r: Review) -> str | None:
    if r.corrected is None:
        return None
    what = "; ".join(r.corrected_changes) or "narrows the request"
    head = (
        f"{review_id} · corrected lease, re-reviewed as {r.corrected_risk}: {clean(what)}. "
        "It never adds access. To use it, submit this block to keymaster as is."
    )
    for pretty in (True, False):
        text = f"{head}\n```lease\n{dumps(r.corrected, pretty=pretty)}\n```"
        if len(text) <= MESSAGE_LIMIT - MARGIN:
            return text
    return f"{review_id} · a corrected lease exists but is too long to post; use the fixes listed."


def explain_text(review_id: str, s: StoredReview) -> str:
    """Every finding with its fix and reference, plus the arithmetic. May span messages."""
    when = f" · reviewed {s.created_at:%Y-%m-%d %H:%M} UTC" if s.created_at else ""
    kind = "verdict" if s.gating else "advisory"
    raw = f" (uncapped {s.raw_score})" if s.raw_score != s.score else ""
    score = "score n/a" if s.risk == "BLOCK" else f"score {s.score}{raw}"
    lines = [
        f"{review_id} · {kind} · {s.risk} · {score}{when}",
        f"Arithmetic: {s.arithmetic}",
        f"AWS Access Analyzer: {s.aws_validation}"
        + (f" ({clean(s.aws_note)})" if s.aws_note else ""),
    ]
    if not s.findings:
        lines.append("No findings.")
    for i, f in enumerate(s.findings[:EXPLAIN_FINDINGS], 1):
        where = f" ({clean(f.statement_sid)})" if f.statement_sid else ""
        detail = _clip(clean(f.detail), EXPLAIN_DETAIL)
        lines.append(f"{i}. [{f.severity}] {f.rule}{where}: {detail}")
        if f.fix:
            lines.append(f"   Fix: {_clip(clean(f.fix), EXPLAIN_FIX)}")
        if f.reference:
            lines.append(f"   Ref: {f.reference}")
    hidden = len(s.findings) - EXPLAIN_FINDINGS
    if hidden > 0:
        lines.append(f"{hidden} more finding(s) not shown; the arithmetic above counts them all.")
    if s.corrected is not None:
        lines.append(f"A corrected lease was offered (re-reviewed as {s.corrected_risk}).")
    return "\n".join(lines)


def rules_text() -> str:
    lines = ["sentinel's rules (id · severity · reference). Scoring: docs/SCORING.md"]
    analyzer = None
    for rule in RULES.values():
        if rule.analyzer != analyzer:
            analyzer = rule.analyzer
            lines.append(f"{analyzer}:")
        lines.append(f"- {rule.id} · {rule.severity} · {rule.reference}")
    return "\n".join(lines)


def help_text() -> str:
    return (
        "sentinel reviews leases with fixed rules and AWS checks, no model. "
        "@sentinel check with a ```lease``` block: an advisory review · "
        "@sentinel explain <id>: every finding and the score arithmetic · "
        "@sentinel rules: every rule with its source."
    )


def slow_down() -> str:
    return "Slow down: sentinel answers 10 requests a minute per person. Try again in a minute."
