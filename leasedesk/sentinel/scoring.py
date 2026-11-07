"""Turning findings into a risk, with arithmetic anyone can redo (docs/SCORING.md).

Any BLOCK finding -> BLOCK (score shown as n/a, 100 in the verdict). Otherwise
score = 40 per HIGH + 12 per MEDIUM + 3 per LOW + 1 per full 30 minutes of duration.
HIGH if any HIGH finding or score >= 40; MEDIUM if score >= 12; otherwise LOW. The verdict
caps the score at 100 (its schema maximum); ``raw`` keeps the uncapped sum for ``explain``.
"""

from __future__ import annotations

from dataclasses import dataclass

from leasedesk.common.schemas import Finding, Risk

POINTS = {"HIGH": 40, "MEDIUM": 12, "LOW": 3}
MINUTES_PER_POINT = 30
HIGH_AT = 40
MEDIUM_AT = 12
MAX_SCORE = 100


@dataclass(frozen=True)
class Scored:
    risk: Risk
    score: int
    raw: int
    arithmetic: str


def score(findings: list[Finding], duration_minutes: int) -> Scored:
    counts = {sev: sum(1 for f in findings if f.severity == sev) for sev in ("BLOCK", *POINTS)}
    if counts["BLOCK"]:
        n = counts["BLOCK"]
        return Scored("BLOCK", MAX_SCORE, MAX_SCORE, f"{n} BLOCK finding(s) → BLOCK (score n/a)")
    minutes = max(duration_minutes, 0) // MINUTES_PER_POINT
    raw = sum(POINTS[sev] * counts[sev] for sev in POINTS) + minutes
    parts = [f"{POINTS[sev]}x{counts[sev]} {sev}" for sev in POINTS if counts[sev]]
    parts.append(f"{minutes} ({duration_minutes} min / {MINUTES_PER_POINT})")
    risk: Risk
    if counts["HIGH"]:
        risk, why = "HIGH", "a HIGH finding"
    elif raw >= HIGH_AT:
        risk, why = "HIGH", f"{HIGH_AT} or more"
    elif raw >= MEDIUM_AT:
        risk, why = "MEDIUM", f"{MEDIUM_AT} or more"
    else:
        risk, why = "LOW", f"under {MEDIUM_AT}"
    arithmetic = f"{' + '.join(parts)} = {raw} → {risk} ({why})"
    return Scored(risk, min(raw, MAX_SCORE), raw, arithmetic)
