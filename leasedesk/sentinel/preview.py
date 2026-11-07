"""The policy keymaster would write for a proposal, with fixed placeholder times.

Analyzers that need real IAM JSON (parliament, Access Analyzer) judge this preview, made with
keymaster's own ``render_policy``. The window is a fixed date so the preview, and so the
verdict, is the same every time the same proposal is reviewed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from leasedesk.common.render import render_policy
from leasedesk.sentinel.model import ProposalView

PLACEHOLDER_ID = "LS-0000"
PLACEHOLDER_START = datetime(2030, 1, 1, tzinfo=UTC)


def preview(p: ProposalView) -> dict[str, Any]:
    end = PLACEHOLDER_START + timedelta(minutes=max(p.duration_minutes, 1))
    return render_policy(p, PLACEHOLDER_ID, PLACEHOLDER_START, end)
