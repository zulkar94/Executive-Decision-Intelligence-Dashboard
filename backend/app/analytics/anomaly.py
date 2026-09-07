"""Robust anomaly detection for KPI series (median/MAD + level-shift check)."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from app.analytics.mcda import AnalyticsError

# 1/Phi^-1(0.75): scales MAD to a normal-consistent standard deviation.
MAD_SCALE = 1.4826
# Iglewicz & Hoaglin fallback scale when the MAD collapses to zero
# (a majority-constant series, where the sample std is inflated by the outlier).
MEAN_AD_SCALE = 1.253314


def _scale(series: np.ndarray, median: float) -> float:
    deviations = np.abs(series - median)
    mad = float(np.median(deviations)) * MAD_SCALE
    if mad > 1e-12:
        return mad
    mean_ad = float(deviations.mean()) * MEAN_AD_SCALE
    return mean_ad if mean_ad > 1e-12 else 1e-12


@dataclass(slots=True)
class Anomaly:
    index: int
    value: float
    score: float
    kind: str


def detect(
    values: list[float] | np.ndarray, *, threshold: float = 3.5, window: int = 0
) -> list[Anomaly]:
    """Flag points whose robust z-score exceeds ``threshold``.

    ``window`` > 0 switches to a rolling baseline, which tolerates slow drift
    and only reports genuine local excursions.
    """
    series = np.asarray(values, dtype=float)
    if series.ndim != 1:
        raise AnalyticsError("Series must be one-dimensional")
    if series.size < 3:
        return []
    if threshold <= 0:
        raise AnalyticsError("Threshold must be positive")

    if window and window >= 3:
        anomalies: list[Anomaly] = []
        for i in range(window, series.size):
            baseline = series[i - window : i]
            score = _robust_z(series[i], baseline)
            if abs(score) >= threshold:
                anomalies.append(
                    Anomaly(i, float(series[i]), float(score), "spike" if score > 0 else "dip")
                )
        return anomalies

    median = float(np.median(series))
    scores = (series - median) / _scale(series, median)
    return [
        Anomaly(int(i), float(series[i]), float(scores[i]), "spike" if scores[i] > 0 else "dip")
        for i in np.flatnonzero(np.abs(scores) >= threshold)
    ]


def _robust_z(point: float, baseline: np.ndarray) -> float:
    median = float(np.median(baseline))
    return (point - median) / _scale(baseline, median)


def volatility(values: list[float] | np.ndarray, window: int = 7) -> float:
    """Coefficient of variation of the most recent window; 0 when flat."""
    series = np.asarray(values, dtype=float)
    if series.size == 0:
        return 0.0
    tail = series[-window:] if series.size > window else series
    mean = float(np.abs(tail.mean()))
    if mean <= 1e-12:
        return 0.0
    return float(tail.std(ddof=0) / mean)
