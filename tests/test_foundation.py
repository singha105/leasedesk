"""Foundation tests: settings defaults and log redaction (security model rule 7)."""

from __future__ import annotations

import json

import pytest
import structlog

from leasedesk.common.logging import REDACTED, configure_logging, redact, redact_text
from leasedesk.config import Settings


def test_aws_mode_defaults_to_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEASEDESK_AWS_MODE", raising=False)
    assert Settings(_env_file=None).aws_mode == "mock"  # type: ignore[call-arg]


def test_registration_key_never_prints(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SWITCH_REGISTRATION_KEY", "reg-key-should-not-appear")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert "reg-key-should-not-appear" not in repr(settings)
    assert "reg-key-should-not-appear" not in str(settings.model_dump())


def test_redacts_secret_keys_recursively() -> None:
    data = {"agent": {"id": "a1", "api_key": "sk-live-123"}, "headers": {"Authorization": "x"}}
    out = redact(data)
    assert out == {
        "agent": {"id": "a1", "api_key": REDACTED},
        "headers": {"Authorization": REDACTED},
    }


def test_redacts_secret_patterns_in_text() -> None:
    text = "calling with Bearer abcdefghijklmnop and key AKIAABCDEFGHIJKLMNOP"
    out = redact_text(text)
    assert "abcdefghijklmnop" not in out
    assert "AKIAABCDEFGHIJKLMNOP" not in out


def test_log_lines_are_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    structlog.reset_defaults()
    configure_logging("INFO")
    structlog.get_logger("t").info("registered", agent_id="a1", api_key="sk-live-123")
    line = capsys.readouterr().out.strip().splitlines()[-1]
    record = json.loads(line)
    assert record["api_key"] == REDACTED
    assert "sk-live-123" not in line
    structlog.reset_defaults()


def test_library_loggers_quiet_below_warning() -> None:
    """httpx/websockets log through plain stdlib logging, outside the redaction processor."""
    import logging

    from leasedesk.common.logging import configure_logging

    configure_logging("INFO")
    for name in ("httpx", "httpcore", "websockets"):
        assert logging.getLogger(name).level >= logging.WARNING


def test_a_warning_configure_does_not_stick_to_module_loggers() -> None:
    """The CLI configures WARNING. A module logger first used then must still log INFO once
    logging is configured again (a test after it, or an agent in the same process)."""
    from structlog.testing import capture_logs

    log = structlog.get_logger("cache-check")
    configure_logging("WARNING")
    log.info("hidden")
    configure_logging("INFO")
    with capture_logs() as logs:
        log.info("shown")
    structlog.reset_defaults()
    assert [e["event"] for e in logs] == ["shown"]
