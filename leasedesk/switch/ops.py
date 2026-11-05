"""OpsClient: every Switch HTTP call an agent makes goes through here.

Ops take unwrapped arguments and answer ``{"result": ...}``; errors are FastAPI
``{"detail": ...}``. Reads are retried on 5xx/transport errors; writes never are, because a
retried post_message could post twice.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from types import TracebackType
from typing import Any

import httpx

from leasedesk.common.logging import get_logger, redact_text
from leasedesk.switch.credentials import AgentCredentials
from leasedesk.switch.errors import SwitchError, Unavailable, error_for
from leasedesk.switch.models import (
    ContextThread,
    DocumentContent,
    Participant,
    References,
    RoomInfo,
    RoomSummary,
)

log = get_logger(__name__)

READ_OPS = frozenset(
    {
        "list_rooms",
        "read_context",
        "list_participants",
        "list_references",
        "load_internal_documents",
        "list_roles",
        "get_my_context",
    }
)
RETRY_DELAYS = (0.3, 0.9)


def _detail(r: httpx.Response, key: str) -> str:
    try:
        body = r.json()
        text = str(body.get("detail", body)) if isinstance(body, dict) else str(body)
    except ValueError:
        text = r.text
    return redact_text(text.replace(key, "[REDACTED]"))[:500]


class OpsClient:
    def __init__(
        self,
        base_url: str,
        creds: AgentCredentials,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 20.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.creds = creds
        self.agent_id = creds.id
        self._key = creds.api_key.get_secret_value()
        self._sleep = sleep
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {self._key}"},
            transport=transport,
            timeout=timeout,
        )

    async def __aenter__(self) -> OpsClient:
        return self

    async def __aexit__(
        self,
        _t: type[BaseException] | None,
        _e: BaseException | None,
        _tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(
        self, label: str, method: str, url: str, *, retry: bool, **kw: Any
    ) -> httpx.Response:
        attempts = 1 + (len(RETRY_DELAYS) if retry else 0)
        last: SwitchError | None = None
        for attempt in range(attempts):
            try:
                r = await self._http.request(method, url, **kw)
            except httpx.TransportError as exc:
                last = Unavailable(0, type(exc).__name__, label)
            else:
                if r.status_code < 400:
                    log.debug("switch_call", op=label, status=r.status_code)
                    return r
                last = error_for(r.status_code, _detail(r, self._key), label)
                if not isinstance(last, Unavailable):
                    log.info("switch_call_failed", op=label, status=r.status_code)
                    raise last
            log.warning("switch_call_unavailable", op=label, attempt=attempt + 1)
            if attempt + 1 < attempts:
                await self._sleep(RETRY_DELAYS[attempt])
        assert last is not None  # noqa: S101 - loop always sets it
        raise last

    async def call(self, op: str, *, connection_id: str | None = None, **args: Any) -> Any:
        headers = {"X-Switch-Connection-Id": connection_id} if connection_id else {}
        r = await self._request(
            op,
            "POST",
            f"/agents/{self.agent_id}/ops/{op}",
            retry=op in READ_OPS,
            json=args,
            headers=headers,
        )
        body = r.json()
        return body.get("result") if isinstance(body, dict) else body

    # Typed wrappers ---------------------------------------------------------------------

    async def connect_to_room(self, room_id: str, *, connection_id: str) -> RoomInfo:
        res = await self.call(
            "connect_to_room",
            connection_id=connection_id,
            room_id=room_id,
            include_general_instructions=False,
        )
        return RoomInfo.model_validate(res)

    async def list_rooms(self, *, include_archived: bool = False) -> list[RoomSummary]:
        res = await self.call("list_rooms", include_archived=include_archived)
        return [RoomSummary.model_validate(x) for x in res or []]

    async def post_message(
        self, body: str, *, connection_id: str, thread_id: str | None = None
    ) -> str:
        args: dict[str, Any] = {"body": body}
        if thread_id:
            args["thread_id"] = thread_id
        res = await self.call("post_message", connection_id=connection_id, **args)
        return str(res.get("event_id", "")) if isinstance(res, dict) else ""

    async def send_targeted_message(
        self,
        body: str,
        *,
        connection_id: str,
        target_names: list[str] | None = None,
        target_roles: list[str] | None = None,
        thread_id: str | None = None,
    ) -> str:
        args: dict[str, Any] = {"body": body}
        if target_names:
            args["target_names"] = target_names
        if target_roles:
            args["target_roles"] = target_roles
        if thread_id:
            args["thread_id"] = thread_id
        res = await self.call("send_targeted_message", connection_id=connection_id, **args)
        return str(res.get("event_id", "")) if isinstance(res, dict) else ""

    async def read_context(
        self,
        room_id: str,
        *,
        limit: int = 50,
        since: str | None = None,
        before: str | None = None,
        connection_id: str | None = None,
    ) -> list[ContextThread]:
        args: dict[str, Any] = {"room_id": room_id, "limit": limit}
        if since:
            args["since"] = since
        if before:
            args["before"] = before
        res = await self.call("read_context", connection_id=connection_id, **args)
        threads = res.get("threads", []) if isinstance(res, dict) else []
        return [ContextThread.model_validate(t) for t in threads]

    async def list_participants(self, *, connection_id: str) -> list[Participant]:
        res = await self.call("list_participants", connection_id=connection_id)
        return [Participant.model_validate(p) for p in res or []]

    async def list_references(self, *, connection_id: str) -> References:
        return References.model_validate(
            await self.call("list_references", connection_id=connection_id) or {}
        )

    async def load_internal_documents(
        self, ids: Iterable[str], *, connection_id: str
    ) -> list[DocumentContent]:
        res = await self.call("load_internal_documents", connection_id=connection_id, ids=list(ids))
        return [DocumentContent.model_validate(d) for d in res or []]

    async def create_room_document(
        self, name: str, description: str, instructions: str, content: str, *, connection_id: str
    ) -> str:
        res = await self.call(
            "create_room_document",
            connection_id=connection_id,
            name=name,
            description=description,
            instructions=instructions,
            content=content,
        )
        return str(res["document_id"])

    async def update_room_document(
        self,
        document_id: str,
        *,
        connection_id: str,
        content: str | None = None,
        description: str | None = None,
        instructions: str | None = None,
        name: str | None = None,
    ) -> None:
        fields = {
            "content": content,
            "description": description,
            "instructions": instructions,
            "name": name,
        }
        await self.call(
            "update_room_document",
            connection_id=connection_id,
            document_id=document_id,
            **{k: v for k, v in fields.items() if v is not None},
        )

    async def assume_role(self, role: str, *, connection_id: str) -> dict[str, Any]:
        res = await self.call("assume_role", connection_id=connection_id, role=role)
        return res if isinstance(res, dict) else {"result": res}

    async def release_role(self, *, connection_id: str) -> None:
        await self.call("release_role", connection_id=connection_id)

    async def list_roles(self, *, connection_id: str) -> Any:
        return await self.call("list_roles", connection_id=connection_id)

    async def define_role(
        self, name: str, instructions: str, *, connection_id: str, exclusive: bool = False
    ) -> Any:
        return await self.call(
            "define_role",
            connection_id=connection_id,
            name=name,
            instructions=instructions,
            exclusive=exclusive,
        )

    async def link_rooms(
        self, source_room_id: str, target_room_id: str, label: str, *, connection_id: str
    ) -> Any:
        return await self.call(
            "link_rooms",
            connection_id=connection_id,
            source_room_id=source_room_id,
            target_room_id=target_room_id,
            label=label,
        )

    async def invite_agent_to_room(
        self, agent_name: str, room_id: str, *, connection_id: str
    ) -> Any:
        return await self.call(
            "invite_agent_to_room",
            connection_id=connection_id,
            agent_name=agent_name,
            room_id=room_id,
        )

    async def get_my_context(self) -> dict[str, Any]:
        res = await self.call("get_my_context")
        return res if isinstance(res, dict) else {}

    async def list_ops(self) -> Any:
        r = await self._request("list_ops", "GET", f"/agents/{self.agent_id}/ops", retry=True)
        return r.json()

    async def post_direct(self, room_id: str, content: str) -> str:
        """POST /agents/{id}/message: needs no connection, but has no thread_id."""
        r = await self._request(
            "message",
            "POST",
            f"/agents/{self.agent_id}/message",
            retry=False,
            json={"room_id": room_id, "content": content},
        )
        return str(r.json().get("event_id", ""))

    async def download_media(self, room_id: str, mxc: str) -> bytes:
        r = await self._request(
            "media",
            "GET",
            f"/agents/{self.agent_id}/rooms/{room_id}/media",
            retry=True,
            params={"mxc": mxc},
        )
        return r.content
