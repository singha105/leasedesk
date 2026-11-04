"""Settings loaded from the environment (and .env). Secrets are SecretStr so they never print."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Switch
    switch_base_url: str | None = Field(default=None, alias="SWITCH_BASE_URL")
    switch_registration_key: SecretStr | None = Field(default=None, alias="SWITCH_REGISTRATION_KEY")

    # AWS
    aws_mode: Literal["mock", "real"] = Field(default="mock", alias="LEASEDESK_AWS_MODE")
    aws_region: str = Field(default="us-east-1", alias="LEASEDESK_AWS_REGION")
    keymaster_profile: str = Field(
        default="leasedesk-keymaster", alias="LEASEDESK_KEYMASTER_PROFILE"
    )
    sentinel_profile: str = Field(default="leasedesk-sentinel", alias="LEASEDESK_SENTINEL_PROFILE")
    enable_paid_checks: bool = Field(default=False, alias="LEASEDESK_ENABLE_PAID_CHECKS")

    # Local
    state_dir: Path = Field(default=Path(".state"), alias="LEASEDESK_STATE_DIR")
    secrets_dir: Path = Field(default=Path(".secrets"), alias="LEASEDESK_SECRETS_DIR")
    log_level: str = Field(default="INFO", alias="LEASEDESK_LOG_LEVEL")

    @property
    def agents_secrets_dir(self) -> Path:
        return self.secrets_dir / "agents"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
