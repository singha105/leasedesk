"""access_level: every action (wildcards expanded with parliament's data) classified by AWS
access level. One finding per statement at its highest level: Permissions management HIGH,
Write or Tagging MEDIUM, Read or List LOW. A service wildcard (``s3:*``) or ``*`` is HIGH, with
a fix naming narrower actions that fit the stated purpose."""

from __future__ import annotations

from leasedesk.common.schemas import Finding
from leasedesk.sentinel.actions import LEVEL_RANK, access_level, display, expand, is_wildcard
from leasedesk.sentinel.context import ReviewContext
from leasedesk.sentinel.findings import ACCESS_LEVELS, LEAST_PRIVILEGE, make, register
from leasedesk.sentinel.model import ProposalView, StatementView
from leasedesk.sentinel.purpose import narrower, purpose

NAME = "access_level"
register(
    "access-permissions-management",
    "HIGH",
    "Permissions-management actions (they change who can do what)",
    ACCESS_LEVELS,
    NAME,
)
register("access-write", "MEDIUM", "Write actions", ACCESS_LEVELS, NAME)
register("access-tagging", "MEDIUM", "Tagging actions", ACCESS_LEVELS, NAME)
register("access-read-list", "LOW", "Read and List actions only", ACCESS_LEVELS, NAME)
register("wildcard-service", "HIGH", "Every action of a service (svc:*)", LEAST_PRIVILEGE, NAME)
register("wildcard-all", "HIGH", "Every action in AWS (*)", LEAST_PRIVILEGE, NAME)

RULE_BY_LEVEL = {
    "Permissions management": "access-permissions-management",
    "Write": "access-write",
    "Tagging": "access-tagging",
    "Read": "access-read-list",
    "List": "access-read-list",
}
_SWAPS = (("Put", "Get"), ("Delete", "Get"), ("Create", "Describe"), ("Update", "Describe"))


def _names(actions: set[str], limit: int = 4) -> str:
    shown = sorted(display(a) for a in actions)
    more = f" and {len(shown) - limit} more" if len(shown) > limit else ""
    return ", ".join(shown[:limit]) + more


def _read_alternative(actions: set[str]) -> str:
    alts = []
    for a in sorted(actions):
        name = display(a)
        service, _, op = name.partition(":")
        for old, new in _SWAPS:
            if op.startswith(old) and access_level(f"{service}:{new}{op[len(old) :]}"):
                alts.append(f"{service}:{new}{op[len(old) :]}")
                break
    return f"; if you only need to read, ask for {', '.join(dict.fromkeys(alts))}" if alts else ""


def _statement(s: StatementView, p: ProposalView) -> list[Finding]:
    out: list[Finding] = []
    levels: dict[str, set[str]] = {}
    why = purpose(p.summary)
    for action in s.actions:
        for a in expand(action):
            level = access_level(a)
            if level:
                levels.setdefault(level, set()).add(a)
        if action == "*":
            out.append(
                make(
                    "wildcard-all",
                    s.sid,
                    "Action * grants every action in every AWS service.",
                    "Name the actions the task needs "
                    "(for reading S3: s3:GetObject and s3:ListBucket).",
                )
            )
        elif action.endswith(":*"):
            service = action.split(":", 1)[0]
            names = narrower(service, why)
            hint = " and ".join(names) if names else f"the {service} actions the task needs"
            out.append(
                make(
                    "wildcard-service",
                    s.sid,
                    f"{action} grants all {len(expand(action))} {service} actions.",
                    f"Replace {action} with {hint}.",
                )
            )
    if not levels:
        return out
    top = min(levels, key=lambda lv: LEVEL_RANK[lv])
    rule = RULE_BY_LEVEL[top]
    acts = (
        levels[top]
        if rule != "access-read-list"
        else levels.get("Read", set()) | levels.get("List", set())
    )
    wild = any(is_wildcard(a) for a in s.actions)
    label = f"{top} actions" + (" (through a wildcard)" if wild else "")
    if rule == "access-permissions-management":
        fix = f"Drop {_names(acts)}; a lease is for using data, not for changing permissions."
    elif rule == "access-write":
        alt = _read_alternative(acts)
        fix = f"Keep writes to the exact objects and 60 minutes or less{alt}."
    elif rule == "access-tagging":
        fix = f"Drop {_names(acts)} unless tagging is the task."
    else:
        fix = None
    out.append(make(rule, s.sid, f"{label}: {_names(acts)}.", fix))
    return out


def analyze(p: ProposalView, ctx: ReviewContext) -> list[Finding]:
    out: list[Finding] = []
    for s in p.statements:
        out += _statement(s, p)
    return out
