"""Person-written documents found directly on a room or inside an attached package (Part 4)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from leasedesk.switch.documents import person_document_fetcher
from leasedesk.switch.models import DocumentContent, References

AGENT = "9f1c0000-agent"


def doc(doc_id: str, name: str = "access-policy", by_agent: str | None = None) -> dict[str, Any]:
    return {"id": doc_id, "name": name, "scope": "library", "created_by_agent_id": by_agent}


def package(*docs: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "pkg-1",
        "name": "leasedesk-governance",
        "description": "d",
        "instructions": "i",
        "references": [],
        "documents": list(docs),
    }


class Session:
    def __init__(self, connection_id: str) -> None:
        self.connection_id = connection_id
        self.ready = asyncio.Event()
        self.ready.set()


class Ops:
    def __init__(self, refs: dict[str, Any]) -> None:
        self.refs = refs
        self.loaded: list[tuple[list[str], str]] = []

    async def list_references(self, *, connection_id: str) -> References:
        return References.model_validate(self.refs)

    async def load_internal_documents(
        self, ids: list[str], *, connection_id: str
    ) -> list[DocumentContent]:
        self.loaded.append((ids, connection_id))
        return [DocumentContent(id=i, content=f"content of {i}") for i in ids]


class Agent:
    def __init__(self, refs: dict[str, Any]) -> None:
        self.ops = Ops(refs)
        self.sessions = {"room-1": Session("conn-1")}


async def fetch(refs: dict[str, Any]) -> tuple[str, Agent]:
    agent = Agent(refs)
    text = await person_document_fetcher(lambda: agent, "access-policy")()  # type: ignore[arg-type,return-value]
    return text, agent


async def test_package_document_found() -> None:
    text, agent = await fetch({"documents": [], "packages": [package(doc("d-1"))]})
    assert text == "content of d-1"
    assert agent.ops.loaded == [(["d-1"], "conn-1")]


async def test_package_document_by_agent_ignored() -> None:
    with pytest.raises(LookupError, match="no person-written document"):
        await fetch({"documents": [], "packages": [package(doc("d-1", by_agent=AGENT))]})


async def test_same_doc_direct_and_in_package_is_one() -> None:
    text, _ = await fetch({"documents": [doc("d-1")], "packages": [package(doc("d-1"))]})
    assert text == "content of d-1"


async def test_two_different_docs_same_name_refused() -> None:
    with pytest.raises(LookupError, match="more than one"):
        await fetch({"documents": [doc("d-1")], "packages": [package(doc("d-2"))]})


def test_references_without_packages_key_parses() -> None:
    refs = References.model_validate({"documents": [doc("d-1")]})
    assert refs.packages == []
    assert [d.id for d in refs.all_documents()] == ["d-1"]
