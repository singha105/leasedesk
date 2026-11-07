"""register: idempotent, never overwrites without --overwrite, writes ids, never prints keys."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx
from pydantic import SecretStr
from structlog.testing import capture_logs

from leasedesk.config import Settings
from leasedesk.switch.credentials import AgentCredentials, load_credentials, save_credentials
from leasedesk.switch.gateway_admin import GatewayAdmin
from leasedesk.switch.register import RegisterError, describe, register_agent

from .conftest import BASE

NEW_KEY = "n" * 43
REG_KEY = "r" * 43
KM_SENDER = "@switch-agent-1234abcd:localhost"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        SWITCH_BASE_URL=BASE,
        SWITCH_REGISTRATION_KEY=REG_KEY,
        LEASEDESK_SECRETS_DIR=tmp_path / ".secrets",
        LEASEDESK_STATE_DIR=tmp_path / ".state",
    )


def mock_gateway_and_rooms(agent_id: str) -> respx.Route:
    respx.post(f"{BASE}/gateway/auth/login").mock(return_value=httpx.Response(200, json={}))
    respx.post(f"{BASE}/gateway/auth/logout").mock(return_value=httpx.Response(200, json={}))
    put = respx.put(f"{BASE}/gateway/agents/{agent_id}/addressing-policy").mock(
        return_value=httpx.Response(200, json={"addressing_policy": None})
    )
    respx.put(f"{BASE}/gateway/agents/{agent_id}/display-name", name="display").mock(
        return_value=httpx.Response(200, json={"id": agent_id})
    )
    ops = f"{BASE}/agents/{agent_id}/ops"
    respx.post(f"{ops}/list_rooms").mock(
        return_value=httpx.Response(200, json={"result": [{"room_id": "r1", "name": "x"}]})
    )
    respx.post(f"{ops}/read_context").mock(
        return_value=httpx.Response(
            200,
            json={
                "result": {
                    "threads": [
                        {
                            "root": {
                                "id": "sw_1",
                                "kind": "room_join",
                                "sender": KM_SENDER,
                                "sender_name": "keymaster",
                            }
                        }
                    ]
                }
            },
        )
    )
    return put


@respx.mock
async def test_new_agent_saved_and_id_written(settings: Settings, tmp_path: Path) -> None:
    reg = respx.post(f"{BASE}/agents").mock(
        return_value=httpx.Response(200, json={"id": "km-1", "api_key": NEW_KEY})
    )
    put = mock_gateway_and_rooms("km-1")
    env = tmp_path / ".env"
    async with GatewayAdmin(BASE, email="a@b", password="pw") as gw:
        result = await register_agent(
            "keymaster", settings=settings, overwrite=False, admin=gw, env_path=env
        )
    assert result.status == "created"
    assert result.participant_id == KM_SENDER
    body = json.loads(reg.calls.last.request.content)
    assert body["name"] == "keymaster" and "overwrite" not in body
    assert reg.calls.last.request.headers["authorization"] == f"Bearer {REG_KEY}"
    assert json.loads(put.calls.last.request.content) == {"policy": None}
    assert load_credentials("keymaster", settings.secrets_dir).id == "km-1"
    assert f"LEASEDESK_KEYMASTER_ID={KM_SENDER}" in env.read_text()


@respx.mock
async def test_existing_agent_not_overwritten(settings: Settings, tmp_path: Path) -> None:
    save_credentials(
        AgentCredentials(name="keymaster", id="km-1", api_key=SecretStr(NEW_KEY)),
        settings.secrets_dir,
    )
    reg = respx.post(f"{BASE}/agents").mock(return_value=httpx.Response(500))
    respx.get(f"{BASE}/agents/km-1/ops").mock(return_value=httpx.Response(200, json=[]))
    mock_gateway_and_rooms("km-1")
    result = await register_agent(
        "keymaster", settings=settings, overwrite=False, admin=None, env_path=tmp_path / ".env"
    )
    assert result.status == "existing"
    assert reg.call_count == 0
    assert load_credentials("keymaster", settings.secrets_dir).api_key.get_secret_value() == NEW_KEY


@respx.mock
async def test_register_sends_display_name(settings: Settings, tmp_path: Path) -> None:
    reg = respx.post(f"{BASE}/agents").mock(
        return_value=httpx.Response(200, json={"id": "se-1", "api_key": NEW_KEY})
    )
    mock_gateway_and_rooms("se-1")
    await register_agent(
        "sentinel", settings=settings, overwrite=False, admin=None, env_path=tmp_path / ".env"
    )
    assert json.loads(reg.calls.last.request.content)["display_name"] == "Sentinel"


@respx.mock
async def test_existing_agent_gets_display_name_through_gateway(
    settings: Settings, tmp_path: Path
) -> None:
    """Registration can't rename an agent without a new key, so the owner sets it."""
    save_credentials(
        AgentCredentials(name="keymaster", id="km-1", api_key=SecretStr(NEW_KEY)),
        settings.secrets_dir,
    )
    respx.get(f"{BASE}/agents/km-1/ops").mock(return_value=httpx.Response(200, json=[]))
    mock_gateway_and_rooms("km-1")
    async with GatewayAdmin(BASE, email="a@b", password="pw") as gw:
        result = await register_agent(
            "keymaster", settings=settings, overwrite=False, admin=gw, env_path=tmp_path / ".env"
        )
    assert result.status == "existing"
    put = respx.routes["display"]
    assert json.loads(put.calls.last.request.content) == {"display_name": "Keymaster"}


@respx.mock
async def test_409_without_creds_needs_overwrite(settings: Settings, tmp_path: Path) -> None:
    respx.post(f"{BASE}/agents").mock(
        return_value=httpx.Response(409, json={"detail": "Agent already exists: 'keymaster'."})
    )
    with pytest.raises(RegisterError, match="--overwrite"):
        await register_agent(
            "keymaster", settings=settings, overwrite=False, admin=None, env_path=tmp_path / ".e"
        )
    assert not (settings.secrets_dir / "agents" / "keymaster.json").exists()


@respx.mock
async def test_overwrite_sends_flag(settings: Settings, tmp_path: Path) -> None:
    reg = respx.post(f"{BASE}/agents").mock(
        return_value=httpx.Response(200, json={"id": "km-1", "api_key": NEW_KEY})
    )
    mock_gateway_and_rooms("km-1")
    result = await register_agent(
        "keymaster", settings=settings, overwrite=True, admin=None, env_path=tmp_path / ".env"
    )
    assert result.status == "overwritten"
    assert json.loads(reg.calls.last.request.content)["overwrite"] is True


@respx.mock
async def test_key_never_printed(
    settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    respx.post(f"{BASE}/agents").mock(
        return_value=httpx.Response(200, json={"id": "km-1", "api_key": NEW_KEY})
    )
    mock_gateway_and_rooms("km-1")
    with capture_logs() as logs:
        result = await register_agent(
            "keymaster", settings=settings, overwrite=False, admin=None, env_path=tmp_path / ".e"
        )
    print(describe(result))
    out = capsys.readouterr().out
    assert "km-1" in out
    assert NEW_KEY not in out and REG_KEY not in out
    assert NEW_KEY not in str(logs) and REG_KEY not in str(logs)
