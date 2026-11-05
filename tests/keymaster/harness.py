"""A Keymaster wired to fakes: messages are captured instead of posted."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from leasedesk.common.schemas import AccessPolicy
from leasedesk.config import Settings
from leasedesk.keymaster.policy import PolicyLoader
from leasedesk.keymaster.service import Keymaster
from leasedesk.keymaster.store import Lease, LeaseState, LeaseStore
from leasedesk.switch.credentials import AgentCredentials
from leasedesk.switch.models import DomainEvent
from leasedesk.switch.ops import OpsClient

from ..common.conftest import proposal_dict
from .conftest import KEYMASTER, SCRIBE, SENTINEL, Clock, policy_dict
from .fakes import FakeExecutor

DESK = "room-desk"
SEC = "room-sec"


class CapturingKeymaster(Keymaster):
    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.sent: list[tuple[str, str | None, str]] = []
        self.ledger_docs: list[str] = []

    async def post(self, room_id: str, text: str, *, thread_id: str | None = None) -> list[str]:
        self.sent.append((room_id, thread_id, text))
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
        return f"sw_out_{len(self.sent)}"

    def texts(self) -> list[str]:
        return [t for _, _, t in self.sent]

    def last(self) -> str:
        return self.sent[-1][2]


class Harness:
    def __init__(self, tmp: Path, clock: Clock, **policy_over: Any) -> None:
        self.clock = clock
        self.policy_text = yaml.safe_dump(policy_dict(**policy_over))
        self.settings = Settings(  # type: ignore[call-arg]
            _env_file=None,
            LEASEDESK_STATE_DIR=tmp / ".state",
            LEASEDESK_DEV_MODE=True,
            LEASEDESK_TZ="America/New_York",
        )
        self.store = LeaseStore(tmp / "k.db", clock=clock)
        self.executor = FakeExecutor()
        creds = AgentCredentials(name="keymaster", id="km-uuid", api_key="k" * 43)  # type: ignore[arg-type]

        async def fetch() -> str:
            return self.policy_text

        self.km = CapturingKeymaster(
            self.settings,
            creds,
            OpsClient("http://switch.test", creds),
            store=self.store,
            executor=self.executor,
            clock=clock,
            loader=PolicyLoader(fetch),
            own_id=KEYMASTER,
            ledger_writer=self._write_ledger,
            jsonl=tmp / "ledger.jsonl",
        )
        self.km.known_agent_ids |= {SCRIBE, SENTINEL}
        self.km.agent_names |= {"scribe", "sentinel"}
        self.km.room_names = {DESK: "Discord: access-desk", SEC: "Discord: security-review"}
        self._seq = 0

    async def _write_ledger(self, room_id: str, content: str) -> None:
        self.km.ledger_docs.append(content)

    async def start(self) -> None:
        await self.km.reload_policy()

    def set_policy(self, **over: Any) -> None:
        self.policy_text = yaml.safe_dump(policy_dict(**over))

    @property
    def policy(self) -> AccessPolicy:
        assert self.km.policy is not None
        return self.km.policy

    async def say(
        self,
        sender: str,
        body: str,
        *,
        room: str = DESK,
        thread_id: str | None = None,
        name: str | None = None,
        addressed: bool = True,
    ) -> None:
        self._seq += 1
        ev = DomainEvent.model_validate(
            {
                "event": "message",
                "id": self._seq,
                "type": "message",
                "room_id": room,
                "sequence": self._seq,
                "payload": {
                    "addressed": addressed,
                    "sender": sender,
                    "sender_name": name or sender.split("-")[-1].split(":")[0],
                    "message_id": f"sw_in_{self._seq}",
                    "body": body,
                    "thread_id": thread_id,
                    "attachments": [],
                },
            }
        )
        await self.km.handle_event(room, ev)

    async def submit(self, sender: str = SCRIBE, **over: Any) -> str:
        block = json.dumps(proposal_dict(**over), indent=2)
        name = "scribe" if sender == SCRIBE else None
        await self.say(sender, f"@keymaster submit\n```lease\n{block}\n```", name=name)
        ids = [lease.id for lease in self.store.recent(1)]
        return ids[0] if ids else ""

    async def verdict(
        self,
        lease_id: str,
        risk: str,
        sender: str = SENTINEL,
        score: int = 10,
        findings: list[dict[str, Any]] | None = None,
    ) -> None:
        v = {
            "kind": "lease_verdict",
            "version": 1,
            "lease_id": lease_id,
            "risk": risk,
            "score": score,
            "findings": findings or [],
            "aws_validation": "pass",
        }
        await self.say(
            sender, f"@keymaster verdict\n```verdict\n{json.dumps(v)}\n```", name="sentinel"
        )

    def lease(self, lease_id: str) -> Lease:
        return self.store.require(lease_id)

    def state(self, lease_id: str) -> LeaseState:
        return self.lease(lease_id).state
