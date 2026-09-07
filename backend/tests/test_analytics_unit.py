"""Unit tests for the numeric core: MCDA, AHP, sensitivity, risk, forecasting."""

from __future__ import annotations

import numpy as np
import pytest

from app.analytics import forecast as forecasting
from app.analytics.anomaly import detect, volatility
from app.analytics.mcda import (
    AnalyticsError,
    ahp_weights,
    evaluate,
    min_max_normalise,
    rank,
    sensitivity,
    topsis,
    weighted_sum,
)
from app.analytics.risk import simulate

MATRIX = np.array([[10.0, 8.0, 2.0], [14.0, 9.0, 5.0], [12.0, 4.0, 1.0]])
WEIGHTS = np.array([0.5, 0.3, 0.2])
BENEFIT = np.array([False, True, False])


def test_min_max_normalise_respects_direction() -> None:
    scaled = min_max_normalise(MATRIX, BENEFIT)
    # Column 0 is a cost: the cheapest option must score 1.0.
    assert scaled[0, 0] == pytest.approx(1.0)
    assert scaled[1, 0] == pytest.approx(0.0)
    assert scaled[:, 1].max() == pytest.approx(1.0)


def test_constant_column_is_neutral_not_a_divide_by_zero() -> None:
    flat = np.array([[5.0, 1.0], [5.0, 2.0]])
    scaled = min_max_normalise(flat, np.array([True, True]))
    assert np.allclose(scaled[:, 0], 0.5)


def test_weighted_sum_contributions_sum_to_score() -> None:
    scores, contributions = weighted_sum(MATRIX, WEIGHTS, BENEFIT)
    assert np.allclose(scores, contributions.sum(axis=1))
    assert scores.max() <= 1.0 + 1e-9


def test_topsis_closeness_is_bounded() -> None:
    closeness, _ = topsis(MATRIX, WEIGHTS, BENEFIT)
    assert ((closeness >= 0) & (closeness <= 1)).all()


def test_both_methods_agree_on_a_dominant_option() -> None:
    dominant = np.array([[1.0, 10.0], [5.0, 5.0], [9.0, 1.0]])
    weights = np.array([0.5, 0.5])
    benefit = np.array([False, True])  # option 0 is best on both criteria
    for method in ("weighted_sum", "topsis"):
        assert evaluate(dominant, weights, benefit, method).ranked[0].index == 0


def test_rank_gives_ties_the_same_position() -> None:
    ranks = rank(np.array([0.9, 0.9, 0.4]))
    assert ranks[0] == ranks[1] == 1
    assert ranks[2] == 3


def test_zero_weight_vector_is_rejected() -> None:
    with pytest.raises(AnalyticsError):
        evaluate(MATRIX, np.zeros(3), BENEFIT)


def test_negative_weight_is_rejected() -> None:
    with pytest.raises(AnalyticsError):
        evaluate(MATRIX, np.array([0.5, -0.2, 0.7]), BENEFIT)


def test_shape_mismatch_is_rejected() -> None:
    with pytest.raises(AnalyticsError):
        evaluate(MATRIX, np.array([0.5, 0.5]), BENEFIT)


def test_unknown_method_is_rejected() -> None:
    with pytest.raises(AnalyticsError):
        evaluate(MATRIX, WEIGHTS, BENEFIT, "borda")


def test_ahp_recovers_weights_from_a_consistent_matrix() -> None:
    true_weights = np.array([0.5, 0.3, 0.2])
    pairwise = true_weights[:, None] / true_weights[None, :]
    weights, lambda_max, ratio = ahp_weights(pairwise)
    assert np.allclose(weights, true_weights, atol=1e-6)
    assert lambda_max == pytest.approx(3.0, abs=1e-6)
    assert ratio == pytest.approx(0.0, abs=1e-6)


def test_ahp_flags_inconsistent_judgements() -> None:
    # A > B, B > C, but C > A: a cycle the eigenvector cannot reconcile.
    pairwise = np.array([[1.0, 5.0, 0.2], [0.2, 1.0, 5.0], [5.0, 0.2, 1.0]])
    _, _, ratio = ahp_weights(pairwise)
    assert ratio > 0.10


def test_ahp_rejects_non_square_and_non_positive_input() -> None:
    with pytest.raises(AnalyticsError):
        ahp_weights(np.ones((2, 3)))
    with pytest.raises(AnalyticsError):
        ahp_weights(np.array([[1.0, 0.0], [0.0, 1.0]]))


def test_sensitivity_is_sorted_by_swing_and_covers_every_criterion() -> None:
    rows = sensitivity(MATRIX, WEIGHTS, BENEFIT, steps=21)
    assert len(rows) == 3
    swings = [row.swing for row in rows]
    assert swings == sorted(swings, reverse=True)
    assert {row.criterion_index for row in rows} == {0, 1, 2}


def test_sensitivity_reports_stability_when_one_option_dominates() -> None:
    dominant = np.array([[1.0, 10.0], [9.0, 1.0]])
    rows = sensitivity(dominant, np.array([0.5, 0.5]), np.array([False, True]), steps=21)
    # Option 0 wins on both criteria, so no weight sweep can unseat it.
    assert all(row.robust for row in rows)


def test_simulation_is_reproducible_under_a_seed() -> None:
    uncertainty = np.full_like(MATRIX, 1.0)
    first = simulate(MATRIX, uncertainty, WEIGHTS, BENEFIT, runs=800, seed=11)
    second = simulate(MATRIX, uncertainty, WEIGHTS, BENEFIT, runs=800, seed=11)
    assert [item.win_probability for item in first.options] == [
        item.win_probability for item in second.options
    ]


def test_simulation_probabilities_form_a_distribution() -> None:
    result = simulate(MATRIX, np.full_like(MATRIX, 1.0), WEIGHTS, BENEFIT, runs=1000, seed=3)
    assert sum(item.win_probability for item in result.options) == pytest.approx(1.0, abs=1e-9)
    for item in result.options:
        assert item.p05 <= item.p50 <= item.p95
        assert sum(item.rank_distribution.values()) == pytest.approx(1.0, abs=1e-9)


def test_zero_uncertainty_makes_the_deterministic_winner_certain() -> None:
    result = simulate(MATRIX, np.zeros_like(MATRIX), WEIGHTS, BENEFIT, runs=300, seed=5)
    assert result.decision_confidence == pytest.approx(1.0)


def test_more_uncertainty_lowers_confidence() -> None:
    calm = simulate(MATRIX, np.full_like(MATRIX, 0.2), WEIGHTS, BENEFIT, runs=1500, seed=9)
    wild = simulate(MATRIX, np.full_like(MATRIX, 6.0), WEIGHTS, BENEFIT, runs=1500, seed=9)
    assert wild.decision_confidence < calm.decision_confidence


def test_simulation_rejects_bad_shapes_and_negative_spread() -> None:
    with pytest.raises(AnalyticsError):
        simulate(MATRIX, np.zeros((2, 2)), WEIGHTS, BENEFIT, runs=100)
    with pytest.raises(AnalyticsError):
        simulate(MATRIX, np.full_like(MATRIX, -1.0), WEIGHTS, BENEFIT, runs=100)


def test_forecast_follows_a_linear_trend() -> None:
    series = [float(10 + 2 * i) for i in range(20)]
    result = forecasting.forecast(series, horizon=3)
    assert result.forecast[0] == pytest.approx(50.0, abs=0.5)
    assert result.trend_per_period == pytest.approx(2.0, abs=0.2)
    assert result.mape is not None and result.mape < 1.0


def test_forecast_band_widens_with_horizon() -> None:
    series = [10.0, 11.0, 10.5, 12.0, 11.5, 13.0, 12.5, 14.0]
    result = forecasting.forecast(series, horizon=5)
    widths = [high - low for high, low in zip(result.upper, result.lower, strict=True)]
    assert widths == sorted(widths)
    assert all(
        low <= point <= high
        for low, point, high in zip(result.lower, result.forecast, result.upper, strict=True)
    )


def test_forecast_rejects_short_or_dirty_series() -> None:
    with pytest.raises(AnalyticsError):
        forecasting.forecast([1.0, 2.0], horizon=2)
    with pytest.raises(AnalyticsError):
        forecasting.forecast([1.0, 2.0, float("nan"), 4.0], horizon=2)


def test_backtest_reports_out_of_sample_error() -> None:
    series = [float(5 + 3 * i) for i in range(15)]
    metrics = forecasting.backtest(series, holdout=3)
    assert metrics["mae"] < 1.0
    assert metrics["rmse"] >= metrics["mae"] - 1e-9


def test_anomaly_detection_finds_a_spike_in_a_flat_series() -> None:
    found = detect([1.0] * 10 + [12.0] + [1.0] * 10)
    assert len(found) == 1
    assert found[0].index == 10
    assert found[0].kind == "spike"


def test_anomaly_detection_ignores_clean_series() -> None:
    assert detect([5.0, 5.1, 4.9, 5.0, 5.2, 4.8]) == []


def test_rolling_window_tolerates_drift_but_catches_a_jump() -> None:
    drifting = [float(i) for i in range(30)]
    assert detect(drifting, window=6) == []
    drifting[20] = 500.0
    assert any(item.index == 20 for item in detect(drifting, window=6))


def test_volatility_is_zero_for_a_flat_series() -> None:
    assert volatility([4.0] * 10) == pytest.approx(0.0)
    assert volatility([]) == 0.0
