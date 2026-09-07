"""Idempotent bootstrap and demo data. Never invoked in production paths."""

from __future__ import annotations

import math
import os
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.db import SessionLocal
from app.models import (
    Criterion,
    Decision,
    DecisionStatus,
    Direction,
    Kpi,
    KpiPoint,
    Option,
    Role,
    Score,
    User,
)
from app.security.passwords import hash_password, validate_password

DEMO_EMAIL = "analyst@edid.io"


async def ensure_admin(email: str, password: str) -> None:
    """Create the first admin if the table is empty; never overwrite one."""
    validate_password(password, email=email)
    async with SessionLocal() as session:
        existing = await session.scalar(select(User).where(User.email == email.lower()))
        if existing is not None:
            return
        session.add(
            User(
                email=email.lower(),
                password_hash=hash_password(password),
                full_name="Bootstrap Admin",
                role=Role.admin,
            )
        )
        await session.commit()


async def seed_demo(password: str | None = None) -> str:
    """Populate a realistic portfolio. Returns the demo decision id.

    The demo account password comes from ``EDID_DEMO_PASSWORD`` when set; a
    random one is generated otherwise so an unattended seed never installs a
    guessable account.
    """
    password = password or os.environ.get("EDID_DEMO_PASSWORD") or secrets.token_urlsafe(18)
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.email == DEMO_EMAIL))
        if user is None:
            user = User(
                email=DEMO_EMAIL,
                password_hash=hash_password(password),
                full_name="Demo Analyst",
                role=Role.analyst,
            )
            session.add(user)
            await session.flush()

        existing = await session.scalar(
            select(Decision).where(Decision.owner_id == user.id).limit(1)
        )
        if existing is not None:
            return existing.id

        decision = Decision(
            title="Cloud migration strategy for FY27",
            description="Choose the target platform for the core ledger workload.",
            status=DecisionStatus.review,
            owner_id=user.id,
        )
        session.add(decision)
        await session.flush()

        criteria_spec = [
            ("Annual run cost", 0.30, Direction.cost),
            ("Migration risk", 0.25, Direction.cost),
            ("Time to value", 0.20, Direction.cost),
            ("Scalability headroom", 0.15, Direction.benefit),
            ("Team readiness", 0.10, Direction.benefit),
        ]
        criteria = [
            Criterion(decision_id=decision.id, name=name, weight=weight, direction=direction)
            for name, weight, direction in criteria_spec
        ]
        options = [
            Option(decision_id=decision.id, name=name)
            for name in ("Lift and shift", "Re-platform to managed services", "Hybrid staged")
        ]
        session.add_all([*criteria, *options])
        await session.flush()

        # rows = options, cols = criteria (cost, risk, ttv, scale, readiness)
        table = [
            [4.2, 3.0, 4.0, 5.0, 8.0],
            [3.1, 7.5, 9.0, 9.0, 5.0],
            [3.6, 5.0, 6.5, 7.5, 7.0],
        ]
        spread = [
            [0.4, 0.8, 0.6, 0.5, 0.7],
            [0.5, 1.4, 1.5, 0.4, 1.2],
            [0.4, 1.0, 1.0, 0.6, 0.9],
        ]
        session.add_all(
            Score(
                option_id=options[i].id,
                criterion_id=criteria[j].id,
                value=table[i][j],
                uncertainty=spread[i][j],
            )
            for i in range(len(options))
            for j in range(len(criteria))
        )

        kpi_spec = [
            ("Gross margin", "%", 62.0, Direction.benefit, 58.0, 0.35),
            ("Monthly cloud spend", "kUSD", 240.0, Direction.cost, 268.0, -0.9),
            ("Delivery lead time", "days", 12.0, Direction.cost, 17.0, -0.12),
        ]
        start = datetime.now(UTC) - timedelta(days=59)
        for name, unit, target, direction, base, slope in kpi_spec:
            kpi = Kpi(name=name, unit=unit, target=target, direction=direction)
            session.add(kpi)
            await session.flush()
            session.add_all(
                KpiPoint(
                    kpi_id=kpi.id,
                    ts=start + timedelta(days=day),
                    value=round(
                        base
                        + slope * day
                        + 0.6 * math.sin(day / 4.0)
                        + (4.0 if day == 45 and name == "Monthly cloud spend" else 0.0),
                        3,
                    ),
                )
                for day in range(60)
            )
        await session.commit()
        return decision.id
