"""
Tests for data filtering:
- Sleep < 1 hour (3600s) exclusion
- 2025/old entries filtering
- Date attribution (Oura labels by wake day)
"""

from datetime import date, timedelta

import pytest

from vital_sync.analytics import (
    DailyRecord,
    _build_record,
    compute_baseline,
    oura_mcp_to_records,
)

# ── Short sleep filtering ────────────────────────────────────────────────────


class TestShortSleepFiltering:
    """Test that sleep artifacts (< 1hr / 3600s) are detected or excluded."""

    def test_oura_mcp_short_sleep_filtered(self, oura_mcp_output_short_sleep):
        """Oura MCP output with 840s sleep has sleep-derived fields nulled.

        The 840s segment (wake date Apr 24) has its sleep fields filtered.
        The daily_sleep_score=30 stays with the MCP label date (Apr 25),
        creating a score-only record there.
        """
        records = oura_mcp_to_records(oura_mcp_output_short_sleep)
        # Two records: Apr 24 (filtered sleep) + Apr 25 (score only)
        assert len(records) == 2
        by_date = {r.date: r for r in records}

        r24 = by_date[date(2026, 4, 24)]
        assert r24.sleep_duration_hours is None
        assert r24.sleep_score is None  # Score is on Apr 25
        assert r24.deep_sleep_min is None
        assert r24.rem_sleep_min is None
        assert r24.sleep_efficiency is None
        assert r24.bedtime is None
        assert r24.wake_time is None

        r25 = by_date[date(2026, 4, 25)]
        assert r25.sleep_score == 30  # Score-only record
        assert r25.sleep_duration_hours is None

    def test_short_sleep_filtered_via_build_record(self):
        """_build_record excludes records with < 1hr sleep.

        Sleep artifacts are filtered at the source so they don't pollute
        baseline computation.
        """
        data = {
            "sleep_total_sleep_duration": 840,  # 14 minutes
            "sleep_score": 30,
            "sleep_deep_sleep_duration": 120,
            "sleep_rem_sleep_duration": 180,
            "sleep_efficiency": 50,
        }
        r = _build_record(date(2026, 4, 25), data)
        assert r.sleep_duration_hours is None
        assert r.sleep_score is None
        assert r.deep_sleep_min is None
        assert r.rem_sleep_min is None
        assert r.sleep_efficiency is None
        # compute_baseline sees no data for the metric
        stats = compute_baseline([r], "sleep_duration_hours", window_days=1)
        assert stats is None


class TestOldEntryFiltering:
    """Test that entries from previous years (e.g., 2025) can be filtered."""

    def test_mixed_year_records_can_be_filtered(self, today_):
        """Old records can be excluded with min_date parameter.

        Previously there was no way to filter out pre-current-year records.
        Now compute_baseline accepts a min_date to exclude stale data.
        """
        records = [
            DailyRecord(date=date(2025, 6, 15), sleep_score=85),
            DailyRecord(date=date(2025, 6, 16), sleep_score=86),
            DailyRecord(date=today_, sleep_score=80),
            DailyRecord(date=today_ - timedelta(days=1), sleep_score=81),
            DailyRecord(date=today_ - timedelta(days=2), sleep_score=82),
        ]
        # Without min_date, old records within window are included
        stats_no_filter = compute_baseline(records, "sleep_score", window_days=7)
        assert stats_no_filter is not None
        # With min_date, old records are excluded
        min_date = date(today_.year, 1, 1)
        stats_filtered = compute_baseline(records, "sleep_score", window_days=7, min_date=min_date)
        assert stats_filtered is not None
        assert stats_filtered.n == 3  # Only 2026 records
        assert stats_filtered.mean == pytest.approx(81.0, abs=0.01)

    def test_recent_window_excludes_old_dates_correctly(self, today_):
        """With a narrow window, old dates are naturally excluded by date range.

        This is the PASSING scenario: window_days=3 naturally excludes 2025.
        """
        records = [
            DailyRecord(date=date(2025, 6, 15), sleep_score=85),
            DailyRecord(date=today_, sleep_score=80),
            DailyRecord(date=today_ - timedelta(days=1), sleep_score=81),
            DailyRecord(date=today_ - timedelta(days=2), sleep_score=82),
        ]
        stats = compute_baseline(records, "sleep_score", window_days=3, end_date=today_)
        assert stats is not None
        assert stats.n == 3  # Only the recent 3, 2025 excluded by date range


class TestDateAttribution:
    """Test Oura's wake-day labeling and how records handle dates."""

    def test_build_record_uses_provided_date(self):
        """_build_record uses the date passed in, not derived from bedtime."""
        d = date(2026, 4, 25)
        data = {
            "sleep_bedtime_start": "2026-04-24T23:30:00Z",
            "sleep_bedtime_end": "2026-04-25T07:00:00Z",
            "sleep_total_sleep_duration": 27000,
        }
        r = _build_record(d, data)
        assert r.date == d  # day 25, which is wake day

    def test_oura_mcp_conversion_date_ordering(self, oura_mcp_output_multi_day):
        """Multiple days are parsed correctly with dates in order."""
        records = oura_mcp_to_records(oura_mcp_output_multi_day)
        assert len(records) == 3
        dates = [r.date.isoformat() for r in records]
        assert dates == ["2026-04-23", "2026-04-24", "2026-04-25"]
