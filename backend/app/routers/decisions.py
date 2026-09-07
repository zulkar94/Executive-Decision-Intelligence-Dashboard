"""Decision, criterion, option, and score management."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app import audit
from app.config import get_settings
from app.deps import (
    CurrentUser,
    RequireAnalyst,
    SessionDep,
    owned_decision,
    rate_limit,
    writable_decision,
)
from app.http_status import HTTP_422_UNPROCESSABLE
from app.models import Criterion, Decision, Option, Role, Score
from app.schemas import (
    CriterionIn,
    CriterionOut,
    DecisionIn,
    DecisionOut,
    DecisionPatch,
    OptionIn,
    OptionOut,
    ScoreBulkIn,
    ScoreOut,
)

router = APIRouter(
    prefix="/decisions", tags=["decisions"], dependencies=[Depends(rate_limit(scope="decisions"))]
)

CONFLICT = HTTPException(
    status_code=status.HTTP_409_CONFLICT, detail="Name already used in this decision"
)


@router.get("", response_model=list[DecisionOut])
async def list_decisions(
    user: CurrentUser,
    session: SessionDep,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[Decision]:
    query = select(Decision).order_by(Decision.updated_at.desc()).limit(limit).offset(offset)
    if user.role is not Role.admin:
        query = query.where(Decision.owner_id == user.id)
    return list((await session.scalars(query)).all())


@router.post("", response_model=DecisionOut, status_code=status.HTTP_201_CREATED)
async def create_decision(
    payload: DecisionIn, user: RequireAnalyst, request: Request, session: SessionDep
) -> Decision:
    decision = Decision(**payload.model_dump(), owner_id=user.id)
    session.add(decision)
    await session.flush()
    # Load the (empty) collections now; serialisation happens after the session
    # closes, where a lazy load would raise MissingGreenlet.
    await session.refresh(decision, attribute_names=["criteria", "options"])
    await audit.record(
        session,
        action="decision.create",
        actor_id=user.id,
        entity="decision",
        entity_id=decision.id,
        request=request,
    )
    return decision


@router.get("/{decision_id}", response_model=DecisionOut)
async def get_decision(decision_id: str, user: CurrentUser, session: SessionDep) -> Decision:
    return await owned_decision(decision_id, session, user)


@router.patch("/{decision_id}", response_model=DecisionOut)
async def patch_decision(
    decision_id: str,
    payload: DecisionPatch,
    user: CurrentUser,
    request: Request,
    session: SessionDep,
) -> Decision:
    decision = await writable_decision(decision_id, session, user)
    changes = payload.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(decision, field, value)
    await session.flush()
    await audit.record(
        session,
        action="decision.update",
        actor_id=user.id,
        entity="decision",
        entity_id=decision.id,
        request=request,
        meta={"fields": sorted(changes)},
    )
    return decision


@router.delete("/{decision_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_decision(
    decision_id: str, user: CurrentUser, request: Request, session: SessionDep
) -> Response:
    decision = await writable_decision(decision_id, session, user)
    await session.delete(decision)
    await audit.record(
        session,
        action="decision.delete",
        actor_id=user.id,
        entity="decision",
        entity_id=decision_id,
        request=request,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- criteria --------------------------------------------------------------
@router.post(
    "/{decision_id}/criteria", response_model=CriterionOut, status_code=status.HTTP_201_CREATED
)
async def add_criterion(
    decision_id: str,
    payload: CriterionIn,
    user: CurrentUser,
    request: Request,
    session: SessionDep,
) -> Criterion:
    decision = await writable_decision(decision_id, session, user)
    settings = get_settings()
    count = len(
        (
            await session.scalars(select(Criterion.id).where(Criterion.decision_id == decision.id))
        ).all()
    )
    if count >= settings.max_criteria_per_decision:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Criterion limit reached")
    criterion = Criterion(decision_id=decision.id, **payload.model_dump())
    session.add(criterion)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise CONFLICT from exc
    await audit.record(
        session,
        action="criterion.create",
        actor_id=user.id,
        entity="criterion",
        entity_id=criterion.id,
        request=request,
        meta={"decision_id": decision.id},
    )
    return criterion


@router.delete("/{decision_id}/criteria/{criterion_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_criterion(
    decision_id: str,
    criterion_id: str,
    user: CurrentUser,
    request: Request,
    session: SessionDep,
) -> Response:
    decision = await writable_decision(decision_id, session, user)
    criterion = await session.get(Criterion, criterion_id)
    if criterion is None or criterion.decision_id != decision.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Criterion not found")
    await session.delete(criterion)
    await audit.record(
        session,
        action="criterion.delete",
        actor_id=user.id,
        entity="criterion",
        entity_id=criterion_id,
        request=request,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- options ---------------------------------------------------------------
@router.post(
    "/{decision_id}/options", response_model=OptionOut, status_code=status.HTTP_201_CREATED
)
async def add_option(
    decision_id: str,
    payload: OptionIn,
    user: CurrentUser,
    request: Request,
    session: SessionDep,
) -> Option:
    decision = await writable_decision(decision_id, session, user)
    settings = get_settings()
    count = len(
        (await session.scalars(select(Option.id).where(Option.decision_id == decision.id))).all()
    )
    if count >= settings.max_options_per_decision:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Option limit reached")
    option = Option(decision_id=decision.id, **payload.model_dump())
    session.add(option)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise CONFLICT from exc
    await audit.record(
        session,
        action="option.create",
        actor_id=user.id,
        entity="option",
        entity_id=option.id,
        request=request,
        meta={"decision_id": decision.id},
    )
    return option


@router.delete("/{decision_id}/options/{option_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_option(
    decision_id: str,
    option_id: str,
    user: CurrentUser,
    request: Request,
    session: SessionDep,
) -> Response:
    decision = await writable_decision(decision_id, session, user)
    option = await session.get(Option, option_id)
    if option is None or option.decision_id != decision.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Option not found")
    await session.delete(option)
    await audit.record(
        session,
        action="option.delete",
        actor_id=user.id,
        entity="option",
        entity_id=option_id,
        request=request,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- scores ----------------------------------------------------------------
@router.get("/{decision_id}/scores", response_model=list[ScoreOut])
async def list_scores(decision_id: str, user: CurrentUser, session: SessionDep) -> list[Score]:
    decision = await owned_decision(decision_id, session, user)
    option_ids = list(
        (await session.scalars(select(Option.id).where(Option.decision_id == decision.id))).all()
    )
    if not option_ids:
        return []
    return list((await session.scalars(select(Score).where(Score.option_id.in_(option_ids)))).all())


@router.put("/{decision_id}/scores", response_model=list[ScoreOut])
async def upsert_scores(
    decision_id: str,
    payload: ScoreBulkIn,
    user: CurrentUser,
    request: Request,
    session: SessionDep,
) -> list[Score]:
    """Idempotent bulk upsert of matrix cells belonging to this decision only."""
    decision = await writable_decision(decision_id, session, user)
    valid_options = set(
        (await session.scalars(select(Option.id).where(Option.decision_id == decision.id))).all()
    )
    valid_criteria = set(
        (
            await session.scalars(select(Criterion.id).where(Criterion.decision_id == decision.id))
        ).all()
    )

    for item in payload.scores:
        # Reject cross-decision writes outright: this is the IDOR boundary.
        if item.option_id not in valid_options or item.criterion_id not in valid_criteria:
            raise HTTPException(
                status_code=HTTP_422_UNPROCESSABLE,
                detail="Score references an option or criterion outside this decision",
            )

    existing = {
        (score.option_id, score.criterion_id): score
        for score in (
            await session.scalars(select(Score).where(Score.option_id.in_(valid_options)))
        ).all()
    }
    touched: list[Score] = []
    for item in payload.scores:
        key = (item.option_id, item.criterion_id)
        score = existing.get(key)
        if score is None:
            score = Score(**item.model_dump())
            session.add(score)
            existing[key] = score
        else:
            score.value = item.value
            score.uncertainty = item.uncertainty
        touched.append(score)
    await session.flush()
    await audit.record(
        session,
        action="score.upsert",
        actor_id=user.id,
        entity="decision",
        entity_id=decision.id,
        request=request,
        meta={"cells": len(touched)},
    )
    return touched


@router.delete("/{decision_id}/scores", status_code=status.HTTP_204_NO_CONTENT)
async def clear_scores(
    decision_id: str, user: CurrentUser, request: Request, session: SessionDep
) -> Response:
    decision = await writable_decision(decision_id, session, user)
    option_ids = list(
        (await session.scalars(select(Option.id).where(Option.decision_id == decision.id))).all()
    )
    if option_ids:
        await session.execute(delete(Score).where(Score.option_id.in_(option_ids)))
    await audit.record(
        session,
        action="score.clear",
        actor_id=user.id,
        entity="decision",
        entity_id=decision.id,
        request=request,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
