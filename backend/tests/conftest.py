"""Test fixtures: isolated in-memory database, seeded users, ready clients."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

os.environ.setdefault("EDID_ENVIRONMENT", "test")
os.environ.setdefault("EDID_SECRET_KEY", "test-secret-key-that-is-long-enough-000000")
os.environ.setdefault("EDID_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("EDID_RATE_LIMIT_ENABLED", "false")

from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db import Base, get_session
from app.main import create_app
from app.security.rate_limit import limiter

ANALYST = {"email": "analyst@edid.io", "password": "Str0ng-Passphrase!42"}
VIEWER = {"email": "viewer@edid.io", "password": "An0ther-Passphrase!42"}


@pytest_asyncio.fixture
async def engine() -> AsyncIterator:
    # StaticPool keeps one connection alive so :memory: survives between calls.
    test_engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield test_engine
    await test_engine.dispose()


@pytest_asyncio.fixture
async def client(engine) -> AsyncIterator[AsyncClient]:
    limiter.reset()
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override_session() -> AsyncIterator:
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app = create_app()
    app.dependency_overrides[get_session] = override_session
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://testserver", headers={"host": "testserver"}
    ) as async_client:
        yield async_client
    app.dependency_overrides.clear()


async def register(client: AsyncClient, credentials: dict[str, str]) -> dict:
    response = await client.post("/api/v1/auth/register", json=credentials)
    assert response.status_code in (201, 409), response.text
    return response.json()


async def login(client: AsyncClient, credentials: dict[str, str]) -> dict:
    response = await client.post("/api/v1/auth/login", json=credentials)
    assert response.status_code == 200, response.text
    return response.json()


def auth_headers(pair: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {pair['access_token']}"}


@pytest_asyncio.fixture
async def admin_headers(client: AsyncClient) -> dict[str, str]:
    """First registered account is promoted to admin automatically."""
    await register(client, ANALYST)
    pair = await login(client, ANALYST)
    return auth_headers(pair)


@pytest_asyncio.fixture
async def viewer_headers(client: AsyncClient, admin_headers) -> dict[str, str]:
    await register(client, VIEWER)
    pair = await login(client, VIEWER)
    return auth_headers(pair)


@pytest_asyncio.fixture
async def decision(client: AsyncClient, admin_headers) -> dict:
    """A fully scored 3x3 decision ready for analytics."""
    created = await client.post(
        "/api/v1/decisions",
        json={"title": "Vendor selection", "description": "Pick the platform"},
        headers=admin_headers,
    )
    assert created.status_code == 201, created.text
    decision_id = created.json()["id"]

    criteria = []
    for name, weight, direction in (
        ("Cost", 0.5, "cost"),
        ("Speed", 0.3, "benefit"),
        ("Risk", 0.2, "cost"),
    ):
        response = await client.post(
            f"/api/v1/decisions/{decision_id}/criteria",
            json={"name": name, "weight": weight, "direction": direction},
            headers=admin_headers,
        )
        assert response.status_code == 201, response.text
        criteria.append(response.json())

    options = []
    for name in ("Alpha", "Bravo", "Charlie"):
        response = await client.post(
            f"/api/v1/decisions/{decision_id}/options",
            json={"name": name},
            headers=admin_headers,
        )
        assert response.status_code == 201, response.text
        options.append(response.json())

    table = [
        [10.0, 8.0, 2.0],
        [14.0, 9.0, 5.0],
        [12.0, 4.0, 1.0],
    ]
    payload = {
        "scores": [
            {
                "option_id": options[i]["id"],
                "criterion_id": criteria[j]["id"],
                "value": table[i][j],
                "uncertainty": 0.5,
            }
            for i in range(3)
            for j in range(3)
        ]
    }
    response = await client.put(
        f"/api/v1/decisions/{decision_id}/scores", json=payload, headers=admin_headers
    )
    assert response.status_code == 200, response.text
    return {"id": decision_id, "criteria": criteria, "options": options}


@pytest.fixture(autouse=True)
def _reset_limiter() -> None:
    limiter.reset()
