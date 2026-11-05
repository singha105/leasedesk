from __future__ import annotations

import stat
from pathlib import Path

import pytest

from leasedesk.common.envfile import set_env_value
from leasedesk.config import Settings


def test_dev_fake_verdict_env_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEASEDESK_DEV_FAKE_VERDICT", "LOW")
    monkeypatch.setenv("LEASEDESK_TZ", "America/New_York")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.dev_fake_verdict == "LOW"
    assert s.tz == "America/New_York"
    assert s.policy_source == "document"


def test_dev_fake_verdict_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEASEDESK_DEV_FAKE_VERDICT", raising=False)
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.dev_fake_verdict is None
    assert s.dev_mode is False


def test_set_env_value_mode_600(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("A=1\nB=2\n")
    set_env_value(env, "B", "3")
    set_env_value(env, "C", "4")
    assert env.read_text() == "A=1\nB=3\nC=4\n"
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
