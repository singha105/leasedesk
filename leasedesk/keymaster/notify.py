"""How the scheduler and reconciler talk to rooms: implemented by the keymaster service."""

from __future__ import annotations

from typing import Protocol

from leasedesk.keymaster.store import Lease


class Notifier(Protocol):
    async def notify(self, lease: Lease | None, text: str) -> None:
        """Post in the lease's thread, or in the access room when there is no lease."""

    async def ledger(self, lease: Lease) -> None:
        """Write the lease's row to the lease-ledger document and logs/ledger.jsonl."""
