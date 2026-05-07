"""
Tests for baseline computation:
- _mean, _median, _std, _linear_slope
- compute_baseline with known data
- compute_all_baselines
- Edge cases: empty, single record, all None
"""

from datetime import date, timedelta

import pytest

from vital_sync.analytics import (
    DailyRecord,
    _linear_slope,
    _mean,
    _median,
    _std,
    compute_all_baselines,
    compute_baseline,
)

# ── Core math utilities ─────────────────────────────────────────────────────


class TestCoreMath:
    """Unit tests for _mean, _median, _std, _linear_slope."""

    def test_mean_basic(self):
        assert _mean([1, 2, 3, 4, 5]) == 3.0

    def test_mean_single_value(self):
        assert _mean([42]) == 42.0

    def test_mean_empty(self):
        with pytest.raises(ZeroDivisionError):
            _mean([])

    def test_median_odd(self):
        assert _median([1, 2, 3, 4, 5]) == 3.0
        assert _median([5, 1, 3, 2, 4]) == 3.0

    def test_median_even(self):
        assert _median([1, 2, 3, 4]) == 2.5

    def test_std_constant(self):
        assert _std([5, 5, 5, 5]) == 0.0

    def test_std_variable(self):
        # Population std of [1, 2, 3] = sqrt(2/3) ≈ 0.816
        assert _std([1, 2, 3]) == pytest.approx(0.816, abs=0.01)

    def test_linear_slope_basic(self):
        # y = 2x + 1: [1, 3, 5, 7, 9]
        slope = _linear_slope([1, 3, 5, 7, 9])
        assert slope == pytest.approx(2.0, abs=0.01)

    def test_linear_slope_flat(self):
        slope = _linear_slope([5, 5, 5, 5, 5])
        assert slope == 0.0

    def test_linear_slope_insufficient_data(self):
        assert _linear_slope([1, 2]) is None
        assert _linear_slope([1]) is None


# ── compute_baseline ────────────────────────────────────────────────────────


class TestComputeBaseline:
    """Test compute_baseline with known data."""

    def test_basic_stats(self, sample_records, today_):
        """compute_baseline returns correct mean, median, std, min, max."""
        stats = compute_baseline(sample_records, "sleep_score", window_days=10, end_date=today_)
        assert stats is not None
        assert stats.metric == "sleep_score"
        assert stats.n == 10
        # sleep_scores: [88, 87, 86, 85, 84, 82, 80, 79, 65, 62]
        assert stats.mean == pytest.approx(79.8, abs=0.1)
        assert stats.median == pytest.approx(83.0, abs=0.1)
        assert stats.min == 62
        assert stats.max == 88
        assert stats.latest == 62

    def test_trend_is_computed(self, sample_records, today_):
        """Trend slope is non-None with 7+ data points."""
        stats = compute_baseline(sample_records, "sleep_score", window_days=10, end_date=today_)
        assert stats is not None
        assert stats.trend is not None
        # Should be negative (declining sleep scores)
        assert stats.trend < 0

    def test_days_since_change(self, sample_records, today_):
        """days_since_change is computed."""
        stats = compute_baseline(sample_records, "sleep_score", window_days=10, end_date=today_)
        assert stats is not None
        assert stats.days_since_change is not None
        assert isinstance(stats.days_since_change, int)

    def test_returns_none_for_no_data(self):
        """Returns None when no values available for metric."""
        records = [
            DailyRecord(date=date(2026, 4, 20), sleep_score=None),
            DailyRecord(date=date(2026, 4, 21), sleep_score=None),
        ]
        stats = compute_baseline(records, "sleep_score", window_days=2)
        assert stats is None

    def test_empty_records(self):
        """Empty record list returns None."""
        stats = compute_baseline([], "sleep_score", window_days=7)
        assert stats is None

    def test_single_record(self):
        """Single record baseline."""
        records = [DailyRecord(date=date(2026, 4, 25), sleep_score=85)]
        stats = compute_baseline(records, "sleep_score", window_days=7)
        assert stats is not None
        assert stats.n == 1
        assert stats.mean == 85
        assert stats.median == 85
        assert stats.std == 0.0  # single value → variance 0
        assert stats.trend is None  # < 3 values → no trend

    def test_none_records_skipped(self, today_):
        """None entries in the record list are skipped (line 265-266)."""
        records = [
            DailyRecord(date=today_, sleep_score=85),
            None,  # should be skipped
            DailyRecord(date=today_ - timedelta(days=1), sleep_score=82),
            None,
            DailyRecord(date=today_ - timedelta(days=2), sleep_score=88),
        ]
        stats = compute_baseline(records, "sleep_score", window_days=3, end_date=today_)
        assert stats is not None
        assert stats.n == 3  # None entries ignored

    def test_window_respects_dates(self, today_):
        """Only records within the window are included."""
        records = [
            DailyRecord(date=today_, sleep_score=85),
            DailyRecord(date=today_ - timedelta(days=1), sleep_score=82),
            DailyRecord(date=today_ - timedelta(days=5), sleep_score=90),
            DailyRecord(date=today_ - timedelta(days=10), sleep_score=95),
        ]
        stats = compute_baseline(records, "sleep_score", window_days=3, end_date=today_)
        assert stats is not None
        assert stats.n == 2  # only first two within 3-day window

    def test_mixed_types_have_no_deviation(self, today_):
        """Records with mixed types (int, float) for sleep_score."""
        records = [
            DailyRecord(date=today_, sleep_score=85.0),
            DailyRecord(date=today_ - timedelta(days=1), sleep_score=82),
            DailyRecord(date=today_ - timedelta(days=2), sleep_score=88.0),
        ]
        stats = compute_baseline(records, "sleep_score", window_days=3, end_date=today_)
        assert stats is not None
        assert stats.n == 3
        assert isinstance(stats.mean, float)


# ── compute_all_baselines ───────────────────────────────────────────────────


class TestComputeAllBaselines:
    """Test compute_all_baselines."""

    def test_default_metrics(self, sample_records, today_):
        """Returns baselines for all default metrics that have data."""
        baselines = compute_all_baselines(sample_records, window_days=10, end_date=today_)
        assert isinstance(baselines, dict)
        # sleep metrics should all be present
        assert "sleep_score" in baselines
        assert "readiness_score" in baselines
        assert "sleep_duration_hours" in baselines
        assert "hrv_ms" in baselines
        assert "steps" in baselines

    def test_custom_metrics(self, sample_records, today_):
        """Only requested metrics are computed."""
        baselines = compute_all_baselines(
            sample_records, metrics=["sleep_score", "hrv_ms"], window_days=10, end_date=today_
        )
        assert set(baselines.keys()) == {"sleep_score", "hrv_ms"}

    def test_no_data_metrics_excluded(self):
        """Metrics with no data are excluded from result."""
        records = [DailyRecord(date=date(2026, 4, 25), sleep_score=85)]
        baselines = compute_all_baselines(records, window_days=7)
        assert "sleep_score" in baselines
        assert "body_fat_pct" not in baselines  # no data

    def test_all_none_records(self, all_none_records):
        """All-None records produce empty baseline dict."""
        baselines = compute_all_baselines(all_none_records, window_days=7)
        assert baselines == {}
