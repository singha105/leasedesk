"""Agent credentials in .secrets/agents/<name>.json (file 600, directories 700)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, SecretStr


class AgentCredentials(BaseModel):
    name: str
    id: str
    api_key: SecretStr


def credentials_path(name: str, secrets_dir: Path) -> Path:
    return secrets_dir / "agents" / f"{name}.json"


def load_credentials(name: str, secrets_dir: Path) -> AgentCredentials:
    path = credentials_path(name, secrets_dir)
    if not path.exists():
        raise FileNotFoundError(f"{path} not found; run `make register` first")
    data = json.loads(path.read_text())
    return AgentCredentials(name=name, id=data["id"], api_key=SecretStr(data["api_key"]))


def save_credentials(creds: AgentCredentials, secrets_dir: Path) -> Path:
    for d in (secrets_dir, secrets_dir / "agents"):
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        d.chmod(0o700)
    path = credentials_path(creds.name, secrets_dir)
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    body = {
        "name": creds.name,
        "id": creds.id,
        "api_key": creds.api_key.get_secret_value(),
        "registered_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    path.write_text(json.dumps(body, indent=2) + "\n")
    path.chmod(0o600)
    return path
