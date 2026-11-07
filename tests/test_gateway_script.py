"""Pure helpers of scripts/gateway.py (the person-side Gateway commands)."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import gateway  # noqa: E402


def test_validation_report_clean() -> None:
    text, code = gateway.validation_report(
        {"ok": True, "blocked": False, "errors": [], "warnings": []}
    )
    assert (text, code) == ("ok: 0 errors, 0 warnings", 0)


def test_validation_report_lists_findings_and_fails() -> None:
    body = {
        "ok": False,
        "blocked": False,
        "errors": [],
        "warnings": [
            {"code": "unused_param", "message": "Parameter 'x' is declared but never used."}
        ],
    }
    text, code = gateway.validation_report(body)
    assert code == 1
    assert text.splitlines() == [
        "not ok: 0 errors, 1 warnings",
        "warning unused_param: Parameter 'x' is declared but never used.",
    ]


def test_validation_report_blocked() -> None:
    body = {"ok": False, "blocked": True, "errors": [{"code": "invalid_yaml", "message": "m"}]}
    text, code = gateway.validation_report(body)
    assert code == 1 and text.startswith("blocked: 1 errors, 0 warnings")


def test_members_to_add_skips_present_and_repeats() -> None:
    assert gateway.members_to_add({"d-1"}, ["d-1", "d-2", "d-2", "d-3"]) == ["d-2", "d-3"]


def _run_body() -> dict:
    return {
        "created_agents": [
            {
                "slot": "scribe",
                "agent_id": "a-1",
                "name": "scribe",
                "machine": "m-1",
                "listening": True,
            },
            {
                "slot": "skeptic",
                "agent_id": "a-2",
                "name": "skeptic",
                "machine": "m-1",
                "listening": False,
            },
        ],
        "room": {
            "group_id": "g-1",
            "group_name": "LeaseDesk",
            "rooms": [
                {
                    "room_id": "r-1",
                    "room_name": "access-desk",
                    "role_names": ["drafter", "reviewer", "executor"],
                    "created_document_ids": ["d-1"],
                    "failed_attachments": [],
                },
                {
                    "room_id": "r-2",
                    "room_name": "security-review",
                    "role_names": ["reviewer", "executor"],
                    "failed_attachments": [],
                },
            ],
            "errors": [],
        },
    }


def test_run_report_lists_agents_group_and_rooms() -> None:
    text, code = gateway.run_report(_run_body())
    assert code == 0
    assert text.splitlines() == [
        "agent scribe (scribe): id=a-1 machine=m-1 listening",
        "agent skeptic (skeptic): id=a-2 machine=m-1 NOT listening (the kickoff may miss it)",
        "group LeaseDesk: id=g-1",
        "room access-desk: id=r-1 roles=drafter,reviewer,executor docs=1",
        "room security-review: id=r-2 roles=reviewer,executor docs=0",
    ]


def test_run_report_fails_on_failed_attachment_or_group_error() -> None:
    body = _run_body()
    body["room"]["rooms"][1]["failed_attachments"] = [{"kind": "document", "error": "boom"}]
    body["room"]["errors"] = [{"room": "x", "error": "link failed"}]
    text, code = gateway.run_report(body)
    assert code == 1
    assert "room security-review failed attachment: {'kind': 'document', 'error': 'boom'}" in text
    assert "group error: {'room': 'x', 'error': 'link failed'}" in text


def test_run_report_refused() -> None:
    body = {
        "code": "agents_not_created",
        "detail": "not created",
        "problems": [{"slot": "skeptic", "code": "provider_login_missing", "message": "sign in"}],
    }
    text, code = gateway.run_report(body)
    assert code == 1
    assert text.splitlines() == [
        "refused: agents_not_created (not created)",
        "skeptic provider_login_missing: sign in",
    ]


def test_run_report_undone_names_what_was_left() -> None:
    body = {"code": "run_undone", "detail": "rooms failed", "left_behind": ["skeptic"]}
    text, code = gateway.run_report(body)
    assert code == 1
    assert text.splitlines() == ["refused: run_undone (rooms failed)", "left behind: skeptic"]


def test_run_report_names_the_machine_the_server_returns() -> None:
    body = _run_body()
    body["created_agents"][0]["machine"] = {"id": "m-1", "name": "Arnab's MacBook Pro"}
    text, _ = gateway.run_report(body)
    first = text.splitlines()[0]
    assert first == "agent scribe (scribe): id=a-1 machine=Arnab's MacBook Pro listening"
