#!/usr/bin/env python3
"""End-to-end smoke test against an in-process instance.

Runs the whole product path -- register, build a decision, rank it, test its
sensitivity, simulate it, ingest a KPI, forecast it, rotate a session -- with a
throwaway SQLite file and no network. Exits non-zero on the first failure so CI
can gate on it.

    cd backend && python ../scripts/smoke.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

WORKDIR = Path(tempfile.mkdtemp(prefix="edid-smoke-"))
os.environ["EDID_ENVIRONMENT"] = "development"
os.environ["EDID_SECRET_KEY"] = "smoke-test-key-not-used-outside-this-process-000"
os.environ["EDID_DATABASE_URL"] = f"sqlite+aiosqlite:///{WORKDIR / 'smoke.db'}"
os.environ["EDID_RATE_LIMIT_ENABLED"] = "false"
os.environ["EDID_SEED_DEMO_DATA"] = "false"

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from httpx import ASGITransport, AsyncClient  # noqa: E402

from app.db import init_models  # noqa: E402
from app.main import create_app  # noqa: E402

API = "/api/v1"
ACCOUNT = {"email": "smoke@edid.io", "password": "Sm0ke-Passphrase!42"}
PASSED = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if condition:
        PASSED += 1
        print(f"  ok    {label}")
        return
    print(f"  FAIL  {label} {detail}")
    raise SystemExit(1)


async def main() -> None:
    await init_models()
    app = create_app()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://smoke.local") as client:
        print("health and hardening")
        health = await client.get(f"{API}/health")
        check("health responds", health.status_code == 200, health.text)
        check("database reachable", health.json()["database"] == "ok")
        headers = health.headers
        check("CSP forbids inline script", "unsafe-inline" not in headers["Content-Security-Policy"])
        check("framing denied", headers["X-Frame-Options"] == "DENY")
        check("request id issued", bool(headers.get("X-Request-ID")))

        print("frontend")
        index = await client.get("/")
        check("dashboard served", index.status_code == 200 and "<html" in index.text)
        for asset in ("/assets/app.js", "/assets/styles.css"):
            response = await client.get(asset)
            check(f"{asset} served", response.status_code == 200)

        print("identity")
        registered = await client.post(f"{API}/auth/register", json=ACCOUNT)
        check("first account registers", registered.status_code == 201, registered.text)
        check("first account is admin", registered.json()["role"] == "admin")
        weak = await client.post(
            f"{API}/auth/register", json={"email": "weak@edid.io", "password": "password1234"}
        )
        check("weak password refused", weak.status_code == 422)

        pair = (await client.post(f"{API}/auth/login", json=ACCOUNT)).json()
        auth = {"Authorization": f"Bearer {pair['access_token']}"}
        check("login returns a token pair", "access_token" in pair and "refresh_token" in pair)
        check(
            "anonymous access refused",
            (await client.get(f"{API}/decisions")).status_code == 401,
        )

        print("session rotation")
        rotated = (
            await client.post(f"{API}/auth/refresh", json={"refresh_token": pair["refresh_token"]})
        ).json()
        check("refresh rotates", rotated["refresh_token"] != pair["refresh_token"])
        replay = await client.post(
            f"{API}/auth/refresh", json={"refresh_token": pair["refresh_token"]}
        )
        check("replayed token rejected", replay.status_code == 401)
        dead = await client.post(
            f"{API}/auth/refresh", json={"refresh_token": rotated["refresh_token"]}
        )
        check("token family revoked after replay", dead.status_code == 401)

        pair = (await client.post(f"{API}/auth/login", json=ACCOUNT)).json()
        auth = {"Authorization": f"Bearer {pair['access_token']}"}

        print("decision")
        decision = (
            await client.post(
                f"{API}/decisions",
                json={"title": "Warehouse automation programme"},
                headers=auth,
            )
        ).json()
        criteria = []
        for name, weight, direction in (
            ("Capital cost", 0.35, "cost"),
            ("Throughput gain", 0.30, "benefit"),
            ("Integration risk", 0.20, "cost"),
            ("Workforce impact", 0.15, "cost"),
        ):
            criteria.append(
                (
                    await client.post(
                        f"{API}/decisions/{decision['id']}/criteria",
                        json={"name": name, "weight": weight, "direction": direction},
                        headers=auth,
                    )
                ).json()
            )
        options = []
        for name in ("Full automation", "Partial retrofit", "Defer one year"):
            options.append(
                (
                    await client.post(
                        f"{API}/decisions/{decision['id']}/options",
                        json={"name": name},
                        headers=auth,
                    )
                ).json()
            )
        table = [
            [9.0, 9.5, 8.0, 7.0],
            [5.0, 6.0, 4.0, 3.5],
            [1.0, 1.0, 1.5, 1.0],
        ]
        scores = await client.put(
            f"{API}/decisions/{decision['id']}/scores",
            json={
                "scores": [
                    {
                        "option_id": options[i]["id"],
                        "criterion_id": criteria[j]["id"],
                        "value": table[i][j],
                        "uncertainty": 0.8,
                    }
                    for i in range(len(options))
                    for j in range(len(criteria))
                ]
            },
            headers=auth,
        )
        check("matrix saved", scores.status_code == 200 and len(scores.json()) == 12)

        print("analytics")
        ranking = (
            await client.get(f"{API}/decisions/{decision['id']}/ranking", headers=auth)
        ).json()
        check("ranking is complete", len(ranking["ranked"]) == 3)
        check("weights normalise", abs(sum(ranking["weights"].values()) - 1.0) < 1e-9)
        topsis = (
            await client.get(
                f"{API}/decisions/{decision['id']}/ranking?method=topsis", headers=auth
            )
        ).json()
        check("topsis bounded", all(0 <= row["score"] <= 1 for row in topsis["ranked"]))

        sensitivity = (
            await client.get(f"{API}/decisions/{decision['id']}/sensitivity", headers=auth)
        ).json()
        check("sensitivity covers every criterion", len(sensitivity["rows"]) == 4)
        check(
            "sensitivity is ordered by swing",
            [row["swing"] for row in sensitivity["rows"]]
            == sorted((row["swing"] for row in sensitivity["rows"]), reverse=True),
        )

        first = (
            await client.post(
                f"{API}/decisions/{decision['id']}/simulate",
                json={"runs": 2000, "seed": 7, "weight_jitter": 0.2},
                headers=auth,
            )
        ).json()
        second = (
            await client.post(
                f"{API}/decisions/{decision['id']}/simulate",
                json={"runs": 2000, "seed": 7, "weight_jitter": 0.2},
                headers=auth,
            )
        ).json()
        check("simulation reproducible under a seed", first["options"] == second["options"])
        check(
            "win probabilities form a distribution",
            abs(sum(row["win_probability"] for row in first["options"]) - 1.0) < 1e-9,
        )

        ahp = (
            await client.post(
                f"{API}/decisions/{decision['id']}/weights/ahp",
                json={
                    "matrix": [
                        [1.0, 2.0, 3.0, 4.0],
                        [0.5, 1.0, 1.5, 2.0],
                        [1 / 3, 2 / 3, 1.0, 4 / 3],
                        [0.25, 0.5, 0.75, 1.0],
                    ],
                    "apply_weights": True,
                },
                headers=auth,
            )
        ).json()
        check("AHP judgements consistent", ahp["consistent"] and ahp["applied"])

        print("indicators")
        kpi = (
            await client.post(
                f"{API}/kpis",
                json={"name": "Order cycle time", "unit": "h", "target": 18.0, "direction": "cost"},
                headers=auth,
            )
        ).json()
        base = datetime.now(timezone.utc) - timedelta(days=40)
        values = [26.0 - 0.15 * day for day in range(40)]
        values[25] += 9.0  # a genuine excursion the detector should catch
        ingested = await client.post(
            f"{API}/kpis/{kpi['id']}/points",
            json={
                "points": [
                    {"ts": (base + timedelta(days=day)).isoformat(), "value": value}
                    for day, value in enumerate(values)
                ]
            },
            headers=auth,
        )
        check("series ingested", ingested.status_code == 201 and len(ingested.json()) == 40)

        forecast = (
            await client.get(f"{API}/kpis/{kpi['id']}/forecast?horizon=10", headers=auth)
        ).json()
        check("forecast horizon respected", len(forecast["forecast"]) == 10)
        check("forecast trend is downward", forecast["trend_per_period"] < 0)
        check(
            "prediction band contains the point forecast",
            all(
                low <= point <= high
                for low, point, high in zip(
                    forecast["lower"], forecast["forecast"], forecast["upper"], strict=True
                )
            ),
        )

        anomalies = (
            await client.get(f"{API}/kpis/{kpi['id']}/anomalies?window=8", headers=auth)
        ).json()
        check("injected excursion detected", len(anomalies) >= 1, str(anomalies))

        summary = (await client.get(f"{API}/analytics/executive-summary", headers=auth)).json()
        check("summary includes the indicator", len(summary["kpi_health"]) == 1)
        check("portfolio score in range", 0.0 <= summary["portfolio_score"] <= 100.0)

        print("audit")
        trail = (await client.get(f"{API}/admin/audit?limit=200", headers=auth)).json()
        actions = {entry["action"] for entry in trail}
        check("login recorded", "auth.login" in actions)
        check("token reuse recorded", "auth.refresh_reuse" in actions)
        check("simulation recorded", "decision.simulate" in actions)
        check("no secret material stored", ACCOUNT["password"] not in str(trail))

    print(f"\n{PASSED} checks passed")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        for leftover in WORKDIR.glob("*"):
            leftover.unlink(missing_ok=True)
        WORKDIR.rmdir()
