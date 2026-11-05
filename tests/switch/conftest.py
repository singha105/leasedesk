from __future__ import annotations

import pytest
from pydantic import SecretStr

from leasedesk.switch.credentials import AgentCredentials

BASE = "http://switch.test"
KEY = "k" * 43


@pytest.fixture
def creds() -> AgentCredentials:
    return AgentCredentials(name="keymaster", id="agent-1", api_key=SecretStr(KEY))
