"""What the requester says the lease is for, read from the summary with a fixed keyword map.

This only picks the wording of a fix (which narrower actions to suggest). It never changes a
finding's rule, severity or the risk, so text in a proposal can't talk sentinel into anything.
The only text copied out of the summary is a path token that matches a strict pattern.
"""

from __future__ import annotations

import re
from typing import Literal

Purpose = Literal["read", "write", "unknown"]
_WRITE = re.compile(r"\b(write|upload|put|delete|remove|fix|update|change|copy to|restore)\b", re.I)
_READ = re.compile(
    r"\b(read|list|view|look|debug|inspect|download|check|audit|investigate)\b", re.I
)
_PATH = re.compile(r"(?<![\w/.-])([A-Za-z0-9._-]+/(?:[A-Za-z0-9._-]+/?)*)(?![\w/.-])")
MAX_PATH = 64


def purpose(summary: str) -> Purpose:
    if _WRITE.search(summary):
        return "write"
    if _READ.search(summary):
        return "read"
    return "unknown"


def path_token(text: str) -> str | None:
    """The first ``folder/sub/`` style token in the text, if one is there and short."""
    for m in _PATH.finditer(text):
        token = m.group(1)
        if len(token) <= MAX_PATH and "//" not in token and ".." not in token:
            return token if token.endswith("/") else token + "/"
    return None


READ_ACTIONS: dict[str, list[str]] = {
    "s3": ["s3:GetObject", "s3:ListBucket"],
    "logs": ["logs:FilterLogEvents", "logs:GetLogEvents"],
    "dynamodb": ["dynamodb:GetItem", "dynamodb:Query"],
    "lambda": ["lambda:GetFunction", "lambda:ListFunctions"],
}
WRITE_ACTIONS: dict[str, list[str]] = {
    "s3": ["s3:GetObject", "s3:PutObject", "s3:ListBucket"],
    "logs": ["logs:PutLogEvents"],
    "dynamodb": ["dynamodb:PutItem", "dynamodb:UpdateItem"],
}


def narrower(service: str, why: Purpose) -> list[str]:
    table = WRITE_ACTIONS if why == "write" else READ_ACTIONS
    return table.get(service.lower(), READ_ACTIONS.get(service.lower(), []))
