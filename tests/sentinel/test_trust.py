"""sentinel gives a gating verdict only to keymaster's pinned participant id."""

from __future__ import annotations

from leasedesk.config import Settings
from leasedesk.sentinel.trust import request_mode

KEYMASTER = "@switch-agent-aaaa0000:localhost"
PERSON = "@switch-discord-b1-priya:localhost"
SCRIBE = "@switch-agent-5c41be00:localhost"


def settings(**env: object) -> Settings:
    return Settings(_env_file=None, **env)  # type: ignore[call-arg]


def test_request_from_keymaster_id_is_gating() -> None:
    assert request_mode(KEYMASTER, settings(LEASEDESK_KEYMASTER_ID=KEYMASTER)) == "gating"


def test_request_from_anyone_else_is_advisory() -> None:
    s = settings(LEASEDESK_KEYMASTER_ID=KEYMASTER)
    assert request_mode(PERSON, s) == "advisory"
    assert request_mode(SCRIBE, s) == "advisory"
    assert request_mode(KEYMASTER.upper(), s) == "advisory"


def test_unpinned_keymaster_means_advisory_outside_dev_mode() -> None:
    assert request_mode(KEYMASTER, settings()) == "advisory"
    assert request_mode(KEYMASTER, settings(LEASEDESK_DEV_MODE=True)) == "advisory"
