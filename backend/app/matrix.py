"""Translate persisted decisions into numpy matrices for the analytics layer."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.http_status import HTTP_413_TOO_LARGE
from app.models import Criterion, Decision, Direction, Option, Score


@dataclass(slots=True)
class DecisionMatrix:
    options: list[Option]
    criteria: list[Criterion]
    values: np.ndarray
    uncertainty: np.ndarray
    weights: np.ndarray
    benefit: np.ndarray
    missing_cells: int

    @property
    def option_ids(self) -> list[str]:
        return [option.id for option in self.options]

    @property
    def criterion_ids(self) -> list[str]:
        return [criterion.id for criterion in self.criteria]


async def build_matrix(session: AsyncSession, decision: Decision) -> DecisionMatrix:
    """Assemble the decision matrix, defaulting absent cells to 0.

    Raises 409 when the decision is not yet analysable so that callers get an
    actionable message instead of a numpy error.
    """
    settings = get_settings()
    criteria = list(
        (
            await session.scalars(
                select(Criterion)
                .where(Criterion.decision_id == decision.id)
                .order_by(Criterion.name)
            )
        ).all()
    )
    options = list(
        (
            await session.scalars(
                select(Option).where(Option.decision_id == decision.id).order_by(Option.name)
            )
        ).all()
    )
    if len(options) < 2:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Decision needs at least 2 options"
        )
    if not criteria:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Decision needs at least 1 criterion"
        )
    if len(options) > settings.max_options_per_decision:
        raise HTTPException(status_code=HTTP_413_TOO_LARGE, detail="Too many options")
    if len(criteria) > settings.max_criteria_per_decision:
        raise HTTPException(status_code=HTTP_413_TOO_LARGE, detail="Too many criteria")

    option_index = {option.id: i for i, option in enumerate(options)}
    criterion_index = {criterion.id: j for j, criterion in enumerate(criteria)}

    values = np.zeros((len(options), len(criteria)), dtype=float)
    uncertainty = np.zeros_like(values)
    filled = np.zeros_like(values, dtype=bool)

    scores = await session.scalars(select(Score).where(Score.option_id.in_(list(option_index))))
    for score in scores:
        i = option_index.get(score.option_id)
        j = criterion_index.get(score.criterion_id)
        if i is None or j is None:
            continue
        values[i, j] = score.value
        uncertainty[i, j] = score.uncertainty
        filled[i, j] = True

    weights = np.array([criterion.weight for criterion in criteria], dtype=float)
    if weights.sum() <= 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Criterion weights must not sum to zero"
        )
    benefit = np.array(
        [criterion.direction is Direction.benefit for criterion in criteria], dtype=bool
    )
    return DecisionMatrix(
        options=options,
        criteria=criteria,
        values=values,
        uncertainty=uncertainty,
        weights=weights,
        benefit=benefit,
        missing_cells=int((~filled).sum()),
    )
