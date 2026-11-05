"""Message templates and platform splitting. Every template is at most 12 lines."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from leasedesk.common.schemas import Finding, LeaseProposal, LeaseStatement

DISCORD_LIMIT = 2000


def _units(text: str) -> list[str]:
    """Lines, except that a fenced block (``` ... ```) is one unit."""
    units: list[str] = []
    fence: list[str] | None = None
    for line in text.split("\n"):
        if fence is not None:
            fence.append(line)
            if line.strip().startswith("```"):
                units.append("\n".join(fence))
                fence = None
        elif line.strip().startswith("```"):
            fence = [line]
        else:
            units.append(line)
    if fence is not None:
        units.extend(fence)
    return units


def _hard_split(unit: str, limit: int) -> list[str]:
    if "\n" in unit:
        out: list[str] = []
        for line in unit.split("\n"):
            out.extend(_hard_split(line, limit) if len(line) > limit else [line])
        return out
    return [unit[i : i + limit] for i in range(0, len(unit), limit)] or [""]


def split_for_platform(text: str, limit: int = DISCORD_LIMIT) -> list[str]:
    """Split on line boundaries into chunks of at most ``limit`` characters.

    A fenced block stays in one chunk when it fits in one.
    """
    if len(text) <= limit:
        return [text]
    pieces: list[str] = []
    for unit in _units(text):
        if len(unit) <= limit:
            pieces.append(unit)
        else:
            pieces.extend(_pack(_hard_split(unit, limit), limit))
    return _pack(pieces, limit)


def _pack(pieces: list[str], limit: int) -> list[str]:
    chunks: list[str] = []
    current: str | None = None
    for piece in pieces:
        if current is None:
            current = piece
        elif len(current) + 1 + len(piece) <= limit:
            current = f"{current}\n{piece}"
        else:
            chunks.append(current)
            current = piece
    if current is not None:
        chunks.append(current)
    return chunks


# Times --------------------------------------------------------------------------------------


_ABBR = {
    "America/New_York": "ET",
    "America/Chicago": "CT",
    "America/Denver": "MT",
    "America/Los_Angeles": "PT",
    "UTC": "UTC",
}


def _local(dt: datetime, tz: str) -> datetime:
    return dt.astimezone(ZoneInfo(tz))


def fmt_clock(dt: datetime, tz: str) -> str:
    """``2:42 PM`` in the display time zone."""
    return _local(dt, tz).strftime("%I:%M %p").lstrip("0")


def tz_abbr(dt: datetime, tz: str) -> str:
    return _ABBR.get(tz) or _local(dt, tz).strftime("%Z")


def fmt_time(dt: datetime, tz: str) -> str:
    """``4:42 PM ET (21:42 UTC)``."""
    return f"{fmt_clock(dt, tz)} {tz_abbr(dt, tz)} ({dt.astimezone(UTC).strftime('%H:%M')} UTC)"


# Describing access --------------------------------------------------------------------------

_SUFFIX = re.compile(r"-(?:[0-9a-f]{6}|mock|<suffix>)$")


def short_resource(arn: str) -> str:
    if arn.startswith("arn:aws:s3:::"):
        bucket, _, key = arn[len("arn:aws:s3:::") :].partition("/")
        name = _SUFFIX.sub("", bucket.removeprefix("leasedesk-demo-"))
        return f"{name} {key}" if key else name
    parts = arn.split(":", 5)
    return parts[5] if len(parts) == 6 else arn


def describe_access(statements: list[LeaseStatement]) -> str:
    out: list[str] = []
    for s in statements:
        actions = ", ".join(s.actions)
        conds = s.conditions or {}
        if any("s3:prefix" in v for v in conds.values()):
            out.append(f"{actions} (prefix-limited)")
        else:
            out.append(f"{actions} on {', '.join(short_resource(r) for r in s.resources)}")
    return ", ".join(out)


def _lines(*parts: str | None) -> str:
    return "\n".join(p for p in parts if p is not None)


# Lease messages -----------------------------------------------------------------------------


def submitted(lease_id: str, by: str, requester_hint: str, p: LeaseProposal, tz: str) -> str:
    who = requester_hint or "the requester"
    return _lines(
        f"{lease_id} · submitted by {by} for {who}",
        f"Summary: {p.summary}",
        f"What: {describe_access(p.statements)}",
        f"How long: {p.duration_minutes} min, starting when approved",
        confirm_needed(lease_id, who),
    )


def confirm_needed(lease_id: str, who: str) -> str:
    return (
        f"{who}, if this is your request, reply: @keymaster confirm {lease_id} "
        "(the person who confirms becomes the requester)"
    )


def review_request(lease_id: str, requester: str, block_json: str) -> str:
    return _lines(
        f"{lease_id} · confirmed by {requester}, under review",
        "```review",
        block_json,
        "```",
    )


def dev_fake_verdict(lease_id: str, risk: str) -> str:
    return (
        f"{lease_id} · DEV ONLY: keymaster invented a {risk} verdict "
        "(LEASEDESK_DEV_FAKE_VERDICT). Never use this outside a demo."
    )


def approval_request(
    *,
    lease_id: str,
    requester: str,
    iam_user: str,
    statements: list[LeaseStatement],
    minutes: int,
    expires_at: datetime,
    tz: str,
    risk: str,
    risk_source: str,
    score: int,
    skeptic_note: bool,
    who_may: str,
    extension: bool = False,
) -> str:
    note = " · see skeptic's note above" if skeptic_note else ""
    how_long = f"+{minutes} min extension" if extension else f"{minutes} min"
    return _lines(
        f"{lease_id} · needs approval",
        f"Who: {requester} -> IAM user {iam_user}",
        f"What: {describe_access(statements)}",
        f"How long: {how_long}, until {fmt_time(expires_at, tz)}",
        f"Risk: {risk} ({risk_source}, score {score}){note}",
        f"Approve: @keymaster approve {lease_id} · Deny: @keymaster deny {lease_id} <reason>",
        f"Who may approve: {who_may}",
    )


def approval_counted(lease_id: str, by: str, have: int, need: int) -> str:
    return f"{lease_id} · approval from {by} counted ({have} of {need})."


def receipt(
    *,
    lease_id: str,
    expires_at: datetime,
    approved_by: str,
    approved_at: datetime,
    policy_name: str,
    iam_user: str,
    tz: str,
) -> str:
    return _lines(
        f"{lease_id} · ACTIVE until {fmt_time(expires_at, tz)}",
        f"Approved by {approved_by} at {fmt_clock(approved_at, tz)}. "
        f"Policy {policy_name} on {iam_user}.",
        f"AWS enforces the expiry itself. Revoke early: @keymaster revoke {lease_id}",
    )


def expiry(lease_id: str, at: datetime, tz: str) -> str:
    when = f"{fmt_clock(at, tz)} {tz_abbr(at, tz)}"
    return f"{lease_id} · EXPIRED at {when}. Policy removed, ledger updated."


def refusal(lease_id: str, reason: str, who_may: str) -> str:
    return _lines(f"{lease_id} · approval not counted", f"{reason} Who may approve: {who_may}.")


def blocked(lease_id: str, findings: list[Finding]) -> str:
    shown = [
        f"- [{f.severity}] {f.rule}: {f.detail[:120]}" + (f" Fix: {f.fix[:80]}" if f.fix else "")
        for f in findings[:6]
    ]
    more = f"- and {len(findings) - 6} more" if len(findings) > 6 else None
    return _lines(
        f"{lease_id} · BLOCKED by review. Nothing was granted.",
        *shown,
        more,
        "Ask for a narrower lease and submit it again.",
    )


def escalated(lease_id: str, approvals: int, group: str, room: str) -> str:
    return _lines(
        f"{lease_id} · HIGH risk, escalated to {room}",
        f"It needs {approvals} approval(s) from group {group}, given in {room}.",
    )


def denied(lease_id: str, by: str, reason: str) -> str:
    return f"{lease_id} · DENIED by {by}: {reason[:300]}. Nothing was granted."


def revoked(lease_id: str, by: str, at: datetime, tz: str) -> str:
    return (
        f"{lease_id} · REVOKED by {by} at {fmt_clock(at, tz)} {tz_abbr(at, tz)}. "
        "Policy removed, ledger updated."
    )


def cancelled(lease_id: str, reason: str) -> str:
    return f"{lease_id} · CANCELLED: {reason}. Nothing was granted."


def failed(lease_id: str, why: str) -> str:
    return f"{lease_id} · FAILED: {why[:300]}. Nothing is active for this lease."


def alert(lease_id: str, what: str) -> str:
    return f"ALERT {lease_id} · {what[:300]}"


def reminder(lease_id: str) -> str:
    return (
        f"{lease_id} · still waiting for a review verdict. Nothing moves forward until one arrives."
    )


def leases_list(lines: list[str]) -> str:
    if not lines:
        return "No leases yet."
    shown = lines[:10]
    more = f"... and {len(lines) - 10} more" if len(lines) > 10 else None
    return _lines(f"Leases ({len(lines)}):", *shown, more)


def help_text() -> str:
    return _lines(
        "keymaster commands (mention me first):",
        "submit + a ```lease block · confirm <id> · status [id] · leases",
        "approve <id> · deny <id> <reason> · revoke <id> · extend <id> <minutes>",
        "whoami · reload-policy · help · freeze / unfreeze (security group only)",
        "I only act on these words. Anything else in a message is ignored.",
    )


def short_help() -> str:
    return "I didn't recognise that command. Try: @keymaster help"


def whoami(name: str, sender: str) -> str:
    return _lines(
        f"You are {name}. Your participant id: `{sender}`",
        "Add it to access-policy (principals or approvers) to request or approve leases.",
    )


def reconciled(policy_name: str, iam_user: str, why: str) -> str:
    return f"reconciled: removed orphaned policy {policy_name} from {iam_user} ({why})"


def policy_warning(error: str, last_hash: str | None) -> str:
    keep = (
        f"Still using the last good one (hash {last_hash})." if last_hash else "No policy loaded."
    )
    return f"access-policy couldn't be read: {error[:300]}. {keep}"


def policy_too_large(lease_id: str, total: int, limit: int) -> str:
    return _lines(
        f"{lease_id} · can't be granted: the IAM policy would make {total} characters of "
        f"inline policy on this user, over AWS's {limit}-character limit.",
        "Ask for fewer statements or shorter resource lists.",
    )


def proposal_error(problems: list[str]) -> str:
    shown = [f"- {p[:200]}" for p in problems[:8]]
    more = f"- and {len(problems) - 8} more" if len(problems) > 8 else None
    return _lines("That lease wasn't accepted:", *shown, more, "Fix it and send it again.")


def frozen(by: str, revoked_count: int) -> str:
    return _lines(
        f"FROZEN by {by}: {revoked_count} active lease(s) revoked.",
        "New leases are blocked until @keymaster unfreeze.",
    )


def unfrozen(by: str) -> str:
    return f"Unfrozen by {by}. New leases can be submitted again."
