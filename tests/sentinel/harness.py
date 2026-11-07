"""A Sentinel wired to fakes: what it would post is captured, and documents come from memory."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from leasedesk.common.policy import PolicyLoader
from leasedesk.common.schemas import LeaseProposal, ReviewRequest
from leasedesk.config import Settings
from leasedesk.sentinel.agent import Sentinel
from leasedesk.sentinel.catalog import CatalogLoader
from leasedesk.sentinel.ratelimit import RateLimiter
from leasedesk.sentinel.store import ReviewStore
from leasedesk.switch.credentials import AgentCredentials
from leasedesk.switch.models import DomainEvent
from leasedesk.switch.ops import OpsClient

from ..keymaster.conftest import KEYMASTER, SENTINEL, Clock, policy_dict
from .conftest import CATALOG, raw

ROOM = "room-desk"
CREDS = AgentCredentials(name="sentinel", id="se-uuid", api_key="s" * 43)  # type: ignore[arg-type]


class CapturingSentinel(Sentinel):
    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.sent: list[tuple[str, str | None, str]] = []
        self.paths: list[str] = []

    async def post(self, room_id: str, text: str, *, thread_id: str | None = None) -> list[str]:
        self.sent.append((room_id, thread_id, text))
        self.paths.append("post")
        return [f"sw_out_{len(self.sent)}"]

    async def targeted(
        self,
        room_id: str,
        text: str,
        *,
        roles: list[str] | None = None,
        names: list[str] | None = None,
        thread_id: str | None = None,
    ) -> str:
        self.sent.append((room_id, thread_id, f"@{(roles or names or ['?'])[0]} {text}"))
        self.paths.append("roles" if roles else "names")
        return f"sw_out_{len(self.sent)}"

    def texts(self) -> list[str]:
        return [t for _, _, t in self.sent]


class SentinelHarness:
    def __init__(self, tmp: Path, clock: Clock, **env: Any) -> None:
        self.tmp = tmp
        self.clock = clock
        self.policy_text = yaml.safe_dump(policy_dict())
        self.catalog_text = yaml.safe_dump(CATALOG)
        self.policy_fetches = 0
        base: dict[str, Any] = {
            "LEASEDESK_STATE_DIR": tmp / ".state",
            "LEASEDESK_SENTINEL_DB": tmp / "sentinel.db",
            "LEASEDESK_KEYMASTER_ID": KEYMASTER,
            "LEASEDESK_SENTINEL_ID": SENTINEL,
        }
        self.settings = Settings(_env_file=None, **{**base, **env})  # type: ignore[call-arg]
        self._seq = 0
        self.sentinel = self.build()

    def build(self) -> CapturingSentinel:
        async def policy() -> str:
            self.policy_fetches += 1
            return self.policy_text

        async def catalog() -> str:
            return self.catalog_text

        return CapturingSentinel(
            self.settings,
            CREDS,
            OpsClient("http://switch.test", CREDS),
            policy_loader=PolicyLoader(policy),
            catalog_loader=CatalogLoader(catalog),
            store=ReviewStore(self.settings.sentinel_db_path, clock=self.clock),
            limiter=RateLimiter(clock=lambda: self.clock().timestamp()),
            clock=self.clock,
            own_id=SENTINEL,
        )

    async def start(self) -> None:
        await self.sentinel.reload()

    async def restart(self) -> None:
        self.sentinel.store.close()
        self.sentinel = self.build()
        await self.start()

    async def say(
        self,
        sender: str,
        body: str,
        *,
        thread_id: str | None = "sw_thread",
        name: str = "arnab.",
        addressed: bool = True,
    ) -> None:
        self._seq += 1
        ev = DomainEvent.model_validate(
            {
                "event": "message",
                "id": self._seq,
                "type": "message",
                "room_id": ROOM,
                "sequence": self._seq,
                "payload": {
                    "addressed": addressed,
                    "sender": sender,
                    "sender_name": name,
                    "message_id": f"sw_in_{self._seq}",
                    "body": body,
                    "thread_id": thread_id,
                    "attachments": [],
                },
            }
        )
        await self.sentinel.handle_event(ROOM, ev)

    def request_text(
        self, lease_id: str = "LS-0007", proposal: dict[str, Any] | None = None, **over: Any
    ) -> str:
        req = ReviewRequest(
            lease_id=lease_id,
            proposal=LeaseProposal.model_validate(proposal or raw(duration_minutes=60)),
            policy_hash=over.get("policy_hash", self.sentinel.policy_hash()),
            iam_user="lease-demo-dev",
        )
        block = req.model_dump_json()
        return f"{lease_id} · confirmed by Arnab, under review\n```review\n{block}\n```"

    async def request(
        self,
        lease_id: str = "LS-0007",
        proposal: dict[str, Any] | None = None,
        *,
        sender: str = KEYMASTER,
        addressed: bool = True,
        **over: Any,
    ) -> None:
        name = "keymaster" if sender == KEYMASTER else "arnab."
        await self.say(
            sender, self.request_text(lease_id, proposal, **over), name=name, addressed=addressed
        )

    async def check(self, proposal: dict[str, Any], sender: str) -> None:
        await self.say(sender, f"@sentinel check\n```lease\n{json.dumps(proposal)}\n```")
