from __future__ import annotations

import json

import pytest

from leasedesk.common.fences import (
    FenceError,
    InputTooLarge,
    NoBlock,
    TooManyBlocks,
    extract_blocks,
    extract_one,
)

from .conftest import proposal_dict


def fence(tag: str, data: object) -> str:
    return f"```{tag}\n{json.dumps(data, indent=2)}\n```"


def test_lease_fence() -> None:
    text = "@keymaster submit\n" + fence("lease", proposal_dict())
    block = extract_one(text, "lease")
    assert block.tag == "lease"
    assert block.data["summary"].startswith("Read October")


def test_json_fence_with_kind() -> None:
    blocks = extract_blocks("here:\n" + fence("json", proposal_dict()))
    assert [b.kind for b in blocks] == ["lease"]


def test_json_fence_without_known_kind_ignored() -> None:
    assert extract_blocks(fence("json", {"kind": "other"}) + fence("python", {"a": 1})) == []


def test_verdict_and_review_tags() -> None:
    text = fence("verdict", {"kind": "lease_verdict"}) + "\n" + fence("review", {"kind": "x"})
    assert [b.kind for b in extract_blocks(text)] == ["verdict", "review"]


def test_two_proposals_rejected() -> None:
    text = fence("lease", proposal_dict()) + "\n" + fence("json", proposal_dict())
    with pytest.raises(TooManyBlocks):
        extract_one(text, "lease")


def test_no_block() -> None:
    with pytest.raises(NoBlock):
        extract_one("@keymaster submit please", "lease")


def test_16kb_cap() -> None:
    with pytest.raises(InputTooLarge):
        extract_blocks("```lease\n{}\n```" + "x" * (16 * 1024))


def test_malformed_json_error_has_line_col() -> None:
    text = '```lease\n{\n  "kind": "lease_proposal"\n  "version": 1\n}\n```'
    with pytest.raises(FenceError) as exc:
        extract_one(text, "lease")
    assert "line 3" in str(exc.value)
    assert "column" in str(exc.value)


def test_lease_fence_must_hold_an_object() -> None:
    with pytest.raises(FenceError, match="object"):
        extract_one("```lease\n[1, 2]\n```", "lease")
