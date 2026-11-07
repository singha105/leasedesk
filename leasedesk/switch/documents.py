"""Reading person-written library documents (access-policy, resource-catalog), attached to a
room directly or inside a package (leasedesk-governance).

Only documents with ``created_by_agent_id`` null count, so no agent (including a
prompt-injected one) can write a document that keymaster or sentinel enforces.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from leasedesk.switch.errors import SwitchError

if TYPE_CHECKING:
    from leasedesk.switch.agent import SwitchAgent


def person_document_fetcher(
    agent: Callable[[], SwitchAgent], name: str
) -> Callable[[], Awaitable[str]]:
    """Read the person-written document ``name`` through any ready room connection."""

    async def fetch() -> str:
        a = agent()
        ready = [s for s in a.sessions.values() if s.ready.is_set()]
        if not ready:
            raise LookupError("not connected to any room yet")
        found: dict[str, str] = {}  # document id -> connection id to load it through
        try:
            for session in ready:
                refs = await a.ops.list_references(connection_id=session.connection_id)
                for d in refs.all_documents():
                    if d.name == name and d.created_by_agent_id is None:
                        found.setdefault(d.id, session.connection_id)
            if not found:
                raise LookupError(f"no person-written document named {name} is attached to me")
            if len(found) > 1:
                raise LookupError(f"more than one document is named {name}; keep one")
            ((doc_id, connection_id),) = found.items()
            loaded = await a.ops.load_internal_documents([doc_id], connection_id=connection_id)
        except SwitchError as exc:
            raise LookupError(f"couldn't read {name}: {exc.detail}") from exc
        if not loaded:
            raise LookupError(f"{name} came back empty")
        return loaded[0].content

    return fetch
