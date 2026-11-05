"""Frame and payload models, checked against frames recorded in docs/PROTOCOL_NOTES.md."""

from __future__ import annotations

import json

from leasedesk.switch.models import (
    ConnectionState,
    DomainEvent,
    Evicted,
    Gap,
    Ping,
    SubscriptionChanged,
    UnknownFrame,
    parse_frame,
)

ROOM = "a4580f7c-088f-4a09-bc04-a1049657d105"
SENDER = "@switch-discord-36e05d34-8d5d-4564-9f88-86e9775fb038-piggyback101:localhost"

MENTIONED = {
    "event": "message",
    "id": 9007194959773697,
    "data": {
        "type": "message",
        "room_id": ROOM,
        "bridge_id": "36e05d34-8d5d-4564-9f88-86e9775fb038",
        "channel_type": "channel_public",
        "payload": {
            "addressed": True,
            "sender": SENDER,
            "sender_name": "piggyback101.",
            "sender_kind": None,
            "on_behalf_of": None,
            "message_id": "sw_d8dc9d3bb835a777136eeb3e9b8cf00c",
            "body": "@leasedesk-probe hello from Arnab",
            "timestamp": 1793769373247,
            "thread_id": None,
            "attachments": [],
            "opening_message": None,
            "sender_role": "owner",
        },
        "sequence": 9007194959773697,
        "missed": {"count": 0, "reason": None},
    },
}


def test_parse_message_frame() -> None:
    ev = parse_frame(json.dumps(MENTIONED))
    assert isinstance(ev, DomainEvent)
    assert ev.id == ev.sequence == 9007194959773697
    assert ev.room_id == ROOM
    msg = ev.message()
    assert msg is not None
    assert msg.addressed is True
    assert msg.sender == SENDER
    assert msg.sender_role == "owner"
    assert msg.thread_id is None


def test_parse_file_frame() -> None:
    frame = json.loads(json.dumps(MENTIONED))
    frame["data"]["payload"]["attachments"] = [
        {
            "filename": "spike-attachment.txt",
            "mimetype": "text/plain",
            "size": 89,
            "mxc": "switch-media://52cff66aea0c41c5b3a61f6bd1783dd6",
            "msgtype": "m.file",
        }
    ]
    msg = parse_frame(json.dumps(frame)).message()  # type: ignore[union-attr]
    assert msg is not None
    assert len(msg.attachments) == 1
    assert msg.attachments[0].mxc.startswith("switch-media://")
    assert msg.attachments[0].size == 89


def test_non_message_event_has_no_message() -> None:
    frame = json.loads(json.dumps(MENTIONED))
    frame["event"] = "room_join"
    frame["data"]["type"] = "room_join"
    frame["data"]["payload"] = {"sender": SENDER, "sender_name": "piggyback101."}
    ev = parse_frame(json.dumps(frame))
    assert isinstance(ev, DomainEvent)
    assert ev.message() is None
    assert ev.room_join() is not None


def test_unknown_kind_is_not_an_error() -> None:
    f = parse_frame('{"event": "mystery", "data": {"x": 1}}')
    assert isinstance(f, UnknownFrame)
    assert f.event == "mystery"


def test_garbage_is_unknown_frame() -> None:
    assert isinstance(parse_frame("not json"), UnknownFrame)


def test_extra_fields_kept() -> None:
    raw = {
        "event": "connection_state",
        "data": {
            "connection_id": "c1",
            "agent_id": "a1",
            "generation": 8589934592,
            "scope": "all",
            "filter": "all",
            "rooms": [],
            "cursor": 0,
            "position": {"s": 4294967296},
            "protocol": 9,
            "heartbeat_interval_seconds": 2.0,
        },
    }
    f = parse_frame(json.dumps(raw))
    assert isinstance(f, ConnectionState)
    assert f.generation == 8589934592
    assert f.model_extra is not None and f.model_extra["protocol"] == 9


def test_gap_and_evicted_parse() -> None:
    gap = parse_frame(
        json.dumps(
            {
                "event": "gap",
                "data": {
                    "from_sequence": 999999,
                    "resumed_at": 9007194959773696,
                    "rooms": [],
                    "all_rooms": True,
                    "reason": "the server restarted",
                },
            }
        )
    )
    assert isinstance(gap, Gap)
    assert gap.resumed_at == 9007194959773696
    assert gap.all_rooms is True
    ev = parse_frame(
        '{"event": "evicted", "data": {"code": "taken_over", "reason": "x", "room_id": null}}'
    )
    assert isinstance(ev, Evicted)
    assert ev.code == "taken_over"
    assert isinstance(parse_frame('{"event": "ping", "data": {}}'), Ping)
    sc = parse_frame('{"event": "subscription_changed", "data": {"rooms": ["r1"], "reason": "x"}}')
    assert isinstance(sc, SubscriptionChanged)
    assert sc.rooms == ["r1"]
