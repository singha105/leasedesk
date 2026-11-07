"""Who sentinel answers with a verdict that gates a lease.

Only a review request whose ``sender`` is keymaster's pinned participant id
(``LEASEDESK_KEYMASTER_ID``) gets a gating verdict. Everyone else, including a person or an
agent that copies keymaster's display name, gets an advisory answer with no verdict block.
Without a pinned id every answer is advisory, dev mode included: a name is never trusted.
"""

from __future__ import annotations

from typing import Literal

from leasedesk.config import Settings

Mode = Literal["gating", "advisory"]


def request_mode(sender: str, settings: Settings) -> Mode:
    pinned = settings.keymaster_id
    return "gating" if pinned and sender == pinned else "advisory"
