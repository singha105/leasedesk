from __future__ import annotations

from leasedesk.common.format import split_for_platform


def test_short_text_is_one_chunk() -> None:
    assert split_for_platform("hello") == ["hello"]


def test_splits_on_lines_under_limit() -> None:
    text = "\n".join(f"line {i:04d} " + "x" * 80 for i in range(60))
    chunks = split_for_platform(text, limit=2000)
    assert len(chunks) > 1
    assert all(len(c) <= 2000 for c in chunks)
    assert "\n".join(chunks) == text


def test_split_keeps_fence_whole() -> None:
    fence = "```lease\n" + "\n".join('  "a": "' + "y" * 60 + '",' for _ in range(15)) + "\n```"
    text = "intro " + "z" * 1500 + "\n" + fence + "\noutro"
    chunks = split_for_platform(text, limit=2000)
    assert any(fence in c for c in chunks)
    assert all(len(c) <= 2000 for c in chunks)


def test_one_huge_line_is_hard_split() -> None:
    chunks = split_for_platform("q" * 4500, limit=2000)
    assert [len(c) for c in chunks] == [2000, 2000, 500]
