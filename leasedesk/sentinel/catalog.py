"""The resource-catalog document: each AWS resource with its owner and sensitivity.

A person writes it in the Gateway (template: switch/docs/resource-catalog.md). Like the
access-policy, it is read only from a document no agent created. ARNs may end in ``*``; they
are matched against lease resources with IAM glob rules, so ``leasedesk-demo-prod-secrets-*``
covers the decoy bucket whatever its random suffix.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from leasedesk.common.schemas import canonical_hash

_FENCE = re.compile(r"```(?:ya?ml)?[ \t]*\r?\n(.*?)```", re.DOTALL | re.IGNORECASE)


class CatalogError(ValueError):
    pass


class CatalogEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    arn: str = Field(pattern=r"^arn:aws:")
    owner: str = ""
    sensitivity: Literal["internal", "crown-jewel"]
    sensitive_actions: list[str] = Field(default_factory=list)
    note: str = ""

    def covers(self) -> list[str]:
        """The ARN patterns this entry protects: an S3 bucket ARN also covers its objects."""
        if self.arn.startswith("arn:aws:s3:::") and "/" not in self.arn:
            return [self.arn, self.arn + "/*"]
        return [self.arn]


class Catalog(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1] = 1
    resources: list[CatalogEntry] = Field(default_factory=list)

    def crown_jewels(self) -> list[CatalogEntry]:
        return [r for r in self.resources if r.sensitivity == "crown-jewel"]

    def hash(self) -> str:
        return canonical_hash(self.model_dump(mode="json"))[7:19]


def parse_catalog_text(text: str) -> Catalog:
    m = _FENCE.search(text)
    body = m.group(1) if m else text
    try:
        data = yaml.safe_load(body)
    except yaml.YAMLError:
        raise CatalogError("bad YAML in resource-catalog") from None
    if not isinstance(data, dict):
        raise CatalogError("resource-catalog must hold a YAML mapping")
    try:
        return Catalog.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(x) for x in first["loc"])
        raise CatalogError(f"resource-catalog {loc}: {first['msg']}") from None


class CatalogLoader:
    """Keeps the last good catalog when a reload fails (same rule as the access-policy)."""

    def __init__(self, fetch: Callable[[], Awaitable[str]]) -> None:
        self._fetch = fetch
        self.current: Catalog | None = None
        self.last_error: str | None = None

    async def load(self) -> Catalog | None:
        try:
            self.current = parse_catalog_text(await self._fetch())
            self.last_error = None
        except (CatalogError, OSError, LookupError, ValueError) as exc:
            self.last_error = str(exc)
        return self.current


def catalog_file_fetcher(path: Path) -> Callable[[], Awaitable[str]]:
    async def fetch() -> str:
        return await asyncio.to_thread(path.read_text)

    return fetch
