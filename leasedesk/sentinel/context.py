"""What a review is judged against: the access-policy, the resource-catalog, the account."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from leasedesk.common.schemas import AccessPolicy
from leasedesk.sentinel.catalog import Catalog

MOTO_ACCOUNT = "123456789012"


@dataclass(frozen=True)
class ReviewContext:
    policy: AccessPolicy | None
    catalog: Catalog | None
    account_id: str = MOTO_ACCOUNT
    aws_mode: Literal["mock", "real"] = "mock"
    paid_checks: bool = False

    def key(self) -> str:
        """Everything besides the proposal that can change a verdict (the cache key part)."""
        policy = self.policy.hash() if self.policy else "-"
        catalog = self.catalog.hash() if self.catalog else "-"
        return f"{policy}|{catalog}|{self.account_id}|{self.aws_mode}|{self.paid_checks}"
