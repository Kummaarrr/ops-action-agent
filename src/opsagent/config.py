"""Paths and settings: secrets from the environment / .env, nothing else machine-specific."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"
GENERATED = DATA_DIR / "generated"
POLICY_DIR = PROJECT_ROOT / "policy"


def project_path(relative: str) -> Path:
    return PROJECT_ROOT / relative


class Env(BaseSettings):
    """Values read from environment variables / .env (the shell wins)."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    anthropic_api_key: str = ""
    max_run_cost_usd: float = 5.0
    # phase 4: real sandboxes (test / development credentials only; never live keys)
    shopify_store_domain: str = ""
    shopify_admin_token: str = ""
    stripe_test_secret_key: str = ""
    helpdesk_subdomain: str = ""
    helpdesk_email: str = ""
    helpdesk_api_token: str = ""
