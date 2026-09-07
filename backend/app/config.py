"""Runtime configuration. Fails closed on unsafe production settings."""

from __future__ import annotations

import secrets
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

INSECURE_PLACEHOLDER = "change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="EDID_", extra="ignore", case_sensitive=False
    )

    environment: Literal["development", "test", "production"] = "development"
    app_name: str = "Executive Decision Intelligence"
    api_prefix: str = "/api/v1"

    # --- secrets -----------------------------------------------------------
    secret_key: str = Field(default=INSECURE_PLACEHOLDER, min_length=8)
    jwt_algorithm: Literal["HS256", "HS384", "HS512"] = "HS256"
    access_token_ttl_seconds: int = Field(default=900, ge=60, le=3600)
    refresh_token_ttl_seconds: int = Field(default=1_209_600, ge=3600)

    # --- database ----------------------------------------------------------
    database_url: str = "sqlite+aiosqlite:///./edid.db"
    db_echo: bool = False

    # --- transport ---------------------------------------------------------
    cors_origins: tuple[str, ...] = ("http://localhost:8000", "http://127.0.0.1:8000")
    trusted_hosts: tuple[str, ...] = ("*",)
    force_https: bool = False
    max_body_bytes: int = Field(default=1_048_576, ge=1024)

    # --- rate limiting -----------------------------------------------------
    rate_limit_enabled: bool = True
    rate_limit_requests: int = Field(default=120, ge=1)
    rate_limit_window_seconds: int = Field(default=60, ge=1)
    auth_rate_limit_requests: int = Field(default=10, ge=1)
    auth_rate_limit_window_seconds: int = Field(default=300, ge=1)

    # --- analytics limits (DoS guards) -------------------------------------
    max_options_per_decision: int = Field(default=50, ge=2)
    max_criteria_per_decision: int = Field(default=30, ge=1)
    max_simulation_runs: int = Field(default=50_000, ge=100)
    max_forecast_horizon: int = Field(default=90, ge=1)

    # --- bootstrap ---------------------------------------------------------
    seed_demo_data: bool = False
    bootstrap_admin_email: str | None = None
    bootstrap_admin_password: str | None = None

    @field_validator("cors_origins", "trusted_hosts", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(item.strip() for item in value.split(",") if item.strip())
        return value

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    def enforce_production_invariants(self) -> None:
        """Refuse to boot production with development defaults."""
        if not self.is_production:
            return
        problems: list[str] = []
        if self.secret_key == INSECURE_PLACEHOLDER or len(self.secret_key) < 32:
            problems.append("EDID_SECRET_KEY must be >=32 random chars in production")
        if "*" in self.cors_origins:
            problems.append("EDID_CORS_ORIGINS must not be '*' in production")
        if "*" in self.trusted_hosts:
            problems.append("EDID_TRUSTED_HOSTS must be an explicit allowlist")
        if not self.force_https:
            problems.append("EDID_FORCE_HTTPS must be true in production")
        if self.database_url.startswith("sqlite"):
            problems.append("SQLite is not supported in production; use PostgreSQL")
        if problems:
            raise RuntimeError("Unsafe production configuration: " + "; ".join(problems))


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    if settings.secret_key == INSECURE_PLACEHOLDER and not settings.is_production:
        # Ephemeral per-process key: dev tokens never survive a restart.
        settings.secret_key = secrets.token_urlsafe(48)
    settings.enforce_production_invariants()
    return settings
