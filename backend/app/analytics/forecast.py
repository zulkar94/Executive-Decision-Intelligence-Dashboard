"""Holt linear-trend exponential smoothing with grid-searched parameters."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from app.analytics.mcda import AnalyticsError

MIN_HISTORY = 4


@dataclass(slots=True)
class ForecastResult:
    horizon: int
    alpha: float
    beta: float
    fitted: list[float]
    forecast: list[float]
    lower: list[float]
    upper: list[float]
    residual_std: float
    mape: float | None
    trend_per_period: float


def _holt(series: np.ndarray, alpha: float, beta: float) -> tuple[np.ndarray, float, float]:
    level = float(series[0])
    trend = float(series[1] - series[0])
    fitted = np.empty_like(series, dtype=float)
    fitted[0] = level
    for t in range(1, series.size):
        prediction = level + trend
        fitted[t] = prediction
        error = series[t] - prediction
        level = prediction + alpha * error
        trend = trend + alpha * beta * error
    return fitted, level, trend


def forecast(
    values: list[float] | np.ndarray, horizon: int = 7, *, z: float = 1.96
) -> ForecastResult:
    series = np.asarray(values, dtype=float)
    if series.ndim != 1:
        raise AnalyticsError("Series must be one-dimensional")
    if series.size < MIN_HISTORY:
        raise AnalyticsError(f"At least {MIN_HISTORY} observations are required")
    if not np.isfinite(series).all():
        raise AnalyticsError("Series must be finite")
    if horizon < 1:
        raise AnalyticsError("Horizon must be positive")

    best = (float("inf"), 0.3, 0.1)
    for alpha in np.linspace(0.05, 0.95, 19):
        for beta in np.linspace(0.02, 0.6, 15):
            fitted, _, _ = _holt(series, float(alpha), float(beta))
            sse = float(((series[1:] - fitted[1:]) ** 2).sum())
            if sse < best[0]:
                best = (sse, float(alpha), float(beta))
    _, alpha, beta = best
    fitted, level, trend = _holt(series, alpha, beta)

    residuals = series[1:] - fitted[1:]
    dof = max(residuals.size - 2, 1)
    residual_std = float(np.sqrt((residuals**2).sum() / dof))

    steps = np.arange(1, horizon + 1)
    point = level + trend * steps
    # Variance of an h-step Holt forecast grows with the accumulated trend error.
    inflation = np.sqrt(1.0 + np.cumsum(np.full(horizon, alpha**2 * (1.0 + beta) ** 2)))
    band = z * residual_std * inflation

    nonzero = np.abs(series[1:]) > 1e-9
    mape = (
        float(np.mean(np.abs(residuals[nonzero] / series[1:][nonzero])) * 100.0)
        if nonzero.any()
        else None
    )
    return ForecastResult(
        horizon=horizon,
        alpha=alpha,
        beta=beta,
        fitted=[float(v) for v in fitted],
        forecast=[float(v) for v in point],
        lower=[float(v) for v in point - band],
        upper=[float(v) for v in point + band],
        residual_std=residual_std,
        mape=mape,
        trend_per_period=float(trend),
    )


def backtest(values: list[float] | np.ndarray, holdout: int = 3) -> dict[str, float | None]:
    """Hold out the tail, refit on the head, and report out-of-sample error."""
    series = np.asarray(values, dtype=float)
    if series.size < MIN_HISTORY + holdout:
        raise AnalyticsError("Series too short for the requested holdout")
    train, test = series[:-holdout], series[-holdout:]
    result = forecast(train, horizon=holdout)
    predicted = np.asarray(result.forecast)
    errors = test - predicted
    nonzero = np.abs(test) > 1e-9
    return {
        "holdout": float(holdout),
        "mae": float(np.abs(errors).mean()),
        "rmse": float(np.sqrt((errors**2).mean())),
        "mape": float(np.mean(np.abs(errors[nonzero] / test[nonzero])) * 100.0)
        if nonzero.any()
        else None,
    }
