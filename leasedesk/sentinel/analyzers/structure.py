"""structure: only Allow; no NotAction, NotResource or Principal; no aws:CurrentTime; every
ARN well formed; and the shape keymaster's schema requires. Every violation is BLOCK."""

from __future__ import annotations

import re
from typing import Any

from leasedesk.common.schemas import ACTION_PATTERN, Finding, mentions_current_time
from leasedesk.sentinel.context import ReviewContext
from leasedesk.sentinel.findings import IAM_DOCS, make, register
from leasedesk.sentinel.model import ProposalView, StatementView

NAME = "structure"
SPEC = "https://github.com/singha105/leasedesk/blob/main/docs/SPEC.md"
register("struct-schema", "BLOCK", "Not the lease proposal shape keymaster accepts", SPEC, NAME)
register(
    "struct-not-allow",
    "BLOCK",
    "A statement that isn't Allow",
    f"{IAM_DOCS}/reference_policies_elements_effect.html",
    NAME,
)
register(
    "struct-not-action",
    "BLOCK",
    "NotAction grants everything except a list",
    f"{IAM_DOCS}/reference_policies_elements_notaction.html",
    NAME,
)
register(
    "struct-not-resource",
    "BLOCK",
    "NotResource grants every resource except a list",
    f"{IAM_DOCS}/reference_policies_elements_notresource.html",
    NAME,
)
register(
    "struct-principal",
    "BLOCK",
    "Principal belongs in resource policies, not a lease",
    f"{IAM_DOCS}/reference_policies_elements_principal.html",
    NAME,
)
register("struct-unknown-key", "BLOCK", "A key a lease statement can't have", SPEC, NAME)
register(
    "struct-current-time",
    "BLOCK",
    "The proposal sets aws:CurrentTime; keymaster owns the time window",
    f"{IAM_DOCS}/reference_policies_condition-keys.html#condition-keys-currenttime",
    NAME,
)
register(
    "struct-bad-arn",
    "BLOCK",
    "A resource that isn't a well-formed ARN",
    f"{IAM_DOCS}/reference-arns.html",
    NAME,
)

ARN = re.compile(r"^arn:aws:([a-z0-9-]+):([a-z0-9-]*):(\d{12}|aws|\*)?:(.+)$")
_ACTION = re.compile(ACTION_PATTERN)
_SID = re.compile(r"^[A-Za-z0-9]{1,48}$")
TOP_KEYS = {
    "kind",
    "version",
    "requester_hint",
    "summary",
    "justification",
    "duration_minutes",
    "statements",
}


def _norm(key: str) -> str:
    return key.lower().replace("_", "").rstrip("s")


def well_formed_arn(resource: str) -> bool:
    return resource == "*" or ARN.match(resource) is not None


def _keys(s: StatementView) -> list[Finding]:
    out: list[Finding] = []
    for key, value in s.extra().items():
        k = _norm(key)
        if k == "notaction":
            out.append(
                make(
                    "struct-not-action",
                    s.sid,
                    f"{key} grants every action except the ones listed.",
                    "Replace NotAction with the exact actions you need in actions.",
                )
            )
        elif k == "notresource":
            out.append(
                make(
                    "struct-not-resource",
                    s.sid,
                    f"{key} grants every resource except the ones listed.",
                    "Replace NotResource with the exact ARNs you need in resources.",
                )
            )
        elif k in ("principal", "notprincipal"):
            out.append(
                make(
                    "struct-principal",
                    s.sid,
                    f"{key} has no place in an identity policy lease.",
                    "Remove it; keymaster attaches the lease to your IAM user.",
                )
            )
        elif k == "effect" and str(value).lower() != "allow":
            out.append(
                make(
                    "struct-not-allow",
                    s.sid,
                    f"Effect {value!r}: leases only grant (Allow).",
                    "Remove the Effect key; keymaster writes Allow itself.",
                )
            )
        else:
            out.append(
                make(
                    "struct-unknown-key",
                    s.sid,
                    f"Statement key {key!r} isn't part of a lease.",
                    "Use only sid, actions, resources and conditions.",
                )
            )
    return out


def _schema(p: ProposalView) -> list[str]:
    problems: list[str] = []
    if p.kind != "lease_proposal" or p.version != 1:
        problems.append("kind must be lease_proposal and version 1")
    problems += [f"unknown top-level key {k!r}" for k in p.extra_keys() if k not in TOP_KEYS]
    if not 1 <= len(p.statements) <= 10:
        problems.append("a lease has 1 to 10 statements")
    if p.duration_minutes < 1:
        problems.append("duration_minutes must be a positive number")
    sids = [s.sid for s in p.statements]
    if len(set(sids)) != len(sids):
        problems.append("each statement needs a different sid")
    for s in p.statements:
        if not _SID.match(s.sid):
            problems.append(f"sid {s.sid!r} must be 1-48 letters or digits")
        if not s.actions or not s.resources:
            problems.append(f"{s.sid or 'a statement'} needs actions and resources")
        problems += [
            f"action {a!r} isn't service:Action" for a in s.actions if not _ACTION.match(a)
        ]
    return problems


def analyze(p: ProposalView, ctx: ReviewContext) -> list[Finding]:
    out: list[Finding] = []
    problems = _schema(p)
    if problems:
        out.append(
            make(
                "struct-schema",
                None,
                "; ".join(problems[:4]) + ".",
                "Fix the lease block so keymaster's schema accepts it.",
            )
        )
    for s in p.statements:
        out += _keys(s)
        cond: Any = s.conditions
        if mentions_current_time(cond) or mentions_current_time(s.extra()):
            out.append(
                make(
                    "struct-current-time",
                    s.sid,
                    "The statement sets an aws:CurrentTime condition.",
                    "Remove it; keymaster adds the start and expiry conditions.",
                )
            )
        bad = [r for r in s.resources if not well_formed_arn(r)]
        if bad:
            out.append(
                make(
                    "struct-bad-arn",
                    s.sid,
                    f"Not a well-formed ARN: {', '.join(bad[:3])}.",
                    "Use a full ARN such as arn:aws:s3:::bucket/prefix/*.",
                )
            )
    return out
