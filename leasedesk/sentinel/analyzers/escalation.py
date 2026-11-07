"""escalation: the lease's expanded action set against known privilege-escalation paths
(``data/escalation_paths.yaml``). Every action of a path present is BLOCK; paths one action
short are listed together in one MEDIUM note, so a single risky action doesn't stack up."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources

import yaml

from leasedesk.common.schemas import Finding, Severity
from leasedesk.sentinel.actions import display, expand
from leasedesk.sentinel.context import ReviewContext
from leasedesk.sentinel.findings import make, register
from leasedesk.sentinel.model import ProposalView

NAME = "escalation"
HTC = "https://hackingthe.cloud/aws/exploitation/iam_privilege_escalation/"


@dataclass(frozen=True)
class Path:
    id: str
    name: str
    requires: tuple[str, ...]
    severity: Severity
    reference: str

    @property
    def rule(self) -> str:
        return f"escalation-{self.id}"

    @property
    def needs(self) -> frozenset[str]:
        return frozenset(a.lower() for a in self.requires)

    @property
    def drop(self) -> str:
        """The action whose removal breaks the path (iam:PassRole when the path uses it)."""
        return "iam:PassRole" if "iam:passrole" in self.needs else self.requires[0]


def load_paths() -> list[Path]:
    text = resources.files("leasedesk.sentinel.data").joinpath("escalation_paths.yaml").read_text()
    data = yaml.safe_load(text)
    paths = [
        Path(e["id"], e["name"], tuple(e["requires"]), e["severity"], e["reference"])
        for e in data["paths"]
    ]
    for p in paths:
        register(p.rule, p.severity, p.name, p.reference, NAME)
    return paths


PATHS = load_paths()
register(
    "escalation-near-miss", "MEDIUM", "One action away from a privilege-escalation path", HTC, NAME
)


def analyze(p: ProposalView, ctx: ReviewContext) -> list[Finding]:
    by_sid: dict[str, str] = {}
    have: set[str] = set()
    for s in p.statements:
        for action in s.actions:
            for a in expand(action):
                have.add(a)
                by_sid.setdefault(a, s.sid)
    out: list[Finding] = []
    near: list[tuple[Path, str]] = []
    for path in PATHS:
        missing = path.needs - have
        if not missing:
            drop = path.drop
            sid = by_sid.get(sorted(path.needs)[0])
            rest = [a for a in path.requires if a != drop]
            keep = f"; keep only what the task needs from {', '.join(rest)}" if rest else ""
            out.append(
                make(
                    path.rule,
                    sid,
                    f"{' + '.join(path.requires)} lets the holder escalate: {path.name}.",
                    f"Drop {drop}{keep}.",
                )
            )
        elif len(path.requires) > 1 and len(missing) == 1:
            near.append((path, display(next(iter(missing)))))
    if near:
        present = sorted({display(a) for path, _ in near for a in path.needs & have})
        listed = ", ".join(f"{m} ({path.id})" for path, m in near[:8])
        more = f" and {len(near) - 8} more" if len(near) > 8 else ""
        out.append(
            make(
                "escalation-near-miss",
                by_sid.get(present[0].lower()),
                f"{', '.join(present)} is one action away from {len(near)} escalation "
                f"path(s): {listed}{more}.",
                f"Drop {', '.join(present)} unless the task can't be done without it.",
            )
        )
    return out
