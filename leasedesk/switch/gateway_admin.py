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
