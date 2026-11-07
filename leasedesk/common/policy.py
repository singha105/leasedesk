"""Loading the access policy (YAML) from the access-policy document or a file.

A parse error never replaces a good policy: the last good one stays in force and the caller
posts a warning. ``status`` shows the policy hash so people can tell which version is live.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import ValidationError

from leasedesk.common.schemas import AccessPolicy

_FENCE = re.compile(r"```(?:ya?ml)?[ \t]*\r?\n(.*?)```", re.DOTALL | re.IGNORECASE)


class PolicyError(ValueError):
    pass


def parse_policy_text(text: str) -> AccessPolicy:
    m = _FENCE.search(text)
    body = m.group(1) if m else text
    try:
        data = yaml.safe_load(body)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 1}" if mark is not None else ""
        raise PolicyError(f"bad YAML{where}") from None
    if not isinstance(data, dict):
        raise PolicyError("the policy must be a YAML mapping")
    try:
        return AccessPolicy.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(x) for x in first["loc"])
        raise PolicyError(f"{loc}: {first['msg']} ({exc.error_count()} problem(s))") from None


@dataclass
class LoadResult:
    policy: AccessPolicy | None
    error: str | None
    changed: bool


class PolicyLoader:
    def __init__(self, fetch: Callable[[], Awaitable[str]]) -> None:
        self._fetch = fetch
        self.current: AccessPolicy | None = None
        self.last_error: str | None = None

    async def load(self) -> LoadResult:
        try:
            policy = parse_policy_text(await self._fetch())
        except (PolicyError, OSError, LookupError, ValueError) as exc:
            self.last_error = str(exc)
            return LoadResult(self.current, self.last_error, False)
        changed = self.current is None or policy.hash() != self.current.hash()
        if changed:
            self.current = policy
        self.last_error = None
        return LoadResult(self.current, None, changed)


def file_fetcher(path: Path) -> Callable[[], Awaitable[str]]:
    async def fetch() -> str:
        return await asyncio.to_thread(path.read_text)

    return fetch
