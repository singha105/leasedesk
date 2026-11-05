"""Pydantic models for every WebSocket frame and the op results LeaseDesk reads.

Shapes come from frames recorded in docs/PROTOCOL_NOTES.md. Every model allows unknown fields:
the server adds fields between revisions (``position`` on connection_state, for one), and a new
field must never crash an agent. A frame kind we don't know becomes an ``UnknownFrame``.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class _Open(BaseModel):
    model_config = ConfigDict(extra="allow")


# Control frames -----------------------------------------------------------------------------


class ConnectionState(_Open):
    connection_id: str
    agent_id: str | None = None
    generation: int
    scope: str | None = None
    filter: str | None = None
    rooms: list[str] = Field(default_factory=list)
    cursor: int | None = None
    heartbeat_interval_seconds: float | None = None


class Ping(_Open):
    pass


class Gap(_Open):
    from_sequence: int | None = None
    resumed_at: int
    rooms: list[str] = Field(default_factory=list)
    all_rooms: bool = False
    reason: str = ""


class Evicted(_Open):
    code: str
    reason: str = ""
    room_id: str | None = None


class SubscriptionChanged(_Open):
    rooms: list[str] = Field(default_factory=list)
    reason: str = ""


class Refused(_Open):
    code: str | None = None
    reason: str = ""


class RoomReleased(_Open):
    room_id: str | None = None
    reason: str = ""


ControlFrame = ConnectionState | Ping | Gap | Evicted | SubscriptionChanged | Refused | RoomReleased

_CONTROL: dict[str, type[_Open]] = {
    "connection_state": ConnectionState,
    "ping": Ping,
    "gap": Gap,
    "evicted": Evicted,
    "subscription_changed": SubscriptionChanged,
    "refused": Refused,
    "room_released": RoomReleased,
}


# Domain event payloads ----------------------------------------------------------------------


class Attachment(_Open):
    filename: str | None = None
    mimetype: str | None = None
    size: int | None = None
    mxc: str
    msgtype: str | None = None


class MessagePayload(_Open):
    addressed: bool = False
    sender: str
    sender_name: str | None = None
    sender_kind: str | None = None
    on_behalf_of: str | None = None
    message_id: str
    body: str | None = None
    timestamp: int | None = None
    thread_id: str | None = None
    attachments: list[Attachment] = Field(default_factory=list)
    sender_role: str | None = None


class CommandPayload(_Open):
    """Never seen live (PROTOCOL_NOTES "Not seen yet"); kept loose on purpose."""

    sender: str | None = None
    sender_name: str | None = None
    command: str | None = None
    args: str | None = None
    message_id: str | None = None


class RoomJoinPayload(_Open):
    sender: str | None = None
    sender_name: str | None = None


class DomainEvent(_Open):
    """message, command or room_join. The top-level frame ``id`` equals ``sequence``."""

    event: str
    id: int
    type: str
    room_id: str
    bridge_id: str | None = None
    channel_type: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    sequence: int
    missed: dict[str, Any] | None = None
    after_gap: bool = False

    def message(self) -> MessagePayload | None:
        if self.type != "message":
            return None
        try:
            return MessagePayload.model_validate(self.payload)
        except ValidationError:
            return None

    def command(self) -> CommandPayload | None:
        return CommandPayload.model_validate(self.payload) if self.type == "command" else None

    def room_join(self) -> RoomJoinPayload | None:
        return RoomJoinPayload.model_validate(self.payload) if self.type == "room_join" else None


class UnknownFrame(_Open):
    event: str
    data: dict[str, Any] = Field(default_factory=dict)


Frame = ControlFrame | DomainEvent | UnknownFrame


def parse_frame(raw: str | bytes) -> Frame:
    """Parse one WebSocket text frame. Never raises: anything odd becomes an UnknownFrame."""
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return UnknownFrame(event="<unparseable>")
    if not isinstance(obj, dict):
        return UnknownFrame(event="<not-an-object>")
    kind = str(obj.get("event", ""))
    raw_data = obj.get("data")
    data: dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}
    try:
        if kind in _CONTROL:
            return _CONTROL[kind].model_validate(data)  # type: ignore[return-value]
        if "id" in obj and "sequence" in data:
            return DomainEvent.model_validate({**data, "event": kind, "id": obj["id"]})
    except ValidationError:
        pass
    return UnknownFrame(event=kind or "<missing>", data=data)


# Op results ---------------------------------------------------------------------------------


class RoomSummary(_Open):
    room_id: str
    name: str = ""
    description: str | None = None
    connected: bool = False
    archived: bool = False


class Participant(_Open):
    id: str
    name: str
    type: str | None = None
    status: str | None = None
    alias: str | None = None


class DocumentMeta(_Open):
    id: str
    name: str
    description: str | None = None
    instructions: str | None = None
    scope: str | None = None
    created_by_agent_id: str | None = None


class DocumentContent(_Open):
    id: str
    name: str | None = None
    description: str | None = None
    content: str = ""


class RoomInfo(_Open):
    agent_id: str | None = None
    room_id: str
    name: str | None = None
    description: str | None = None
    participants: list[Participant] = Field(default_factory=list)
    documents: list[DocumentMeta] = Field(default_factory=list)


class ContextEntry(_Open):
    id: str
    kind: str
    sender: str | None = None
    sender_name: str | None = None
    body: str | None = None
    timestamp: int | None = None


class ContextThread(_Open):
    root: ContextEntry
    replies: list[ContextEntry] = Field(default_factory=list)


class References(_Open):
    documents: list[DocumentMeta] = Field(default_factory=list)
    linked_rooms: list[dict[str, Any]] = Field(default_factory=list)
