"""Ruling 9 (Part 4): keymaster's lease replies all start "LS-n · STATE · ", so none is
written by hand: they go through leasedesk.common.format (fmt.head for one-liners)."""

from __future__ import annotations

import re
from pathlib import Path

KEYMASTER = Path(__file__).resolve().parents[2] / "leasedesk" / "keymaster"
HAND_WRITTEN = re.compile(r'f"\{(?:lease|fresh|done|lease_id)(?:\.id)?\} (?:is |· )')


def test_no_hand_written_lease_message_in_keymaster() -> None:
    hits = [
        f"{path.name}:{n}: {line.strip()}"
        for path in sorted(KEYMASTER.glob("*.py"))
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if HAND_WRITTEN.search(line) and "raise " not in line  # exceptions aren't chat
    ]
    assert hits == [], "use fmt.head(lease.id, lease.state, ...):\n" + "\n".join(hits)
