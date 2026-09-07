"""Ranking, sensitivity, Monte Carlo, AHP weighting, executive summary."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Literal

import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select

from app import audit
from app.analytics import mcda, risk
from app.analytics.anomaly import detect, volatility
from app.config import get_settings
from app.deps import CurrentUser, SessionDep, owned_decision, rate_limit, writable_decision
from app.http_status import HTTP_422_UNPROCESSABLE
from app.matrix import build_matrix
from app.models import Decision, DecisionStatus, Kpi, KpiPoint, Role
from app.schemas import (
    AhpOut,
    AhpRequest,
    ExecutiveSummaryOut,
    KpiHealthOut,
    RankedOptionOut,
    RankingOut,
    SensitivityOut,
    SensitivityRowOut,
    SimulatedOptionOut,
    SimulationOut,
    SimulationRequest,
)

router = APIRouter(tags=["analytics"], dependencies=[Depends(rate_limit(scope="analytics"))])

Method = Literal["weighted_sum", "topsis"]


def _analytics_error(exc: mcda.AnalyticsError) -> HTTPException:
    return HTTPException(status_code=HTTP_422_UNPROCESSABLE, detail=str(exc))


@router.get("/decisions/{decision_id}/ranking", response_model=RankingOut)
async def ranking(
    decision_id: str,
    user: CurrentUser,
    session: SessionDep,
    method: Method = Query(default="weighted_sum"),
) -> RankingOut:
    decision = await owned_decision(decision_id, session, user)
    dm = await build_matrix(session, decision)
    try:
        result = mcda.evaluate(dm.values, dm.weights, dm.benefit, method)
    except mcda.AnalyticsError as exc:
        raise _analytics_error(exc) from exc

    criterion_ids = dm.criterion_ids
    return RankingOut(
        decision_id=decision.id,
        method=method,
        ranked=[
            RankedOptionOut(
                option_id=dm.options[item.index].id,
                name=dm.options[item.index].name,
                score=item.score,
                rank=item.rank,
                contributions={
                    criterion_ids[j]: value for j, value in enumerate(item.contributions)
                },
            )
            for item in result.ranked
        ],
        weights=dict(zip(criterion_ids, result.normalised_weights, strict=True)),
        winner_margin=result.spread,
        generated_at=datetime.now(UTC),
    )


@router.get("/decisions/{decision_id}/sensitivity", response_model=SensitivityOut)
async def sensitivity(
    decision_id: str,
    user: CurrentUser,
    session: SessionDep,
    method: Method = Query(default="weighted_sum"),
    steps: int = Query(default=41, ge=5, le=101),
) -> SensitivityOut:
    decision = await owned_decision(decision_id, session, user)
    dm = await build_matrix(session, decision)
    try:
        # Sweep is O(criteria x steps) numpy work; keep the event loop free.
        rows = await asyncio.to_thread(
            mcda.sensitivity, dm.values, dm.weights, dm.benefit, method, steps
        )
        incumbent = mcda.evaluate(dm.values, dm.weights, dm.benefit, method).ranked[0].index
    except mcda.AnalyticsError as exc:
        raise _analytics_error(exc) from exc

    return SensitivityOut(
        decision_id=decision.id,
        method=method,
        incumbent_option=dm.options[incumbent].name,
        rows=[
            SensitivityRowOut(
                criterion_id=dm.criteria[row.criterion_index].id,
                criterion=dm.criteria[row.criterion_index].name,
                base_weight=row.base_weight,
                swing=row.swing,
                flip_weight=row.flip_weight,
                flip_to_option=(
                    dm.options[row.flip_to_option_index].name
                    if row.flip_to_option_index is not None
                    else None
                ),
                robust=row.robust,
            )
            for row in rows
        ],
    )


@router.post("/decisions/{decision_id}/simulate", response_model=SimulationOut)
async def simulate(
    decision_id: str,
    payload: SimulationRequest,
    user: CurrentUser,
    request: Request,
    session: SessionDep,
) -> SimulationOut:
    decision = await owned_decision(decision_id, session, user)
    settings = get_settings()
    if payload.runs > settings.max_simulation_runs:
        raise HTTPException(status_code=HTTP_422_UNPROCESSABLE, detail="Too many simulation runs")
    dm = await build_matrix(session, decision)
    uncertainty = dm.uncertainty.copy()
    if payload.default_uncertainty > 0:
        # Cells with no stated uncertainty inherit the caller's default.
        uncertainty = np.where(uncertainty <= 0, payload.default_uncertainty, uncertainty)
    if uncertainty.sum() <= 0 and payload.weight_jitter <= 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Simulation needs score uncertainty, default_uncertainty, or weight_jitter",
        )
    try:
        result = await asyncio.to_thread(
            risk.simulate,
            dm.values,
            uncertainty,
            dm.weights,
            dm.benefit,
            runs=payload.runs,
            seed=payload.seed,
            weight_jitter=payload.weight_jitter,
        )
    except mcda.AnalyticsError as exc:
        raise _analytics_error(exc) from exc

    await audit.record(
        session,
        action="decision.simulate",
        actor_id=user.id,
        entity="decision",
        entity_id=decision.id,
        request=request,
        meta={"runs": result.runs, "seed": result.seed},
    )
    return SimulationOut(
        decision_id=decision.id,
        runs=result.runs,
        seed=result.seed,
        decision_confidence=result.decision_confidence,
        options=[
            SimulatedOptionOut(
                option_id=dm.options[item.index].id,
                name=dm.options[item.index].name,
                win_probability=item.win_probability,
                mean_score=item.mean_score,
                p05=item.p05,
                p50=item.p50,
                p95=item.p95,
                rank_distribution=item.rank_distribution,
            )
            for item in result.options
        ],
    )


@router.post("/decisions/{decision_id}/weights/ahp", response_model=AhpOut)
async def ahp(
    decision_id: str,
    payload: AhpRequest,
    user: CurrentUser,
    request: Request,
    session: SessionDep,
) -> AhpOut:
    decision = (
        await writable_decision(decision_id, session, user)
        if payload.apply_weights
        else await owned_decision(decision_id, session, user)
    )
    dm = await build_matrix(session, decision)
    if len(payload.matrix) != len(dm.criteria):
        raise HTTPException(
            status_code=HTTP_422_UNPROCESSABLE,
            detail=f"Pairwise matrix must be {len(dm.criteria)}x{len(dm.criteria)}",
        )
    try:
        weights, lambda_max, ratio = mcda.ahp_weights(np.asarray(payload.matrix, dtype=float))
    except mcda.AnalyticsError as exc:
        raise _analytics_error(exc) from exc

    consistent = ratio <= 0.10
    applied = False
    if payload.apply_weights:
        if not consistent:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Consistency ratio {ratio:.3f} exceeds 0.10; revise the judgements",
            )
        for criterion, weight in zip(dm.criteria, weights, strict=True):
            criterion.weight = float(weight)
        applied = True
        await audit.record(
            session,
            action="decision.weights_ahp",
            actor_id=user.id,
            entity="decision",
            entity_id=decision.id,
            request=request,
            meta={"consistency_ratio": round(ratio, 4)},
        )
    return AhpOut(
        weights={
            criterion.id: float(weight)
            for criterion, weight in zip(dm.criteria, weights, strict=True)
        },
        lambda_max=lambda_max,
        consistency_ratio=ratio,
        consistent=consistent,
        applied=applied,
    )


@router.get("/analytics/executive-summary", response_model=ExecutiveSummaryOut)
async def executive_summary(user: CurrentUser, session: SessionDep) -> ExecutiveSummaryOut:
    """Portfolio-level view: KPI health plus decisions whose winner is fragile."""
    kpis = list((await session.scalars(select(Kpi).order_by(Kpi.name))).all())
    health: list[KpiHealthOut] = []
    for kpi in kpis:
        points = list(
            (
                await session.scalars(
                    select(KpiPoint).where(KpiPoint.kpi_id == kpi.id).order_by(KpiPoint.ts)
                )
            ).all()
        )
        values = [point.value for point in points]
        health.append(_kpi_health(kpi, values))

    query = select(Decision).where(Decision.status != DecisionStatus.archived)
    if user.role is not Role.admin:
        query = query.where(Decision.owner_id == user.id)
    decisions = list((await session.scalars(query.limit(25))).all())

    fragile: list[dict[str, object]] = []
    for decision in decisions:
        try:
            dm = await build_matrix(session, decision)
            result = mcda.evaluate(dm.values, dm.weights, dm.benefit)
        except (HTTPException, mcda.AnalyticsError):
            continue
        if result.spread < 0.05:  # winner within 5 points of the runner-up
            fragile.append(
                {
                    "decision_id": decision.id,
                    "title": decision.title,
                    "winner": dm.options[result.ranked[0].index].name,
                    "margin": round(result.spread, 4),
                }
            )

    scored = [item for item in health if item.status != "insufficient_data"]
    weights = {"on_track": 1.0, "watch": 0.6, "off_track": 0.15}
    portfolio = (
        round(sum(weights[item.status] for item in scored) / len(scored) * 100.0, 2)
        if scored
        else 0.0
    )
    open_decisions = await session.scalar(
        select(func.count())
        .select_from(Decision)
        .where(Decision.status.in_([DecisionStatus.draft, DecisionStatus.review]))
    )
    return ExecutiveSummaryOut(
        generated_at=datetime.now(UTC),
        kpi_health=health,
        portfolio_score=portfolio,
        open_decisions=int(open_decisions or 0),
        fragile_decisions=fragile,
    )


def _kpi_health(kpi: Kpi, values: list[float]) -> KpiHealthOut:
    if len(values) < 3:
        return KpiHealthOut(
            kpi_id=kpi.id,
            name=kpi.name,
            latest=values[-1] if values else None,
            change_pct=None,
            to_target_pct=None,
            volatility=0.0,
            anomalies=0,
            status="insufficient_data",
        )
    latest = values[-1]
    previous = values[-2]
    change = ((latest - previous) / abs(previous) * 100.0) if abs(previous) > 1e-9 else None
    to_target = (
        ((latest - kpi.target) / abs(kpi.target) * 100.0)
        if kpi.target is not None and abs(kpi.target) > 1e-9
        else None
    )
    anomalies = detect(values)
    vol = volatility(values)

    status_label: str = "on_track"
    if to_target is not None:
        favourable = to_target >= 0 if kpi.direction.value == "benefit" else to_target <= 0
        if not favourable:
            status_label = "off_track" if abs(to_target) > 10 else "watch"
    if anomalies and status_label == "on_track":
        status_label = "watch"
    if vol > 0.35 and status_label != "off_track":
        status_label = "watch"
    return KpiHealthOut(
        kpi_id=kpi.id,
        name=kpi.name,
        latest=latest,
        change_pct=change,
        to_target_pct=to_target,
        volatility=round(vol, 4),
        anomalies=len(anomalies),
        status=status_label,  # type: ignore[arg-type]
    )
