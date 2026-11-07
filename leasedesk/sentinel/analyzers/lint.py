"""lint: parliament (Duo Labs) on the rendered preview, one statement at a time so every
finding maps to a sid. Malformed JSON, unknown actions or services and invalid ARNs are
BLOCK; a resource that doesn't fit an action, or a condition that doesn't apply to it, is
MEDIUM with parliament's explanation as the fix; anything else parliament says is LOW.
Resource ``*`` is left to ``scope``, which rates it HIGH."""

from __future__ import annotations

import difflib
import json
import warnings
from typing import Any

from leasedesk.common.schemas import Finding
from leasedesk.sentinel.actions import service_actions
from leasedesk.sentinel.context import ReviewContext
from leasedesk.sentinel.findings import PARLIAMENT, make, register
from leasedesk.sentinel.model import ProposalView
from leasedesk.sentinel.preview import preview

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    import parliament

NAME = "lint"
register("lint-malformed", "BLOCK", "parliament couldn't read the policy", PARLIAMENT, NAME)
register("lint-unknown-action", "BLOCK", "An action AWS doesn't have", PARLIAMENT, NAME)
register("lint-unknown-prefix", "BLOCK", "A service prefix AWS doesn't have", PARLIAMENT, NAME)
register("lint-invalid-arn", "BLOCK", "parliament says an ARN is malformed", PARLIAMENT, NAME)
register(
    "lint-resource-mismatch",
    "MEDIUM",
    "A resource that no action in the statement applies to",
    PARLIAMENT,
    NAME,
)
register(
    "lint-condition-mismatch",
    "MEDIUM",
    "A condition that doesn't apply to the action, so it restricts nothing",
    PARLIAMENT,
    NAME,
)
register("lint-other", "LOW", "Another parliament remark", PARLIAMENT, NAME)

BLOCKING = {
    "MALFORMED_JSON": "lint-malformed",
    "MALFORMED": "lint-malformed",
    "EXCEPTION": "lint-malformed",
    "UNKNOWN_ACTION": "lint-unknown-action",
    "UNKNOWN_PREFIX": "lint-unknown-prefix",
    "INVALID_ARN": "lint-invalid-arn",
}
CONDITION = {
    "UNKNOWN_CONDITION_FOR_ACTION",
    "MISMATCHED_TYPE",
    "MISMATCHED_TYPE_BUT_USABLE",
    "UNKNOWN_OPERATOR",
    "MISMATCHED_TYPE_OPERATION_TO_NULL",
}
SKIP = {"RESOURCE_STAR", "RESOURCE_EFFECTIVELY_STAR"}


def _suggest(action: str) -> str:
    service = action.split(":", 1)[0]
    close = difflib.get_close_matches(action, service_actions(service), n=1, cutoff=0.8)
    hint = f"; did you mean {close[0]}?" if close else "."
    return f"Use the exact IAM action name{hint}"


def _mismatch_fix(detail: Any) -> str:
    items = detail if isinstance(detail, list) else []
    parts = [
        f"{d.get('action')} needs a resource like {d.get('required_format')}"
        for d in items
        if isinstance(d, dict)
    ]
    return ("; ".join(parts) or str(detail)) + " (parliament)."


def _one(statement: dict[str, Any], sid: str) -> list[Finding]:
    policy = parliament.analyze_policy_string(
        json.dumps({"Version": "2012-10-17", "Statement": [statement]})
    )
    out: list[Finding] = []
    # parliament repeats a remark once per action it is about; one finding per kind is enough.
    others: dict[str, list[str]] = {}
    conditions: list[tuple[str, str]] = []  # one per action a wildcard covers; report one
    for f in policy.findings:
        issue = str(f.issue)
        if issue in SKIP:
            continue
        detail = f.detail if isinstance(f.detail, str) else json.dumps(f.detail)
        if issue in BLOCKING:
            rule = BLOCKING[issue]
            text = detail or issue
            fix = (
                _suggest(str(f.location.get("string", "")))
                if rule == "lint-unknown-action"
                else "Fix the statement so it is valid IAM."
            )
            out.append(make(rule, sid, f"parliament: {text}", fix))
        elif issue == "RESOURCE_MISMATCH":
            named = {str(a).lower() for a in statement["Action"]}
            items = f.detail if isinstance(f.detail, list) else []
            # For a wildcard, parliament lists every covered action with another resource
            # type; only actions named in the lease are worth a finding.
            mine = [
                d for d in items if isinstance(d, dict) and str(d.get("action")).lower() in named
            ]
            if not mine:
                continue
            names = ", ".join(str(d.get("action")) for d in mine[:3])
            out.append(
                make(
                    "lint-resource-mismatch",
                    sid,
                    f"parliament: no listed resource fits {names}.",
                    _mismatch_fix(mine),
                )
            )
        elif issue in CONDITION:
            enhanced = parliament.enhance_finding(f)
            conditions.append(
                (
                    _remark(enhanced.title, detail),
                    f"{enhanced.description or enhanced.title} Remove or correct the condition.",
                )
            )
        else:
            enhanced = parliament.enhance_finding(f)
            others.setdefault(issue, []).append(_remark(enhanced.title, detail))
    if conditions:
        more = f" (+{len(conditions) - 1} similar)" if len(conditions) > 1 else ""
        text, fix = conditions[0]
        out.append(make("lint-condition-mismatch", sid, f"parliament: {text}{more}", fix))
    for remarks in others.values():
        more = f" (+{len(remarks) - 1} similar)" if len(remarks) > 1 else ""
        out.append(make("lint-other", sid, f"parliament: {remarks[0]}{more}"))
    return out


def _remark(title: str, detail: str) -> str:
    """parliament's detail often repeats the title; say it once."""
    if detail.lower().startswith(title.lower()):
        return detail
    return f"{title} {detail}".strip()


def analyze(p: ProposalView, ctx: ReviewContext) -> list[Finding]:
    doc = preview(p)
    out: list[Finding] = []
    for statement, s in zip(doc["Statement"], p.statements, strict=True):
        out += _one(statement, s.sid)
    return out
