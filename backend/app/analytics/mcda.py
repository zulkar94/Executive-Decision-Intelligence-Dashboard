"""Multi-criteria decision analysis.

Implements weighted sum (SAW) and TOPSIS over a decision matrix, plus AHP
weight derivation with a consistency ratio, and one-at-a-time weight
sensitivity with rank-reversal thresholds.

Conventions
-----------
matrix : ndarray, shape (n_options, n_criteria) of raw scores
weights: ndarray, shape (n_criteria,), non-negative, normalised internally
benefit: bool mask, shape (n_criteria,), True = higher is better
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

EPS = 1e-12

# Saaty random consistency index by matrix order (1..10).
RANDOM_INDEX = {
    1: 0.0,
    2: 0.0,
    3: 0.58,
    4: 0.90,
    5: 1.12,
    6: 1.24,
    7: 1.32,
    8: 1.41,
    9: 1.45,
    10: 1.49,
}


class AnalyticsError(ValueError):
    """Invalid analytics input (shape, sign, or degenerate matrix)."""


@dataclass(slots=True)
class RankedOption:
    index: int
    score: float
    rank: int
    contributions: list[float] = field(default_factory=list)


@dataclass(slots=True)
class SensitivityRow:
    criterion_index: int
    base_weight: float
    swing: float
    flip_weight: float | None
    flip_to_option_index: int | None
    robust: bool


@dataclass(slots=True)
class RankingResult:
    method: str
    ranked: list[RankedOption]
    normalised_weights: list[float]
    spread: float  # winner margin over runner-up, in score units


def _validate(matrix: np.ndarray, weights: np.ndarray, benefit: np.ndarray) -> None:
    if matrix.ndim != 2:
        raise AnalyticsError("Decision matrix must be 2-dimensional")
    n_options, n_criteria = matrix.shape
    if n_options < 1 or n_criteria < 1:
        raise AnalyticsError("Decision matrix must contain at least one option and criterion")
    if weights.shape != (n_criteria,):
        raise AnalyticsError("Weight vector length must match criterion count")
    if benefit.shape != (n_criteria,):
        raise AnalyticsError("Direction mask length must match criterion count")
    if not np.isfinite(matrix).all() or not np.isfinite(weights).all():
        raise AnalyticsError("Decision matrix and weights must be finite")
    if (weights < 0).any():
        raise AnalyticsError("Weights must be non-negative")
    if weights.sum() <= EPS:
        raise AnalyticsError("Weights must not sum to zero")


def normalise_weights(weights: np.ndarray) -> np.ndarray:
    return weights.astype(float) / float(weights.sum())


def min_max_normalise(matrix: np.ndarray, benefit: np.ndarray) -> np.ndarray:
    """Scale each criterion column to [0, 1] with direction applied.

    A constant column carries no discriminating information and is mapped to
    0.5 for every option rather than producing a divide-by-zero.
    """
    columns = matrix.astype(float)
    lo = columns.min(axis=0)
    hi = columns.max(axis=0)
    span = hi - lo
    flat = span <= EPS
    safe_span = np.where(flat, 1.0, span)
    scaled = (columns - lo) / safe_span
    scaled = np.where(benefit, scaled, 1.0 - scaled)
    scaled[:, flat] = 0.5
    return scaled


def weighted_sum(
    matrix: np.ndarray, weights: np.ndarray, benefit: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Return (scores, per-criterion contributions)."""
    _validate(matrix, weights, benefit)
    w = normalise_weights(weights)
    scaled = min_max_normalise(matrix, benefit)
    contributions = scaled * w
    return contributions.sum(axis=1), contributions


def topsis(
    matrix: np.ndarray, weights: np.ndarray, benefit: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Return (closeness coefficients, weighted normalised matrix)."""
    _validate(matrix, weights, benefit)
    w = normalise_weights(weights)
    columns = matrix.astype(float)
    norms = np.sqrt((columns**2).sum(axis=0))
    norms = np.where(norms <= EPS, 1.0, norms)
    weighted = (columns / norms) * w
    ideal_best = np.where(benefit, weighted.max(axis=0), weighted.min(axis=0))
    ideal_worst = np.where(benefit, weighted.min(axis=0), weighted.max(axis=0))
    d_best = np.sqrt(((weighted - ideal_best) ** 2).sum(axis=1))
    d_worst = np.sqrt(((weighted - ideal_worst) ** 2).sum(axis=1))
    denom = d_best + d_worst
    closeness = np.where(denom <= EPS, 0.5, d_worst / np.where(denom <= EPS, 1.0, denom))
    return closeness, weighted


def rank(scores: np.ndarray) -> np.ndarray:
    """Dense competition ranks, 1 = best. Ties share the better rank."""
    order = np.argsort(-scores, kind="stable")
    ranks = np.empty_like(order)
    current = 1
    for position, idx in enumerate(order):
        if position > 0 and not np.isclose(scores[idx], scores[order[position - 1]]):
            current = position + 1
        ranks[idx] = current
    return ranks


def evaluate(
    matrix: np.ndarray, weights: np.ndarray, benefit: np.ndarray, method: str = "weighted_sum"
) -> RankingResult:
    if method == "weighted_sum":
        scores, contributions = weighted_sum(matrix, weights, benefit)
    elif method == "topsis":
        scores, contributions = topsis(matrix, weights, benefit)
    else:
        raise AnalyticsError(f"Unknown method: {method}")
    ranks = rank(scores)
    ordered = np.sort(scores)[::-1]
    spread = float(ordered[0] - ordered[1]) if scores.size > 1 else float(ordered[0])
    ranked = [
        RankedOption(
            index=i,
            score=float(scores[i]),
            rank=int(ranks[i]),
            contributions=[float(v) for v in contributions[i]],
        )
        for i in range(scores.size)
    ]
    ranked.sort(key=lambda item: (item.rank, -item.score))
    return RankingResult(
        method=method,
        ranked=ranked,
        normalised_weights=[float(v) for v in normalise_weights(weights)],
        spread=spread,
    )


def ahp_weights(pairwise: np.ndarray) -> tuple[np.ndarray, float, float]:
    """Derive weights from a Saaty pairwise matrix via power iteration.

    Returns (weights, lambda_max, consistency_ratio). CR > 0.10 conventionally
    means the judgements are too inconsistent to trust.
    """
    a = np.asarray(pairwise, dtype=float)
    if a.ndim != 2 or a.shape[0] != a.shape[1]:
        raise AnalyticsError("Pairwise matrix must be square")
    n = a.shape[0]
    if n < 2:
        raise AnalyticsError("Pairwise matrix must have at least 2 criteria")
    if not np.isfinite(a).all() or (a <= 0).any():
        raise AnalyticsError("Pairwise entries must be finite and positive")

    vector = np.full(n, 1.0 / n)
    lambda_max = float(n)
    for _ in range(200):
        product = a @ vector
        total = product.sum()
        if total <= EPS:
            raise AnalyticsError("Degenerate pairwise matrix")
        updated = product / total
        if np.abs(updated - vector).max() < 1e-12:
            vector = updated
            break
        vector = updated
    weighted = a @ vector
    lambda_max = float((weighted / np.where(vector <= EPS, EPS, vector)).mean())
    consistency_index = (lambda_max - n) / (n - 1)
    random_index = RANDOM_INDEX.get(n, 1.49 + 0.02 * (n - 10))
    ratio = 0.0 if random_index <= EPS else consistency_index / random_index
    return vector, lambda_max, float(max(ratio, 0.0))


def sensitivity(
    matrix: np.ndarray,
    weights: np.ndarray,
    benefit: np.ndarray,
    method: str = "weighted_sum",
    steps: int = 41,
) -> list[SensitivityRow]:
    """One-at-a-time weight sweep per criterion.

    For each criterion the weight is swept across [0, 1] (remaining criteria
    keep their relative proportions). Reports the swing in the incumbent
    winner's score and the smallest weight at which the winner changes.
    """
    base = evaluate(matrix, weights, benefit, method)
    incumbent = base.ranked[0].index
    w = normalise_weights(weights)
    grid = np.linspace(0.0, 1.0, max(3, steps))
    report: list[SensitivityRow] = []

    for j in range(w.size):
        others = np.delete(w, j)
        others_total = float(others.sum())
        winner_scores: list[float] = []
        flip_at: float | None = None
        flip_to: int | None = None
        for candidate in grid:
            trial = np.empty_like(w)
            trial[j] = candidate
            if others_total <= EPS:
                trial[np.arange(w.size) != j] = (1.0 - candidate) / max(w.size - 1, 1)
            else:
                trial[np.arange(w.size) != j] = others * (1.0 - candidate) / others_total
            if trial.sum() <= EPS:
                continue
            result = evaluate(matrix, trial, benefit, method)
            winner_scores.append(result.ranked[0].score)
            if flip_at is None and result.ranked[0].index != incumbent:
                flip_at = float(candidate)
                flip_to = int(result.ranked[0].index)
        swing = float(max(winner_scores) - min(winner_scores)) if winner_scores else 0.0
        report.append(
            SensitivityRow(
                criterion_index=j,
                base_weight=float(w[j]),
                swing=swing,
                flip_weight=flip_at,
                flip_to_option_index=flip_to,
                robust=flip_at is None,
            )
        )
    report.sort(key=lambda item: item.swing, reverse=True)
    return report
