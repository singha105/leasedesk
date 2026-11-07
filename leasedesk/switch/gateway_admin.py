"""The person-side Gateway API, signed in as the local server's admin.

A native local Switch server has no Gateway web page, so the owner steps (addressing, library
documents) go through these routes, as recorded in docs/PROTOCOL_NOTES.md. Credentials are read
from Console's switch-core.env or typed in; they are never printed or logged.
"""

from __future__ import annotations

import getpass
from pathlib import Path
from types import TracebackType
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

from leasedesk.common.envfile import read_env_file
from leasedesk.switch.errors import error_for

CONSOLE_ENV = (
    Path.home()
    / "Library/Application Support/switchdash/local-switch-server/native/run/switch-core.env"
)
AddressingMode = Literal["anyone", "owner_and_owner_agents", "owner_only"]
_RULE: dict[str, Any] = {
    "rooms": "*",
    "room_groups": "*",
    "users": [],
    "agents": [],
    "platform": False,
}


class GwRoom(BaseModel):
    id: str
    name: str
    archived: bool = False


class GwRole(BaseModel):
    name: str
    exclusive: bool = False
    held_by: list[str] = Field(default_factory=list)  # agent names, not ids


class GwRoomDetail(GwRoom):
    agent_ids: list[str] = Field(default_factory=list)
    roles: list[GwRole] = Field(default_factory=list)


class GwNamed(BaseModel):
    """An agent, package or template: what make doctor needs is the id and the name."""

    id: str
    name: str


class GwTemplate(GwNamed):
    official: bool = False
    version: int = 0


class GwDocument(GwNamed):
    created_by_agent_id: str | None = None
    content: str | None = None


async def server_health(base_url: str, *, transport: httpx.AsyncBaseTransport | None = None) -> int:
    """GET /health (no sign-in): the HTTP status."""
    async with httpx.AsyncClient(timeout=3, transport=transport) as c:
        r = await c.get(base_url.rstrip("/") + "/health")
        return r.status_code


def admin_login(prompt: bool = False) -> tuple[str, str]:
    if prompt:
        return input("Gateway admin email: ").strip(), getpass.getpass("Gateway admin password: ")
    env = read_env_file(CONSOLE_ENV)
    email, password = env.get("GATEWAY_ADMIN_EMAIL", ""), env.get("GATEWAY_ADMIN_PASSWORD", "")
    if not email or not password:
        raise SystemExit("Gateway admin login not found in Console's env file; use --prompt")
    return email, password


class GatewayAdmin:
    def __init__(
        self,
        base_url: str,
        *,
        email: str,
        password: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._email = email
        self._password = password
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/gateway", timeout=20, transport=transport
        )

    async def __aenter__(self) -> GatewayAdmin:
        r = await self._http.post(
            "/auth/login", json={"email": self._email, "password": self._password}
        )
        self._password = ""
        if r.status_code != 200:
            await self._http.aclose()
            raise error_for(r.status_code, "Gateway admin login failed", "gateway_login")
        return self

    async def __aexit__(
        self,
        _t: type[BaseException] | None,
        _e: BaseException | None,
        _tb: TracebackType | None,
    ) -> None:
        try:
            await self._http.post("/auth/logout")
        finally:
            await self._http.aclose()

    async def _send(self, label: str, method: str, url: str, body: Any = None) -> Any:
        r = await self._http.request(method, url, json=body)
        if r.status_code >= 400:
            raise error_for(r.status_code, r.text[:300], label)
        return r.json() if r.content else None

    async def set_addressing(self, agent_id: str, mode: AddressingMode) -> Any:
        policies: dict[str, Any] = {
            "anyone": None,
            "owner_and_owner_agents": {"rules": [{**_RULE, "owner": True, "owner_agents": True}]},
            "owner_only": {"rules": [{**_RULE, "owner": True, "owner_agents": False}]},
        }
        return await self._send(
            "addressing",
            "PUT",
            f"/agents/{agent_id}/addressing-policy",
            {"policy": policies[mode]},
        )

    async def set_display_name(self, agent_id: str, display_name: str | None) -> Any:
        """PUT /gateway/agents/{id}/display-name (null clears it)."""
        return await self._send(
            "display_name",
            "PUT",
            f"/agents/{agent_id}/display-name",
            {"display_name": display_name},
        )

    async def create_library_document(
        self,
        name: str,
        description: str,
        instructions: str,
        content: str,
        *,
        read_visibility: str = "public",
    ) -> str:
        res = await self._send(
            "create_document",
            "POST",
            "/documents",
            {
                "name": name,
                "description": description,
                "instructions": instructions,
                "content": content,
                "read_visibility": read_visibility,
                "write_visibility": "private",
            },
        )
        return str(res["id"])

    async def attach_document(self, room_id: str, document_id: str) -> None:
        await self._send("attach_document", "POST", f"/rooms/{room_id}/documents/{document_id}")

    # Reads (make doctor) ------------------------------------------------------------------------

    async def list_rooms(self, *, include_archived: bool = False) -> list[GwRoom]:
        r = await self._http.get(
            "/rooms", params={"include_archived": "true"} if include_archived else None
        )
        if r.status_code >= 400:
            raise error_for(r.status_code, r.text[:300], "list_rooms")
        return [GwRoom.model_validate(x) for x in r.json()]

    async def room(self, room_id: str) -> GwRoomDetail:
        return GwRoomDetail.model_validate(await self._send("room", "GET", f"/rooms/{room_id}"))

    async def room_packages(self, room_id: str) -> list[GwNamed]:
        res = await self._send("room_packages", "GET", f"/rooms/{room_id}/packages")
        return [GwNamed.model_validate(x) for x in res]

    async def package_documents(self, package_id: str) -> list[GwDocument]:
        res = await self._send("package_documents", "GET", f"/packages/{package_id}/documents")
        return [GwDocument.model_validate(x) for x in res]

    async def document(self, document_id: str) -> GwDocument:
        res = await self._send("document", "GET", f"/documents/{document_id}")
        return GwDocument.model_validate(res)

    async def list_agents(self) -> list[GwNamed]:
        return [GwNamed.model_validate(x) for x in await self._send("agents", "GET", "/agents")]

    async def list_templates(self) -> list[GwTemplate]:
        res = await self._send("templates", "GET", "/templates")
        return [GwTemplate.model_validate(x) for x in res]

    async def template_content(self, template_id: str) -> str:
        """The stored file, byte for byte (served as application/x-yaml)."""
        r = await self._http.get(f"/templates/{template_id}/content")
        if r.status_code >= 400:
            raise error_for(r.status_code, r.text[:300], "template_content")
        return r.text
