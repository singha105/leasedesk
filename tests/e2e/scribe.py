"""A scripted stand-in for scribe (the Claude agent): it posts what the scribe brief says to
post, with no model behind it."""

from __future__ import annotations

import json
from typing import Any

from .fakeswitch import FakeSwitch


def _block(kind: str, proposal: dict[str, Any]) -> str:
    return f"```{kind}\n{json.dumps(proposal, indent=2)}\n```"


class ScribeStandIn:
    def __init__(self, sw: FakeSwitch) -> None:
        self.sw = sw

    async def draft(
        self, room_id: str, thread_id: str, asker: str, proposal: dict[str, Any]
    ) -> None:
        """Brief step 5: `@keymaster submit` plus exactly one lease block, in the asker's
        thread, naming the asker in requester_hint."""
        lease = {**proposal, "requester_hint": asker}
        await self.sw.post_as(
            "scribe", room_id, f"@keymaster submit\n{_block('lease', lease)}", thread_id
        )

    async def check(self, room_id: str, thread_id: str, proposal: dict[str, Any]) -> None:
        """Brief step 4: one `@sentinel check` round before submitting."""
        await self.sw.post_as(
            "scribe", room_id, f"@sentinel check\n{_block('lease', proposal)}", thread_id
        )
