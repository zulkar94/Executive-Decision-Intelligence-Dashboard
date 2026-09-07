"""Configuration fail-closed behaviour and the idempotency of seeding."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import INSECURE_PLACEHOLDER, Settings
from app.models import Decision, Score, User


def production_settings(**overrides) -> Settings:
    base = {
        "environment": "production",
        "secret_key": "a" * 48,
        "cors_origins": ("https://edid.example.com",),
        "trusted_hosts": ("edid.example.com",),
        "force_https": True,
        "database_url": "postgresql+asyncpg://edid:pw@db:5432/edid",
    }
    base.update(overrides)
    return Settings(**base)


def test_a_valid_production_configuration_boots() -> None:
    production_settings().enforce_production_invariants()


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({"secret_key": INSECURE_PLACEHOLDER}, "SECRET_KEY"),
        ({"secret_key": "tooshort"}, "SECRET_KEY"),
        ({"cors_origins": ("*",)}, "CORS_ORIGINS"),
        ({"trusted_hosts": ("*",)}, "TRUSTED_HOSTS"),
        ({"force_https": False}, "FORCE_HTTPS"),
        ({"database_url": "sqlite+aiosqlite:///./edid.db"}, "SQLite"),
    ],
)
def test_production_refuses_unsafe_settings(override: dict, expected: str) -> None:
    with pytest.raises(RuntimeError, match=expected):
        production_settings(**override).enforce_production_invariants()


def test_development_tolerates_defaults() -> None:
    Settings(environment="development").enforce_production_invariants()


def test_csv_environment_values_become_tuples() -> None:
    settings = Settings(cors_origins="https://a.example, https://b.example")
    assert settings.cors_origins == ("https://a.example", "https://b.example")


async def test_seeding_is_idempotent(engine, monkeypatch) -> None:
    """Re-running the seed must not duplicate the portfolio."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("app.seed.SessionLocal", factory)

    from app.seed import ensure_admin, seed_demo

    first = await seed_demo("Dem0-Passphrase!42")
    second = await seed_demo("Dem0-Passphrase!42")
    assert first == second

    await ensure_admin("boss@edid.io", "B0ss-Passphrase!42")
    await ensure_admin("boss@edid.io", "B0ss-Passphrase!42")

    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(Decision)) == 1
        assert await session.scalar(select(func.count()).select_from(Score)) == 15
        assert await session.scalar(select(func.count()).select_from(User)) == 2
        admin = await session.scalar(select(User).where(User.email == "boss@edid.io"))
        assert admin is not None and admin.role.value == "admin"
        assert admin.password_hash.startswith("$argon2id$")
