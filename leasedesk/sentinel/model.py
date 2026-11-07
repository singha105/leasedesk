"""ProposalView: a lenient reading of a lease proposal.

keymaster's strict ``LeaseProposal`` refuses ``NotAction``, ``Principal``, ``aws:CurrentTime``
and resource ``*`` outright. sentinel has to say *why* a proposal is dangerous, so it reads the
same JSON leniently, keeps unknown keys, and lets the analyzers turn each problem into a
finding. Only a shape it can't read at all (not an object, no statement list) becomes a single
``struct-unreadable`` BLOCK.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from leasedesk.common.schemas import Finding, canonical_hash
from leasedesk.sentinel.findings import make, register

register(
    "struct-unreadable",
    "BLOCK",
    "The proposal can't be read as a lease",
    "https://github.com/singha105/leasedesk/blob/main/docs/SPEC.md",
    "structure",
)


def _as_list(v: Any) -> Any:
    return [v] if isinstance(v, str) else v


class StatementView(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)
    sid: str = ""
    actions: list[str] = Field(default_factory=list)
    resources: list[str] = Field(default_factory=list)
    conditions: dict[str, Any] | None = None

    _lists = field_validator("actions", "resources", mode="before")(_as_list)

    def extra_keys(self) -> list[str]:
        return sorted((self.model_extra or {}).keys())

    def extra(self) -> dict[str, Any]:
        return dict(self.model_extra or {})


class ProposalView(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)
    kind: Any = None
    version: Any = None
    requester_hint: Any = ""
    summary: str = ""
    justification: str = ""
    duration_minutes: int = 0
    statements: list[StatementView]

    @classmethod
    def parse(cls, raw: Any) -> tuple[ProposalView | None, list[Finding]]:
        try:
            return cls.model_validate(raw), []
        except ValidationError as exc:
            first = exc.errors()[0]
            loc = ".".join(str(x) for x in first["loc"]) or "proposal"
            detail = f"{loc}: {first['msg']}"
            return None, [
                make(
                    "struct-unreadable",
                    None,
                    f"The lease block isn't a readable proposal ({detail}).",
                    "Send one lease_proposal object with a list of statements "
                    "(sid, actions, resources).",
                )
            ]

    def extra_keys(self) -> list[str]:
        return sorted((self.model_extra or {}).keys())

    def hash(self) -> str:
        return canonical_hash(self.model_dump(mode="json"))
