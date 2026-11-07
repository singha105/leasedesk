"""Review a proposal offline: ``python -m leasedesk.sentinel.cli review proposal.json``.

Mock mode, no Switch and no AWS call. Prints the same summary and verdict block sentinel would
post (for lease ``LS-0000``), then the corrected lease when there is one. Exit 0 once a review
is printed (whatever the risk), 2 when a file can't be read.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import yaml

from leasedesk.common.fences import FenceError, extract_one
from leasedesk.common.logging import configure_logging
from leasedesk.common.policy import parse_policy_text
from leasedesk.sentinel.catalog import parse_catalog_text
from leasedesk.sentinel.context import MOTO_ACCOUNT, ReviewContext
from leasedesk.sentinel.review import review
from leasedesk.sentinel.text import corrected_message, verdict_message

LEASE_ID = "LS-0000"
EXIT_UNREADABLE = 2


def _proposal(path: Path) -> Any:
    text = path.read_text()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return extract_one(text, "lease").data  # a chat message with a ```lease block


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m leasedesk.sentinel.cli",
        description="Review a lease proposal offline (no Switch, no AWS).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    cmd = sub.add_parser("review", help="review a lease proposal JSON file")
    cmd.add_argument("proposal", type=Path, help="lease_proposal JSON, or text with a lease block")
    cmd.add_argument("--policy", type=Path, default=Path("switch/docs/access-policy.yaml"))
    cmd.add_argument("--catalog", type=Path, default=Path("switch/docs/resource-catalog.md"))
    cmd.add_argument("--account", default=MOTO_ACCOUNT, help="the AWS account leases are for")
    args = parser.parse_args(argv)
    configure_logging("WARNING")
    try:
        raw = _proposal(args.proposal)
    except (OSError, FenceError) as exc:
        print(f"can't read the proposal {args.proposal}: {exc}", file=sys.stderr)
        return EXIT_UNREADABLE
    try:
        policy = parse_policy_text(args.policy.read_text())
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"can't read access-policy {args.policy}: {exc}", file=sys.stderr)
        return EXIT_UNREADABLE
    try:
        catalog = parse_catalog_text(args.catalog.read_text())
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"can't read resource-catalog {args.catalog}: {exc}", file=sys.stderr)
        return EXIT_UNREADABLE
    ctx = ReviewContext(policy=policy, catalog=catalog, account_id=args.account)
    result = asyncio.run(review(raw, ctx))
    if result.proposal_hash is None:
        print(f"{LEASE_ID} · BLOCK: {result.findings[0].detail}")
        return 0
    print(verdict_message(LEASE_ID, result))
    corrected = corrected_message(LEASE_ID, result)
    if corrected is not None:
        print()
        print(corrected)
    return 0


if __name__ == "__main__":
    sys.exit(main())
