"""Pull lease, verdict and review blocks out of chat text.

A block is a fenced code block tagged ``lease``, ``verdict`` or ``review``, or tagged ``json``
(or untagged) whose ``kind`` names one of them. Input over 16 KB is refused before parsing.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel

MAX_INPUT_BYTES = 16 * 1024
BlockKind = Literal["lease", "verdict", "review"]
_TAGS: dict[str, BlockKind] = {"lease": "lease", "verdict": "verdict", "review": "review"}
_KINDS: dict[str, BlockKind] = {
    "lease_proposal": "lease",
    "lease_verdict": "verdict",
    "lease_review_request": "review",
}
_FENCE = re.compile(r"```[ \t]*([A-Za-z0-9_-]*)[ \t]*\r?\n(.*?)```", re.DOTALL)


class FenceError(ValueError):
    pass


class InputTooLarge(FenceError):
    pass


class NoBlock(FenceError):
    pass


class TooManyBlocks(FenceError):
    pass


class FencedBlock(BaseModel):
    tag: str
    kind: BlockKind
    data: dict[str, Any]


def _parse(tag: str, body: str) -> Any:
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise FenceError(
            f"the {tag} block isn't valid JSON: line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from None


def extract_blocks(text: str) -> list[FencedBlock]:
    if len(text.encode()) > MAX_INPUT_BYTES:
        raise InputTooLarge(f"message is over {MAX_INPUT_BYTES // 1024} KB; send a smaller block")
    blocks: list[FencedBlock] = []
    for m in _FENCE.finditer(text):
        tag, body = m.group(1).lower(), m.group(2)
        if tag in _TAGS:
            data = _parse(tag, body)
            if not isinstance(data, dict):
                raise FenceError(f"the {tag} block must hold one JSON object")
            blocks.append(FencedBlock(tag=tag, kind=_TAGS[tag], data=data))
        elif tag in ("json", ""):
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict) and data.get("kind") in _KINDS:
                blocks.append(FencedBlock(tag=tag or "json", kind=_KINDS[data["kind"]], data=data))
    return blocks


def extract_one(text: str, kind: BlockKind) -> FencedBlock:
    found = [b for b in extract_blocks(text) if b.kind == kind]
    if not found:
        raise NoBlock(f"no ```{kind} block found")
    if len(found) > 1:
        raise TooManyBlocks(f"one {kind} block per message, please (found {len(found)})")
    return found[0]
