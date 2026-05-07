"""
Edge case tests for the health data analytics module.
- Empty records, single records, all-None records
- Date boundaries, window edge cases
- `from __future__ import annotations` interaction with __dict__
- Merge priority and conflict resolution
- Large/small values
"""

from datetime import date, timedelta

import pytest

from vital_sync.analytics import (
    DailyRecord,
    compute_all_baselines,
    compute_baseline,
    detect_deviations,
    detect_deviations_adaptive,
    detect_deviations_hybrid,
    load_cache,
    merge_records,
)

# ── Empty/single/all-None record edge cases ─────────────────────────────────


class TestEmptyAndEdgeCases:
    """Edge case handling for empty/single/None records."""

    def test_compute_baseline_empty(self):
        """Empty records → None."""
        assert compute_baseline([], "sleep_score") is None

    def test_compute_baseline_single(self):
        """Single record → stats with n=1, std=0, trend=None."""
        records = [DailyRecord(date=date(2026, 4, 25), sleep_score=85)]
        stats = compute_baseline(records, "sleep_score", window_days=7)
        assert stats is not None
        assert stats.n == 1
        assert stats.mean == 85
        assert stats.median == 85
        assert stats.std == 0.0
        assert stats.trend is None  # n < 3
        assert stats.min == 85
        assert stats.max == 85

    def test_compute_all_baselines_all_none(self, all_none_records):
        """All-None records → empty dict."""
        baselines = compute_all_baselines(all_none_records, window_days=7)
        assert baselines == {}

    def test_detect_deviations_empty(self):
        """Empty records → empty alerts."""
        assert detect_deviations([]) == []
        assert detect_deviations_adaptive([]) == []
        assert detect_deviations_hybrid([]) == []

    def test_detect_deviations_all_none(self, all_none_records):
        """All-None records — no baselines, so no alerts."""
        alerts = detect_deviations(all_none_records)
        assert alerts == []

    def test_merge_records_empty(self):
        """Merging empty lists produces empty result."""
        assert merge_records() == []
        assert merge_records([]) == []
        assert merge_records([], []) == []

    def test_merge_records_single(self):
        """Single list merges correctly."""
        records = [DailyRecord(date=date(2026, 4, 25), sleep_score=85)]
        merged = merge_records(records)
        assert len(merged) == 1
        assert merged[0].sleep_score == 85

    def test_none_entries_skipped_in_merge(self):
        """None entries in record lists are skipped."""
        records = [
            DailyRecord(date=date(2026, 4, 25), sleep_score=85),
            None,
            None,
            DailyRecord(date=date(2026, 4, 26), sleep_score=82),
        ]
        merged = merge_records(records)
        assert len(merged) == 2


# ── Date boundaries ─────────────────────────────────────────────────────────


class TestDateBoundaries:
    """Test behavior at date boundaries."""

    def test_window_edge_inclusive(self):
        """Records exactly at start_date are included."""
        end = date(2026, 4, 25)
        records = [
            DailyRecord(date=end, sleep_score=85),
            DailyRecord(date=end - timedelta(days=6), sleep_score=82),  # exactly at window edge
            DailyRecord(date=end - timedelta(days=7), sleep_score=90),  # just outside
        ]
        stats = compute_baseline(records, "sleep_score", window_days=7, end_date=end)
        assert stats is not None
        assert stats.n == 2  # first two within window

    def test_no_records_in_window(self):
        """When no records fall in the window, returns None."""
        records = [
            DailyRecord(date=date(2026, 4, 1), sleep_score=85),
            DailyRecord(date=date(2026, 4, 2), sleep_score=82),
        ]
        end = date(2026, 4, 25)
        stats = compute_baseline(records, "sleep_score", window_days=3, end_date=end)
        assert stats is None

    def test_future_dates_excluded(self, today_):
        """Records with dates > end_date are excluded."""
        records = [
            DailyRecord(date=today_, sleep_score=85),
            DailyRecord(date=today_ + timedelta(days=1), sleep_score=82),  # future!
        ]
        stats = compute_baseline(records, "sleep_score", window_days=7, end_date=today_)
        assert stats is not None
        assert stats.n == 1  # only today's record


# ── __dict__ and annotations interaction ────────────────────────────────────


class TestDictAndAnnotations:
    """Test that `from __future__ import annotations` doesn't break __dict__."""

    def test_to_dict_works(self):
        """DailyRecord.to_dict() works despite annotations import."""
        r = DailyRecord(date=date(2026, 4, 25), sleep_score=85, steps=10000)
        d = r.to_dict()
        assert d["date"] == "2026-04-25"
        assert d["sleep_score"] == 85
        assert d["steps"] == 10000
        assert "hevy_muscle_groups" in d  # default_factory field present
        assert "sources" in d
        assert "sleep_tags" in d

    def test_to_dict_includes_none_fields(self):
        """to_dict includes fields that are None (not just set fields)."""
        r = DailyRecord(date=date(2026, 4, 25), sleep_score=85)
        d = r.to_dict()
        assert d["readiness_score"] is None
        assert d["hrv_ms"] is None
        assert d["body_fat_pct"] is None

    def test_getattr_on_record_works(self, sample_records):
        """getattr(r, metric, None) works despite annotations import."""
        r = sample_records[0]
        assert getattr(r, "sleep_score", None) == 88
        assert getattr(r, "nonexistent_field", None) is None

    def test_default_factory_fields_are_lists(self):
        """Fields with default_factory=list start as empty lists, not None."""
        r = DailyRecord(date=date(2026, 4, 25))
        assert r.hevy_muscle_groups == []
        assert r.sources == []
        assert r.sleep_tags == []
        assert r.negated_baseline_tags == []


# ── Merge priority and conflicts ────────────────────────────────────────────


class TestMergeConflicts:
    """Test merge_records priority and conflict resolution."""

    def test_default_priority_order(self):
        """Default priority: manual > renpho > oura > mynetdiary."""
        manual = [
            DailyRecord(date=date(2026, 4, 25), weight_kg=80.0, sources=["manual"]),
        ]
        oura = [
            DailyRecord(date=date(2026, 4, 25), sleep_score=85, sources=["oura"]),
        ]
        renpho = [
            DailyRecord(date=date(2026, 4, 25), weight_kg=81.0, sources=["renpho"]),
        ]
        merged = merge_records(manual, renpho, oura)
        assert len(merged) == 1
        r = merged[0]
        # manual weight should win over renpho weight
        assert r.weight_kg == 80.0
        assert r.sleep_score == 85

    def test_sources_are_merged(self):
        """Multiple sources are accumulated in the sources list."""
        oura = [
            DailyRecord(date=date(2026, 4, 25), sleep_score=85, sources=["oura"]),
        ]
        renpho = [
            DailyRecord(date=date(2026, 4, 25), weight_kg=80.0, sources=["renpho"]),
        ]
        merged = merge_records(oura, renpho)
        r = merged[0]
        assert "oura" in r.sources
        assert "renpho" in r.sources

    def test_different_dates_do_not_conflict(self):
        """Records on different dates are separate in output."""
        oura = [
            DailyRecord(date=date(2026, 4, 25), sleep_score=85),
        ]
        renpho = [
            DailyRecord(date=date(2026, 4, 26), weight_kg=80.0),
        ]
        merged = merge_records(oura, renpho)
        assert len(merged) == 2


# ── Large and small values ──────────────────────────────────────────────────


class TestExtremeValues:
    """Test handling of extreme and edge-case values."""

    def test_very_large_sleep_duration(self):
        """Very large sleep values (e.g., 24h from misrecorded data)."""
        records = [
            DailyRecord(date=date(2026, 4, 25), sleep_duration_hours=24.0),
            DailyRecord(date=date(2026, 4, 26), sleep_duration_hours=7.5),
        ]
        stats = compute_baseline(records, "sleep_duration_hours", window_days=2)
        assert stats is not None
        assert stats.max == 24.0
        assert stats.mean > 7.5

    def test_negative_sleep_duration(self):
        """Negative sleep duration — handled by computation (not validated)."""
        records = [
            DailyRecord(date=date(2026, 4, 25), sleep_duration_hours=-1.0),
            DailyRecord(date=date(2026, 4, 26), sleep_duration_hours=7.5),
        ]
        stats = compute_baseline(records, "sleep_duration_hours", window_days=2)
        assert stats is not None
        assert stats.min == -1.0  # negative value passes through

    def test_zero_division_in_trend(self, today_):
        """All identical values produce a zero numerator/denominator for slope."""
        records = [DailyRecord(date=today_ - timedelta(days=i), sleep_score=85) for i in range(5)]
        stats = compute_baseline(records, "sleep_score", window_days=5, end_date=today_)
        assert stats is not None
        # With all identical values, _linear_slope returns 0 (not None)
        # because den != 0 even though num == 0
        assert stats.trend == 0.0

    def test_null_stress_summary(self):
        """Stress summary can be None or string."""
        r = DailyRecord(date=date(2026, 4, 25), stress_summary=None)
        assert r.stress_summary is None
        r2 = DailyRecord(date=date(2026, 4, 25), stress_summary="normal")
        assert r2.stress_summary == "normal"


# ── Cache interaction ───────────────────────────────────────────────────────


class TestCacheInteraction:
    """Test the real cache.json integrity."""

    def test_cache_loadable(self):
        """The real cache.json can be loaded without errors.

        Skipped unless VITAL_SYNC_INTEGRATION_TESTS is set and cache exists.
        """
        import os

        if not os.environ.get("VITAL_SYNC_INTEGRATION_TESTS"):
            pytest.skip("set VITAL_SYNC_INTEGRATION_TESTS=1 to run")
        cache_path = os.environ.get("VITAL_SYNC_CACHE", "")
        if not cache_path or not os.path.exists(cache_path):
            pytest.skip("VITAL_SYNC_CACHE not set or file not found")
        records = load_cache(cache_path)
        assert isinstance(records, list)
        assert len(records) > 0
        assert all(isinstance(r, DailyRecord) for r in records)

    def test_cache_dates_are_recent(self):
        """Cache records have dates from 2026 (not 2025).

        Skipped unless VITAL_SYNC_INTEGRATION_TESTS is set.
        """
        import os

        if not os.environ.get("VITAL_SYNC_INTEGRATION_TESTS"):
            pytest.skip("set VITAL_SYNC_INTEGRATION_TESTS=1 to run")
        cache_path = os.environ.get("VITAL_SYNC_CACHE", "")
        if not cache_path or not os.path.exists(cache_path):
            pytest.skip("VITAL_SYNC_CACHE not set or file not found")
        records = load_cache(cache_path)
        for r in records:
            assert r.date.year >= 2026, f"Found record from {r.date} — should be filtered"

    def test_cache_records_have_sleep_data(self):
        """Cache records should have sleep metrics when available.

        Skipped unless VITAL_SYNC_INTEGRATION_TESTS is set.
        """
        import os

        if not os.environ.get("VITAL_SYNC_INTEGRATION_TESTS"):
            pytest.skip("set VITAL_SYNC_INTEGRATION_TESTS=1 to run")
        cache_path = os.environ.get("VITAL_SYNC_CACHE", "")
        if not cache_path or not os.path.exists(cache_path):
            pytest.skip("VITAL_SYNC_CACHE not set or file not found")
        records = load_cache(cache_path)
        records_with_sleep = [r for r in records if r.sleep_score is not None]
        assert len(records_with_sleep) > 0
