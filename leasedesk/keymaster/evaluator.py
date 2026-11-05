"""A deliberately small IAM policy evaluator, for ``simulate`` and tests.

It is NOT AWS's evaluator. It knows: Allow/Deny, Action and Resource with ``*``/``?`` globs,
an optional permissions boundary, and four condition operators (DateGreaterThan, DateLessThan,
StringEquals, StringLike). Any other operator, NotAction/NotResource, or a condition key missing
from the request context makes that statement not match (fail closed). It exists because moto
ignores aws:CurrentTime, so mock mode needs another way to show the time window working.
"""

from __future__ import annotations

from datetime import datetime
from fnmatch import fnmatchcase
from typing import Any, Literal

Decision = Literal["ALLOWED", "DENIED"]


def _list(v: Any) -> list[str]:
    if v is None:
        return []
    return [str(x) for x in v] if isinstance(v, list) else [str(v)]


def _when(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _condition_ok(op: str, key: str, wanted: list[str], context: dict[str, str]) -> bool:
    if key not in context:
        return False
    have = context[key]
    if op == "DateGreaterThan":
        return any(_when(have) > _when(w) for w in wanted)
    if op == "DateLessThan":
        return any(_when(have) < _when(w) for w in wanted)
    if op == "StringEquals":
        return have in wanted
    if op == "StringLike":
        return any(fnmatchcase(have, w) for w in wanted)
    return False


def _matches(st: dict[str, Any], action: str, resource: str, context: dict[str, str]) -> bool:
    if "NotAction" in st or "NotResource" in st:
        return False
    if not any(fnmatchcase(action.lower(), a.lower()) for a in _list(st.get("Action"))):
        return False
    if not any(fnmatchcase(resource, r) for r in _list(st.get("Resource"))):
        return False
    for op, pairs in (st.get("Condition") or {}).items():
        for key, wanted in pairs.items():
            if not _condition_ok(op, key, _list(wanted), context):
                return False
    return True


def _statements(doc: dict[str, Any]) -> list[dict[str, Any]]:
    st = doc.get("Statement", [])
    return st if isinstance(st, list) else [st]


def evaluate(
    identity_docs: list[dict[str, Any]],
    boundary: dict[str, Any] | None,
    action: str,
    resource: str,
    context: dict[str, str],
) -> Decision:
    docs = [*identity_docs, *([boundary] if boundary else [])]
    for doc in docs:
        for st in _statements(doc):
            if st.get("Effect") == "Deny" and _matches(st, action, resource, context):
                return "DENIED"
    allowed = any(
        st.get("Effect") == "Allow" and _matches(st, action, resource, context)
        for doc in identity_docs
        for st in _statements(doc)
    )
    if not allowed:
        return "DENIED"
    if boundary is not None and not any(
        st.get("Effect") == "Allow" and _matches(st, action, resource, context)
        for st in _statements(boundary)
    ):
        return "DENIED"
    return "ALLOWED"
