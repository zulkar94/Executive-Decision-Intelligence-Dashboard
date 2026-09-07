"""KPI registry, time-series ingestion, forecasting, anomaly detection."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app import audit
from app.analytics import forecast as forecasting
from app.analytics.anomaly import detect
from app.analytics.mcda import AnalyticsError
from app.config import get_settings
from app.deps import CurrentUser, RequireAnalyst, SessionDep, rate_limit
from app.http_status import HTTP_422_UNPROCESSABLE
from app.models import Kpi, KpiPoint
from app.schemas import (
    AnomalyOut,
    ForecastOut,
    KpiIn,
    KpiOut,
    KpiPointOut,
    KpiPointsIn,
)

router = APIRouter(prefix="/kpis", tags=["kpis"], dependencies=[Depends(rate_limit(scope="kpis"))])


async def _get_kpi(session, kpi_id: str) -> Kpi:
    kpi = await session.get(Kpi, kpi_id)
    if kpi is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="KPI not found")
    return kpi


async def _series(session, kpi_id: str, limit: int = 2000) -> list[KpiPoint]:
    return list(
        (
            await session.scalars(
                select(KpiPoint).where(KpiPoint.kpi_id == kpi_id).order_by(KpiPoint.ts).limit(limit)
            )
        ).all()
    )


@router.get("", response_model=list[KpiOut])
async def list_kpis(user: CurrentUser, session: SessionDep) -> list[Kpi]:
    return list((await session.scalars(select(Kpi).order_by(Kpi.name))).all())


@router.post("", response_model=KpiOut, status_code=status.HTTP_201_CREATED)
async def create_kpi(
    payload: KpiIn, user: RequireAnalyst, request: Request, session: SessionDep
) -> Kpi:
    kpi = Kpi(**payload.model_dump())
    session.add(kpi)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="KPI name already exists"
        ) from exc
    await audit.record(
        session,
        action="kpi.create",
        actor_id=user.id,
        entity="kpi",
        entity_id=kpi.id,
        request=request,
    )
    return kpi


@router.delete("/{kpi_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_kpi(
    kpi_id: str, user: RequireAnalyst, request: Request, session: SessionDep
) -> Response:
    kpi = await _get_kpi(session, kpi_id)
    await session.delete(kpi)
    await audit.record(
        session,
        action="kpi.delete",
        actor_id=user.id,
        entity="kpi",
        entity_id=kpi_id,
        request=request,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{kpi_id}/points", response_model=list[KpiPointOut], status_code=status.HTTP_201_CREATED
)
async def ingest_points(
    kpi_id: str,
    payload: KpiPointsIn,
    user: RequireAnalyst,
    request: Request,
    session: SessionDep,
) -> list[KpiPoint]:
    """Upsert observations by timestamp; re-sending a batch is safe."""
    kpi = await _get_kpi(session, kpi_id)
    existing = {point.ts: point for point in await _series(session, kpi.id, limit=10_000)}
    written: list[KpiPoint] = []
    for item in payload.points:
        point = existing.get(item.ts)
        if point is None:
            point = KpiPoint(kpi_id=kpi.id, ts=item.ts, value=item.value)
            session.add(point)
            existing[item.ts] = point
        else:
            point.value = item.value
        written.append(point)
    await session.flush()
    await audit.record(
        session,
        action="kpi.ingest",
        actor_id=user.id,
        entity="kpi",
        entity_id=kpi.id,
        request=request,
        meta={"points": len(written)},
    )
    return written


@router.get("/{kpi_id}/points", response_model=list[KpiPointOut])
async def list_points(
    kpi_id: str,
    user: CurrentUser,
    session: SessionDep,
    limit: int = Query(default=500, ge=1, le=2000),
) -> list[KpiPoint]:
    kpi = await _get_kpi(session, kpi_id)
    return await _series(session, kpi.id, limit=limit)


@router.get("/{kpi_id}/forecast", response_model=ForecastOut)
async def kpi_forecast(
    kpi_id: str,
    user: CurrentUser,
    session: SessionDep,
    horizon: int = Query(default=7, ge=1),
    with_backtest: bool = Query(default=True),
) -> ForecastOut:
    settings = get_settings()
    if horizon > settings.max_forecast_horizon:
        raise HTTPException(status_code=HTTP_422_UNPROCESSABLE, detail="Horizon exceeds limit")
    kpi = await _get_kpi(session, kpi_id)
    values = [point.value for point in await _series(session, kpi.id)]
    try:
        # Grid search is CPU-bound; offload so concurrent requests stay responsive.
        result = await asyncio.to_thread(forecasting.forecast, values, horizon)
        holdout = min(3, max(1, len(values) // 5))
        backtest = (
            await asyncio.to_thread(forecasting.backtest, values, holdout)
            if with_backtest and len(values) >= forecasting.MIN_HISTORY + holdout
            else None
        )
    except AnalyticsError as exc:
        raise HTTPException(status_code=HTTP_422_UNPROCESSABLE, detail=str(exc)) from exc
    return ForecastOut(
        kpi_id=kpi.id,
        horizon=result.horizon,
        alpha=result.alpha,
        beta=result.beta,
        forecast=result.forecast,
        lower=result.lower,
        upper=result.upper,
        trend_per_period=result.trend_per_period,
        mape=result.mape,
        backtest=backtest,
    )


@router.get("/{kpi_id}/anomalies", response_model=list[AnomalyOut])
async def kpi_anomalies(
    kpi_id: str,
    user: CurrentUser,
    session: SessionDep,
    threshold: float = Query(default=3.5, ge=1.0, le=10.0),
    window: int = Query(default=0, ge=0, le=200),
) -> list[AnomalyOut]:
    kpi = await _get_kpi(session, kpi_id)
    points = await _series(session, kpi.id)
    try:
        found = detect([point.value for point in points], threshold=threshold, window=window)
    except AnalyticsError as exc:
        raise HTTPException(status_code=HTTP_422_UNPROCESSABLE, detail=str(exc)) from exc
    return [
        AnomalyOut(ts=points[item.index].ts, value=item.value, score=item.score, kind=item.kind)
        for item in found
    ]
