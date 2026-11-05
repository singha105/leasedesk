"""Message templates: the brief's four examples, line limits, times in ET with UTC."""

from __future__ import annotations

from datetime import UTC, datetime

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
    )
    assert text == (
        "LS-0007 · needs approval\n"
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
        "LS-0007 · ACTIVE until 4:42 PM ET (21:42 UTC)\n"
        "Approved by Priya at 2:42 PM. Policy leasedesk-LS-0007 on lease-demo-dev.\n"
        "AWS enforces the expiry itself. Revoke early: @keymaster revoke LS-0007"
    )


def test_expiry_example() -> None:
    assert fmt.expiry("LS-0007", EXPIRY, TZ) == (
        "LS-0007 · EXPIRED at 4:42 PM ET. Policy removed, ledger updated."
    )


def test_refusal_example() -> None:
    assert fmt.refusal(
        "LS-0007", "Arnab requested this lease, so Arnab can't approve it.", "Priya"
    ) == (
        "LS-0007 · approval not counted\n"
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
        fmt.approval_counted("LS-0001", "Priya", 1, 2),
        fmt.leases_list([f"LS-{i:04d} ACTIVE" for i in range(40)]),
        fmt.help_text(),
        fmt.short_help(),
        fmt.whoami("Arnab", "@switch-discord-x:localhost"),
        fmt.reconciled("leasedesk-LS-0003", "lease-demo-dev", "lease EXPIRED"),
        fmt.policy_warning("bad YAML at line 3", "abc123def456"),
        fmt.policy_too_large("LS-0001", 2100, 2048),
        fmt.proposal_error([f"problem {i}" for i in range(30)]),
        fmt.frozen("Priya", 3),
        fmt.unfrozen("Priya"),
        fmt.alert("LS-0001", "policy vanished from lease-demo-dev"),
    ]
    for t in texts:
        assert 1 <= len(t.splitlines()) <= 12, t
        assert len(t) <= 2000, t
