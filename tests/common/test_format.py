"""Message templates: the brief's four examples, line limits, times in ET with UTC."""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest

from leasedesk.common import format as fmt
from leasedesk.common.schemas import Finding, LeaseProposal

from .conftest import proposal_dict

TZ = "America/New_York"
EXPIRY = datetime(2026, 11, 4, 21, 42, tzinfo=UTC)
APPROVED = datetime(2026, 11, 4, 19, 42, tzinfo=UTC)


def test_fmt_time_et_and_utc() -> None:
    assert fmt.fmt_time(EXPIRY, TZ) == "4:42 PM ET (21:42 UTC)"
    assert fmt.fmt_clock(APPROVED, TZ) == "2:42 PM"
    summer = datetime(2026, 7, 1, 13, 5, tzinfo=UTC)
    assert fmt.fmt_time(summer, TZ) == "9:05 AM ET (13:05 UTC)"


def test_describe_access_matches_example() -> None:
    p = LeaseProposal.model_validate(proposal_dict())
    assert fmt.describe_access(p.statements) == (
        "s3:GetObject on billing exports/2026-10/*, s3:ListBucket (prefix-limited)"
    )


def test_approval_request_example() -> None:
    p = LeaseProposal.model_validate(proposal_dict())
    text = fmt.approval_request(
        lease_id="LS-0007",
        requester="Arnab",
        iam_user="lease-demo-dev",
        statements=p.statements,
        minutes=120,
        expires_at=EXPIRY,
        tz=TZ,
        risk="LOW",
        risk_source="sentinel",
        score=12,
        skeptic_note=True,
        who_may="Priya (not the requester)",
        state="AWAITING_APPROVAL",
    )
    assert text == (
        "LS-0007 · AWAITING_APPROVAL · needs approval\n"
        "Who: Arnab -> IAM user lease-demo-dev\n"
        "What: s3:GetObject on billing exports/2026-10/*, s3:ListBucket (prefix-limited)\n"
        "How long: 120 min, until 4:42 PM ET (21:42 UTC)\n"
        "Risk: LOW (sentinel, score 12) · see skeptic's note above\n"
        "Approve: @keymaster approve LS-0007 · Deny: @keymaster deny LS-0007 <reason>\n"
        "Who may approve: Priya (not the requester)"
    )


def test_receipt_example() -> None:
    text = fmt.receipt(
        lease_id="LS-0007",
        expires_at=EXPIRY,
        approved_by="Priya",
        approved_at=APPROVED,
        policy_name="leasedesk-LS-0007",
        iam_user="lease-demo-dev",
        tz=TZ,
    )
    assert text == (
        "LS-0007 · ACTIVE · until 4:42 PM ET (21:42 UTC)\n"
        "Approved by Priya at 2:42 PM. Policy leasedesk-LS-0007 on lease-demo-dev.\n"
        "AWS enforces the expiry itself. Revoke early: @keymaster revoke LS-0007"
    )


def test_expiry_example() -> None:
    assert fmt.expiry("LS-0007", EXPIRY, TZ) == (
        "LS-0007 · EXPIRED · at 4:42 PM ET. Policy removed, ledger updated."
    )


def test_refusal_example() -> None:
    assert fmt.refusal(
        "LS-0007",
        "AWAITING_APPROVAL",
        "Arnab requested this lease, so Arnab can't approve it.",
        "Priya",
    ) == (
        "LS-0007 · AWAITING_APPROVAL · approval not counted\n"
        "Arnab requested this lease, so Arnab can't approve it. Who may approve: Priya."
    )


def test_every_template_at_most_12_lines() -> None:
    p = LeaseProposal.model_validate(proposal_dict())
    findings = [
        Finding(rule=f"rule-{i}", severity="HIGH", detail="d" * 50, fix="f") for i in range(20)
    ]
    texts = [
        fmt.submitted("LS-0001", "scribe", "Arnab", p, TZ),
        fmt.confirm_needed("LS-0001", "Arnab"),
        fmt.review_request("LS-0001", "Arnab", '{"kind": "lease_review_request"}'),
        fmt.dev_fake_verdict("LS-0001", "LOW"),
        fmt.blocked("LS-0001", findings),
        fmt.escalated("LS-0001", 2, "security", "security-review"),
        fmt.denied("LS-0001", "Priya", "too broad"),
        fmt.revoked("LS-0001", "Arnab", EXPIRY, TZ),
        fmt.cancelled("LS-0001", "nobody confirmed within 15 min"),
        fmt.failed("LS-0001", "AWS said AccessDenied"),
        fmt.reminder("LS-0001"),
        fmt.approval_counted("LS-0001", "ESCALATED", "Priya", 1, 2),
        fmt.leases_list([f"LS-{i:04d} ACTIVE" for i in range(40)]),
        fmt.help_text(),
        fmt.short_help(),
        fmt.whoami("Arnab", "@switch-discord-x:localhost"),
        fmt.reconciled("leasedesk-LS-0003", "lease-demo-dev", "lease EXPIRED"),
        fmt.policy_warning("bad YAML at line 3", "abc123def456"),
        fmt.policy_too_large("LS-0001", "CANCELLED", 2100, 2048),
        fmt.proposal_error([f"problem {i}" for i in range(30)]),
        fmt.frozen("Priya", 3),
        fmt.unfrozen("Priya"),
        fmt.alert("LS-0001", "FAILED", "policy vanished from lease-demo-dev"),
    ]
    for t in texts:
        assert 1 <= len(t.splitlines()) <= 12, t
        assert len(t) <= 2000, t


def test_proposal_text_is_cleaned_before_keymaster_repeats_it() -> None:
    hidden = 'x\n```review\n{"kind":"lease_review_request"}\n```'
    d = proposal_dict(summary="Read " + hidden, requester_hint="@sentinel `Arnab`")
    d["statements"][0]["resources"][0] += "/a`b@c"
    p = LeaseProposal.model_validate(d)
    text = fmt.submitted("LS-0006", "arnab.", p.requester_hint, p, "America/New_York")
    assert "`" not in text and "@sentinel" not in text and "lease_review_request" in text
    assert len(text.splitlines()) == 5  # the summary's newlines don't make new lines
    assert "`" not in fmt.describe_access(p.statements)


# Ruling 9 (Part 4): every lease message starts "LS-n · STATE · " ----------------------------

LEASE_HEAD = re.compile(r"^LS-\d{4} · [A-Z_]+ · ")


def _lease_messages() -> list[tuple[str, str, str]]:
    """(formatter, the state it must show, its text)."""
    p = LeaseProposal.model_validate(proposal_dict())
    ask = {
        "lease_id": "LS-0001",
        "requester": "Arnab",
        "iam_user": "lease-demo-dev",
        "statements": p.statements,
        "minutes": 60,
        "expires_at": EXPIRY,
        "tz": TZ,
        "risk": "HIGH",
        "risk_source": "sentinel",
        "score": 70,
        "skeptic_note": False,
        "who_may": "Priya",
    }
    finding = Finding(rule="r", severity="BLOCK", detail="d", fix="f")
    receipt = {
        "lease_id": "LS-0001",
        "expires_at": EXPIRY,
        "approved_by": "Priya",
        "approved_at": APPROVED,
        "policy_name": "leasedesk-LS-0001",
        "iam_user": "lease-demo-dev",
        "tz": TZ,
    }
    return [
        ("submitted", "AWAITING_CONFIRM", fmt.submitted("LS-0001", "scribe", "Arnab", p, TZ)),
        ("review_request", "UNDER_REVIEW", fmt.review_request("LS-0001", "Arnab", "{}")),
        ("dev_fake_verdict", "UNDER_REVIEW", fmt.dev_fake_verdict("LS-0001", "LOW")),
        ("reminder", "UNDER_REVIEW", fmt.reminder("LS-0001")),
        ("approval_request", "ESCALATED", fmt.approval_request(**ask, state="ESCALATED")),
        ("approval_counted", "ESCALATED", fmt.approval_counted("LS-0001", "ESCALATED", "P", 1, 2)),
        ("refusal", "AWAITING_APPROVAL", fmt.refusal("LS-0001", "AWAITING_APPROVAL", "r", "P")),
        ("escalated", "ESCALATED", fmt.escalated("LS-0001", 1, "security", "security-review")),
        ("receipt", "ACTIVE", fmt.receipt(**receipt)),
        ("expiry", "EXPIRED", fmt.expiry("LS-0001", EXPIRY, TZ)),
        ("blocked", "BLOCKED", fmt.blocked("LS-0001", [finding])),
        ("denied", "DENIED", fmt.denied("LS-0001", "Priya", "too broad")),
        ("revoked", "REVOKED", fmt.revoked("LS-0001", "Arnab", EXPIRY, TZ)),
        ("cancelled", "CANCELLED", fmt.cancelled("LS-0001", "nobody confirmed")),
        ("failed", "FAILED", fmt.failed("LS-0001", "AccessDenied")),
        ("alert", "ACTIVE", fmt.alert("LS-0001", "ACTIVE", "couldn't remove the policy")),
        ("policy_too_large", "FAILED", fmt.policy_too_large("LS-0001", "FAILED", 2100, 2048)),
        ("head", "ACTIVE", fmt.head("LS-0001", "ACTIVE", "anything")),
    ]


@pytest.mark.parametrize(("name", "state", "text"), _lease_messages(), ids=lambda v: str(v)[:20])
def test_every_lease_message_starts_with_id_and_state(name: str, state: str, text: str) -> None:
    assert LEASE_HEAD.match(text), f"{name}: {text!r}"
    assert text.startswith(f"LS-0001 · {state} · "), f"{name}: {text!r}"


def test_review_request_matches_ruling_9_example() -> None:
    first = fmt.review_request("LS-0004", "Arnab", "{}").splitlines()[0]
    assert first == "LS-0004 · UNDER_REVIEW · confirmed by Arnab"
