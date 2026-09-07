"""Monte Carlo simulation over an uncertain decision matrix."""

from __future__ import annotations

import secrets
from dataclasses import dataclass

import numpy as np

from app.analytics.mcda import AnalyticsError, evaluate, min_max_normalise, normalise_weights


@dataclass(slots=True)
class SimulatedOption:
    index: int
    win_probability: float
    mean_score: float
    p05: float
    p50: float
    p95: float
    rank_distribution: dict[int, float]


@dataclass(slots=True)
class SimulationResult:
    runs: int
    seed: int
    options: list[SimulatedOption]
    decision_confidence: float  # win probability of the deterministic winner


def simulate(
    matrix: np.ndarray,
    uncertainty: np.ndarray,
    weights: np.ndarray,
    benefit: np.ndarray,
    *,
    runs: int = 5_000,
    seed: int | None = None,
    weight_jitter: float = 0.0,
) -> SimulationResult:
    """Sample scores as Normal(value, uncertainty) and re-rank each draw.

    ``weight_jitter`` (0..1) additionally perturbs the weight vector with a
    Dirichlet draw concentrated on the stated weights, which models the fact
    that stakeholder weights are themselves estimates.
    """
    if runs < 1:
        raise AnalyticsError("runs must be positive")
    if matrix.shape != uncertainty.shape:
        raise AnalyticsError("Uncertainty matrix shape must match the decision matrix")
    if (uncertainty < 0).any():
        raise AnalyticsError("Uncertainty must be non-negative")
    if not 0.0 <= weight_jitter <= 1.0:
        raise AnalyticsError("weight_jitter must be within [0, 1]")

    seed = secrets.randbits(32) if seed is None else int(seed)
    rng = np.random.default_rng(seed)
    n_options, n_criteria = matrix.shape
    base_weights = normalise_weights(weights)

    draws = rng.normal(
        loc=matrix[None, :, :], scale=uncertainty[None, :, :], size=(runs, n_options, n_criteria)
    )

    if weight_jitter > 0.0:
        # Higher jitter -> lower Dirichlet concentration -> more weight dispersion.
        concentration = base_weights * (1.0 / max(weight_jitter, 1e-3)) * n_criteria
        concentration = np.clip(concentration, 1e-3, None)
        sampled_weights = rng.dirichlet(concentration, size=runs)
    else:
        sampled_weights = np.repeat(base_weights[None, :], runs, axis=0)

    scores = np.empty((runs, n_options), dtype=float)
    # Chunked to bound peak memory on large run counts.
    chunk = max(1, min(runs, 2_000))
    for start in range(0, runs, chunk):
        stop = min(start + chunk, runs)
        block = draws[start:stop]
        scaled = np.stack([min_max_normalise(block[i], benefit) for i in range(stop - start)])
        scores[start:stop] = np.einsum("rij,rj->ri", scaled, sampled_weights[start:stop])

    winners = np.argmax(scores, axis=1)
    win_counts = np.bincount(winners, minlength=n_options) / runs
    order = np.argsort(-scores, axis=1)
    ranks = np.empty_like(order)
    np.put_along_axis(ranks, order, np.arange(1, n_options + 1)[None, :], axis=1)

    deterministic = evaluate(matrix, weights, benefit).ranked[0].index
    options = []
    for i in range(n_options):
        column = scores[:, i]
        counts = np.bincount(ranks[:, i], minlength=n_options + 1)[1:]
        options.append(
            SimulatedOption(
                index=i,
                win_probability=float(win_counts[i]),
                mean_score=float(column.mean()),
                p05=float(np.percentile(column, 5)),
                p50=float(np.percentile(column, 50)),
                p95=float(np.percentile(column, 95)),
                rank_distribution={int(r + 1): float(counts[r] / runs) for r in range(n_options)},
            )
        )
    options.sort(key=lambda item: item.win_probability, reverse=True)
    return SimulationResult(
        runs=runs,
        seed=seed,
        options=options,
        decision_confidence=float(win_counts[deterministic]),
    )
