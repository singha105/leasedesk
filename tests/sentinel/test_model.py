"""ProposalView reads proposals the strict schema refuses, so sentinel can explain why."""

from __future__ import annotations

from leasedesk.sentinel.model import ProposalView

from .conftest import BUCKET, raw, stmt


def test_valid_proposal_parses() -> None:
    v, problems = ProposalView.parse(raw())
    assert v is not None and not problems
    assert [s.sid for s in v.statements] == ["ReadInvoiceExports", "ListInvoiceExports"]


def test_unknown_keys_are_kept_for_structure() -> None:
    v, _ = ProposalView.parse(raw(stmt("S", ["s3:GetObject"], [BUCKET], NotAction=["iam:*"])))
    assert v is not None and v.statements[0].extra_keys() == ["NotAction"]


def test_string_action_and_resource_are_lists() -> None:
    v, _ = ProposalView.parse(raw({"sid": "S", "actions": "s3:GetObject", "resources": "*"}))
    assert v is not None
    assert v.statements[0].actions == ["s3:GetObject"] and v.statements[0].resources == ["*"]


def test_unreadable_shapes_become_one_block_finding() -> None:
    for bad in ["not a dict", {"statements": "nope"}, {"statements": [1, 2]}, None]:
        v, problems = ProposalView.parse(bad)
        assert v is None
        assert [p.rule for p in problems] == ["struct-unreadable"]
        assert problems[0].severity == "BLOCK"


def test_catalog_template_parses() -> None:
    from pathlib import Path

    from leasedesk.sentinel.catalog import parse_catalog_text

    text = Path("switch/docs/resource-catalog.md").read_text()
    catalog = parse_catalog_text(text)
    assert [j.arn for j in catalog.crown_jewels()] == ["arn:aws:s3:::leasedesk-demo-prod-secrets-*"]
