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

    # keymaster
    tz: str = Field(default="America/New_York", alias="LEASEDESK_TZ")
    policy_source: Literal["document", "file"] = Field(
        default="document", alias="LEASEDESK_POLICY_SOURCE"
    )
    policy_file: Path = Field(
        default=Path("switch/docs/access-policy.yaml"), alias="LEASEDESK_POLICY_FILE"
    )
    policy_document_name: str = Field(default="access-policy", alias="LEASEDESK_POLICY_DOC")
    catalog_document_name: str = Field(default="resource-catalog", alias="LEASEDESK_CATALOG_DOC")
    catalog_file: Path = Field(
        default=Path("switch/docs/resource-catalog.md"), alias="LEASEDESK_CATALOG_FILE"
    )
    dev_fake_verdict: Literal["LOW", "MEDIUM", "HIGH", "BLOCK"] | None = Field(
        default=None, alias="LEASEDESK_DEV_FAKE_VERDICT"
    )
    dev_mode: bool = Field(default=False, alias="LEASEDESK_DEV_MODE")
    moto_port: int = Field(default=5055, alias="LEASEDESK_MOTO_PORT")
    db_path: Path = Field(default=Path(".state/keymaster.db"), alias="LEASEDESK_DB")
    sentinel_db_path: Path = Field(
        default=Path(".state/sentinel.db"), alias="LEASEDESK_SENTINEL_DB"
    )
    aws_account_id: str | None = Field(default=None, alias="LEASEDESK_AWS_ACCOUNT_ID")
    keymaster_id: str | None = Field(default=None, alias="LEASEDESK_KEYMASTER_ID")
    sentinel_id: str | None = Field(default=None, alias="LEASEDESK_SENTINEL_ID")

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
