"""The corrected lease: the same proposal with only mechanical, narrowing changes.

Mechanical means no judgment is needed: replace ``s3:*`` (object ARNs get ``s3:GetObject``, plus
``s3:PutObject`` when the summary says the task writes; bucket ARNs get ``s3:ListBucket``), add an
``s3:prefix`` condition to ``s3:ListBucket``, drop named tamper, forbidden,
permissions-management and escalation actions, and shorten to 60 minutes. Every change removes
access, never adds it.

A BLOCK or HIGH finding outside ``FIXABLE`` (``*`` resources, crown jewels, another account, a
resource outside the allowed prefixes, a malformed statement, an AWS finding) means there is no
corrected lease: those need the requester to rethink the request. ``review`` re-reviews whatever
this returns and offers it only if it is valid, has no BLOCK or HIGH finding and is lower risk.
"""

from __future__ import annotations

from typing import Any

from leasedesk.common.globs import glob_intersects
from leasedesk.common.schemas import Finding, LeaseProposal
from leasedesk.sentinel.actions import access_level, is_wildcard
from leasedesk.sentinel.analyzers.escalation import PATHS
from leasedesk.sentinel.analyzers.scope import derived_prefix
from leasedesk.sentinel.analyzers.tamper import TAMPER
from leasedesk.sentinel.context import ReviewContext
from leasedesk.sentinel.model import ProposalView, StatementView
from leasedesk.sentinel.purpose import purpose

SHORT_MINUTES = 60
S3_ALL = "s3:*"
_ESCALATION = {p.rule: p for p in PATHS}
FIXABLE = {
    "wildcard-service",
    "wildcard-covers-tamper",
    "tamper-exposure",
    "forbidden-action",
    "access-permissions-management",
    "duration-over-max",
    *_ESCALATION,
}
_SHORTEN = {"duration-over-max", "duration-long", "write-long"}
_S3_OBJECT = "arn:aws:s3:::"


def mechanical(findings: list[Finding]) -> bool:
    """True when every BLOCK and HIGH finding is one a mechanical change can remove."""
    return all(f.rule in FIXABLE for f in findings if f.severity in ("BLOCK", "HIGH"))


def _dropped(view: ProposalView, findings: list[Finding], ctx: ReviewContext) -> set[str]:
    drop = {t.lower() for t in TAMPER}
    drop |= {_ESCALATION[f.rule].drop.lower() for f in findings if f.rule in _ESCALATION}
    forbidden = ctx.policy.forbidden_actions if ctx.policy else []
    for s in view.statements:
        for a in s.actions:
            if is_wildcard(a):
                continue
            if access_level(a) == "Permissions management" or any(
                glob_intersects(a, f) for f in forbidden
            ):
                drop.add(a.lower())
    return drop


def _sid(base: str, used: set[str]) -> str:
    for n in range(1, 100):
        sid = (base[: 48 - 4 - len(str(n))] + "List" + ("" if n == 1 else str(n)))[:48]
        if sid not in used:
            return sid
    return base  # unreachable in practice: a lease has at most 10 statements


def _split_s3_all(s: StatementView, write: bool, used: set[str]) -> list[dict[str, Any]] | None:
    """``s3:*`` on bucket and object ARNs -> one narrow statement per resource type."""
    if any(a.lower() != S3_ALL and not a.lower().startswith("s3:") for a in s.actions):
        return None
    objects = [r for r in s.resources if r.startswith(_S3_OBJECT) and "/" in r]
    buckets = [r for r in s.resources if r.startswith(_S3_OBJECT) and "/" not in r]
    if len(objects) + len(buckets) != len(s.resources):
        return None
    # Conditions only ever restrict, so each split statement keeps all of them.
    keep = {"conditions": s.conditions} if s.conditions else {}
    out: list[dict[str, Any]] = []
    if objects:
        acts = ["s3:GetObject", "s3:PutObject"] if write else ["s3:GetObject"]
        out.append({"sid": s.sid, "actions": acts, "resources": objects, **keep})
    if buckets:
        sid = _sid(s.sid, used) if objects else s.sid
        used.add(sid)
        out.append({"sid": sid, "actions": ["s3:ListBucket"], "resources": buckets, **keep})
    return out


def _narrow_objects(st: dict[str, Any], view: ProposalView) -> dict[str, Any]:
    """``bucket/*`` -> ``bucket/<folder>*`` when the lease or its summary names the folder."""
    out = []
    for r in st["resources"]:
        if r.startswith(_S3_OBJECT) and r.endswith("/*") and r.count("/") == 1:
            prefix = derived_prefix(view, r[:-2])
            if prefix and prefix != "*":
                r = f"{r[:-1]}{prefix}"
        out.append(r)
    return {**st, "resources": list(dict.fromkeys(out))}


def _with_prefix(st: dict[str, Any], view: ProposalView) -> dict[str, Any]:
    if not any(a.lower() == "s3:listbucket" for a in st["actions"]):
        return st
    cond: dict[str, dict[str, Any]] = {k: dict(v) for k, v in (st.get("conditions") or {}).items()}
    if any(k.lower() == "s3:prefix" for keys in cond.values() for k in keys):
        return st
    buckets = [r for r in st["resources"] if r.startswith(_S3_OBJECT) and "/" not in r]
    prefix = derived_prefix(view, buckets[0]) if buckets else None
    if not prefix:
        return st
    cond.setdefault("StringLike", {})["s3:prefix"] = [prefix]
    return {**st, "conditions": cond}


def corrected_lease(
    view: ProposalView, findings: list[Finding], ctx: ReviewContext
) -> dict[str, Any] | None:
    """The narrowed proposal as a lease block dict, or None when a fix needs judgment."""
    if not mechanical(findings) or view.extra_keys():
        return None
    if any(s.extra_keys() for s in view.statements):
        return None
    write = purpose(view.summary) == "write"
    drop = _dropped(view, findings, ctx)
    used = {s.sid for s in view.statements}
    statements: list[dict[str, Any]] = []
    for s in view.statements:
        if any(a.lower() == S3_ALL for a in s.actions):
            split = _split_s3_all(s, write, used)
            if split is None:
                return None
            statements += split
            continue
        actions = [a for a in s.actions if a.lower() not in drop]
        if actions:
            st: dict[str, Any] = {"sid": s.sid, "actions": actions, "resources": list(s.resources)}
            if s.conditions:
                st["conditions"] = s.conditions
            statements.append(st)
    if not statements:
        return None
    duration = view.duration_minutes
    if any(f.rule in _SHORTEN for f in findings):
        duration = min(duration, SHORT_MINUTES)
    draft = {
        "kind": view.kind,
        "version": view.version,
        "requester_hint": view.requester_hint,
        "summary": view.summary,
        "justification": view.justification,
        "duration_minutes": duration,
        "statements": [_with_prefix(_narrow_objects(st, view), view) for st in statements],
    }
    try:
        return LeaseProposal.model_validate(draft).model_dump(mode="json", exclude_none=True)
    except ValueError:
        return None


def _names(actions: list[str], limit: int = 4) -> str:
    shown = ", ".join(actions[:limit])
    return shown + (f" and {len(actions) - limit} more" if len(actions) > limit else "")


def describe_changes(view: ProposalView, fixed: dict[str, Any]) -> list[str]:
    """What the corrected lease changed, in the words the thread shows."""
    before = list(dict.fromkeys(a for s in view.statements for a in s.actions))
    after = list(dict.fromkeys(a for s in fixed["statements"] for a in s["actions"]))
    removed = [a for a in before if a.lower() not in {x.lower() for x in after}]
    added = [a for a in after if a.lower() not in {x.lower() for x in before}]
    changes: list[str] = []
    if added:
        changes.append(f"replaces {_names(removed)} with {_names(added)}")
    elif removed:
        changes.append(f"drops {_names(removed)}")
    resources = {r for s in view.statements for r in s.resources}
    for s in fixed["statements"]:
        for r in s["resources"]:
            wide = next(
                (o for o in resources if o.endswith("/*") and r != o and r.startswith(o[:-1])),
                None,
            )
            if wide is not None and r not in resources:
                name = wide.removeprefix(_S3_OBJECT)
                changes.append(f"narrows {name} to {r[len(wide) - 1 :]}")
    had = {s.sid: s.conditions or {} for s in view.statements}
    for s in fixed["statements"]:
        prefix = (s.get("conditions") or {}).get("StringLike", {}).get("s3:prefix")
        old = had.get(s["sid"], {}).get("StringLike", {}).get("s3:prefix")
        if prefix and prefix != old:
            changes.append(f"adds an s3:prefix condition ({', '.join(prefix)})")
            break
    if fixed["duration_minutes"] < view.duration_minutes:
        changes.append(f"shortens {view.duration_minutes} to {fixed['duration_minutes']} min")
    return changes
