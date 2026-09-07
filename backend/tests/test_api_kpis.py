"""KPI ingestion, forecasting, anomalies, and the executive summary."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient

BASE = datetime(2026, 1, 1, tzinfo=UTC)


async def create_kpi(client: AsyncClient, headers: dict, **overrides) -> dict:
    payload = {"name": "Gross margin", "unit": "%", "target": 60.0, "direction": "benefit"}
    payload.update(overrides)
    response = await client.post("/api/v1/kpis", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def series(values: list[float]) -> dict:
    return {
        "points": [
            {"ts": (BASE + timedelta(days=index)).isoformat(), "value": value}
            for index, value in enumerate(values)
        ]
    }


async def test_kpi_lifecycle(client: AsyncClient, admin_headers) -> None:
    kpi = await create_kpi(client, admin_headers)
    listed = (await client.get("/api/v1/kpis", headers=admin_headers)).json()
    assert [item["id"] for item in listed] == [kpi["id"]]
    assert (
        await client.delete(f"/api/v1/kpis/{kpi['id']}", headers=admin_headers)
    ).status_code == 204
    assert (await client.get("/api/v1/kpis", headers=admin_headers)).json() == []


async def test_duplicate_kpi_name_conflicts(client: AsyncClient, admin_headers) -> None:
    await create_kpi(client, admin_headers)
    response = await client.post(
        "/api/v1/kpis", json={"name": "Gross margin"}, headers=admin_headers
    )
    assert response.status_code == 409


async def test_viewer_cannot_write_kpis(client: AsyncClient, admin_headers, viewer_headers) -> None:
    assert (
        await client.post("/api/v1/kpis", json={"name": "Blocked"}, headers=viewer_headers)
    ).status_code == 403
    kpi = await create_kpi(client, admin_headers)
    assert (
        await client.post(
            f"/api/v1/kpis/{kpi['id']}/points", json=series([1.0]), headers=viewer_headers
        )
    ).status_code == 403
    # Reading stays open to viewers.
    assert (await client.get("/api/v1/kpis", headers=viewer_headers)).status_code == 200


async def test_point_ingestion_is_idempotent_by_timestamp(
    client: AsyncClient, admin_headers
) -> None:
    kpi = await create_kpi(client, admin_headers)
    await client.post(
        f"/api/v1/kpis/{kpi['id']}/points", json=series([1.0, 2.0, 3.0]), headers=admin_headers
    )
    await client.post(
        f"/api/v1/kpis/{kpi['id']}/points", json=series([9.0, 2.0, 3.0]), headers=admin_headers
    )
    points = (await client.get(f"/api/v1/kpis/{kpi['id']}/points", headers=admin_headers)).json()
    assert len(points) == 3
    assert points[0]["value"] == 9.0  # overwritten, not duplicated


async def test_forecast_tracks_a_trend_and_returns_a_band(
    client: AsyncClient, admin_headers
) -> None:
    kpi = await create_kpi(client, admin_headers)
    await client.post(
        f"/api/v1/kpis/{kpi['id']}/points",
        json=series([float(50 + 2 * i) for i in range(20)]),
        headers=admin_headers,
    )
    body = (
        await client.get(f"/api/v1/kpis/{kpi['id']}/forecast?horizon=5", headers=admin_headers)
    ).json()
    assert len(body["forecast"]) == 5
    assert body["trend_per_period"] == pytest.approx(2.0, abs=0.3)
    assert all(
        low <= point <= high
        for low, point, high in zip(body["lower"], body["forecast"], body["upper"], strict=True)
    )
    assert body["backtest"]["mae"] < 2.0


async def test_forecast_needs_history(client: AsyncClient, admin_headers) -> None:
    kpi = await create_kpi(client, admin_headers)
    await client.post(
        f"/api/v1/kpis/{kpi['id']}/points", json=series([1.0, 2.0]), headers=admin_headers
    )
    response = await client.get(f"/api/v1/kpis/{kpi['id']}/forecast", headers=admin_headers)
    assert response.status_code == 422


async def test_forecast_horizon_is_capped(client: AsyncClient, admin_headers) -> None:
    kpi = await create_kpi(client, admin_headers)
    await client.post(
        f"/api/v1/kpis/{kpi['id']}/points",
        json=series([float(i) for i in range(10)]),
        headers=admin_headers,
    )
    response = await client.get(
        f"/api/v1/kpis/{kpi['id']}/forecast?horizon=5000", headers=admin_headers
    )
    assert response.status_code == 422


async def test_anomalies_are_timestamped(client: AsyncClient, admin_headers) -> None:
    kpi = await create_kpi(client, admin_headers)
    values = [10.0] * 12
    values[6] = 90.0
    await client.post(
        f"/api/v1/kpis/{kpi['id']}/points", json=series(values), headers=admin_headers
    )
    found = (await client.get(f"/api/v1/kpis/{kpi['id']}/anomalies", headers=admin_headers)).json()
    assert len(found) == 1
    assert found[0]["kind"] == "spike"
    assert found[0]["ts"].startswith("2026-01-07")


async def test_unknown_kpi_returns_404(client: AsyncClient, admin_headers) -> None:
    assert (
        await client.get("/api/v1/kpis/missing/forecast", headers=admin_headers)
    ).status_code == 404


async def test_executive_summary_reports_health_and_fragility(
    client: AsyncClient, admin_headers, decision
) -> None:
    healthy = await create_kpi(client, admin_headers, name="Margin", target=50.0)
    await client.post(
        f"/api/v1/kpis/{healthy['id']}/points",
        json=series([float(60 + i) for i in range(10)]),
        headers=admin_headers,
    )
    lagging = await create_kpi(
        client, admin_headers, name="Lead time", target=10.0, direction="cost"
    )
    await client.post(
        f"/api/v1/kpis/{lagging['id']}/points",
        json=series([float(30 + i) for i in range(10)]),
        headers=admin_headers,
    )
    thin = await create_kpi(client, admin_headers, name="New signal", target=None)
    await client.post(
        f"/api/v1/kpis/{thin['id']}/points", json=series([1.0]), headers=admin_headers
    )

    body = (await client.get("/api/v1/analytics/executive-summary", headers=admin_headers)).json()
    statuses = {row["name"]: row["status"] for row in body["kpi_health"]}
    assert statuses["Margin"] == "on_track"
    assert statuses["Lead time"] == "off_track"
    assert statuses["New signal"] == "insufficient_data"
    assert 0.0 <= body["portfolio_score"] <= 100.0
    assert body["open_decisions"] >= 1


async def test_executive_summary_requires_authentication(client: AsyncClient) -> None:
    assert (await client.get("/api/v1/analytics/executive-summary")).status_code == 401


async def test_health_endpoint_is_open_and_reports_the_database(client: AsyncClient) -> None:
    body = (await client.get("/api/v1/health")).json()
    assert body["status"] in {"ok", "degraded"}
    assert body["version"]
