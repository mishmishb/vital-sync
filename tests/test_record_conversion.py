"""
Tests for Oura MCP output → DailyRecord conversion.
Validates:
- seconds → hours conversion
- bedtime parsing
- missing fields handling
- edge cases in _build_record
"""

import tempfile
from datetime import date
from pathlib import Path

import pytest

from vital_sync.analytics import (
    DailyRecord,
    _build_record,
    load_cache,
    oura_mcp_to_records,
    records_from_json,
    records_to_json,
    save_cache,
)

# ── oura_mcp_to_records ─────────────────────────────────────────────────────


class TestOuraMcpToRecords:
    """Test the full Oura MCP text parsing pipeline."""

    def test_single_day_conversion(self, oura_mcp_output):
        r"""A full Oura MCP result converts to a DailyRecord correctly.

        BUG: The metric regex at line 195 only captures [\d.]+ values.
        String values like 'normal' (stress summary) and ISO datetime
        strings (bedtime_start/end) are silently dropped.
        """
        records = oura_mcp_to_records(oura_mcp_output)
        assert len(records) == 1
        r = records[0]
        assert isinstance(r, DailyRecord)
        assert r.date == date(2026, 4, 25)
        # Numeric metrics parse correctly
        assert r.sleep_score == 85
        assert r.readiness_score == 82
        assert r.activity_score == 95
        assert r.spo2 == 97.5
        assert r.steps == 11000
        assert r.sleep_duration_hours == pytest.approx(7.5, abs=0.01)  # 27000/3600
        assert r.deep_sleep_min == pytest.approx(90.0, abs=0.01)  # 5400/60
        assert r.rem_sleep_min == pytest.approx(120.0, abs=0.01)  # 7200/60
        assert r.sleep_efficiency == 92
        assert r.avg_hr == 55
        assert r.lowest_hr == 48
        assert r.hrv_ms == 42
        assert r.sources == ["oura"]
        # String and datetime values now parse correctly
        assert r.stress_summary == "normal"
        assert r.bedtime == "23:30:00"
        assert r.wake_time == "07:00:00"

    def test_bedtime_parsing(self, oura_mcp_output):
        r"""Bedtime parsing works correctly after regex fix.

        Previously the metric regex only captured [\d.]+ values, so ISO datetime
        strings like '2026-04-24T23:30:00Z' were dropped. Now the general regex
        captures them and _build_record parses the datetime.
        """
        records = oura_mcp_to_records(oura_mcp_output)
        r = records[0]
        assert r.bedtime == "23:30:00"
        assert r.wake_time == "07:00:00"

    def test_empty_result(self):
        """Empty or missing result returns empty list."""
        records = oura_mcp_to_records({})
        assert records == []
        records = oura_mcp_to_records({"result": ""})
        assert records == []

    def test_multi_day_conversion(self, oura_mcp_output_multi_day):
        """Multiple days parsed correctly."""
        records = oura_mcp_to_records(oura_mcp_output_multi_day)
        assert len(records) == 3
        dates = [r.date for r in records]
        assert dates == [date(2026, 4, 23), date(2026, 4, 24), date(2026, 4, 25)]

    def test_missing_optional_fields(self):
        """Fields not in the MCP output are left as None."""
        result = {"result": ("2026-04-25:\ndaily_sleep_score: 80 score\n")}
        records = oura_mcp_to_records(result)
        r = records[0]
        assert r.sleep_score == 80
        assert r.readiness_score is None
        assert r.sleep_duration_hours is None
        assert r.avg_hr is None
        assert r.hrv_ms is None

    def test_invalid_bedtime_handling(self):
        """Malformed bedtime strings don't crash the conversion."""
        result = {
            "result": (
                "2026-04-25:\n"
                "daily_sleep_score: 80 score\n"
                "sleep_bedtime_start: not-a-datetime\n"
                "sleep_bedtime_end: also-invalid\n"
            )
        }
        # Should not raise — invalid values silently skipped
        records = oura_mcp_to_records(result)
        assert len(records) == 1
        r = records[0]
        assert r.bedtime is None  # Malformed → skipped
        assert r.wake_time is None

    def test_multiple_sleep_durations_selects_longest(self, oura_mcp_output_multiple_durations):
        """When Oura returns multiple sleep durations, segments are separated.

        The 27000s segment (bedtime_end Apr 24) is assigned to Apr 24 with full
        metrics. The 840s artifact (bedtime_end Apr 25) is assigned to Apr 25
        and filtered as <1h. Both records get daily_sleep_score from their
        respective MCP labels.
        """
        records = oura_mcp_to_records(oura_mcp_output_multiple_durations)
        by_date = {r.date: r for r in records}

        assert date(2026, 4, 24) in by_date
        r24 = by_date[date(2026, 4, 24)]
        assert r24.sleep_duration_hours == pytest.approx(7.5, abs=0.01)
        assert r24.deep_sleep_min == pytest.approx(90.0, abs=0.01)
        assert r24.rem_sleep_min == pytest.approx(120.0, abs=0.01)
        assert r24.sleep_efficiency == 92
        assert r24.sleep_score == 84
        assert r24.bedtime == "23:30:00"
        assert r24.wake_time == "07:00:00"
        assert r24.avg_hr == 55
        assert r24.hrv_ms == 42

        # April 25 gets the artifact (filtered) — score-only from daily_sleep_score
        assert date(2026, 4, 25) in by_date
        r25 = by_date[date(2026, 4, 25)]
        assert r25.sleep_duration_hours is None  # Filtered <1h
        assert r25.bedtime is None
        assert r25.wake_time is None

    def test_short_sleep_only_is_filtered(self, oura_mcp_output_short_sleep):
        """When ONLY an artifact (<1h) exists, sleep-derived fields are nulled.

        The 840s segment (bedtime_end Apr 24 23:44) is reassigned to wake date Apr 24.
        The daily_sleep_score=30 stays with the MCP label date (Apr 25) — creating a
        score-only record. This reflects Oura's behavior: scores are per wake-date,
        not per segment.
        """
        records = oura_mcp_to_records(oura_mcp_output_short_sleep)
        # Two records: Apr 24 (sleep data, filtered) + Apr 25 (score only)
        assert len(records) == 2
        by_date = {r.date: r for r in records}

        # Apr 24: sleep segment, artifact filtered
        r24 = by_date[date(2026, 4, 24)]
        assert r24.sleep_duration_hours is None
        assert r24.sleep_score is None  # Score belongs to Apr 25
        assert r24.bedtime is None
        assert r24.wake_time is None

        # Apr 25: score-only record (from daily_sleep_score)
        r25 = by_date[date(2026, 4, 25)]
        assert r25.sleep_score == 30
        assert r25.sleep_duration_hours is None

    def test_segments_reassigned_to_wake_date(self, oura_mcp_output_multi_segment_cross_midnight):
        """Sleep segments are reassigned to wake date, not MCP label date.

        BUG: Oura MCP labels sleep by onset date. A segment starting at
        23:43 on Apr 22 that wakes at 08:32 on Apr 23 gets labeled under Apr 22.
        The Oura APP (and correct convention) assigns it to Apr 23 (wake date).

        Daily scores stay with their MCP label's wake date: score=80 goes to
        Apr 22, score=77 goes to Apr 23. Apr 24 has no MCP label in this fixture
        so it gets no score — just the sleep segment data.
        """
        records = oura_mcp_to_records(oura_mcp_output_multi_segment_cross_midnight)

        # Should produce 3 records (Apr 22, 23, 24)
        by_date = {r.date: r for r in records}

        # Apr 22: the 23070s segment (wakes at 10:05 Apr 22) + score from MCP label
        assert date(2026, 4, 22) in by_date, f"Missing Apr 22 record. Got: {list(by_date.keys())}"
        r22 = by_date[date(2026, 4, 22)]
        assert r22.sleep_duration_hours == pytest.approx(6.41, abs=0.01)  # 23070/3600
        assert r22.sleep_score == 80  # From MCP label April 22
        assert r22.bedtime == "02:52:27"
        assert r22.wake_time == "10:05:22"

        # Apr 23: the 26340s segment (wakes at 08:32 Apr 23) + score from MCP label
        assert date(2026, 4, 23) in by_date
        r23 = by_date[date(2026, 4, 23)]
        assert r23.sleep_duration_hours == pytest.approx(7.32, abs=0.01)  # 26340/3600
        assert r23.sleep_score == 77  # From MCP label April 23
        assert r23.bedtime == "23:43:28"
        assert r23.wake_time == "08:32:34"

        # Apr 24: the 27300s segment (wakes at 09:00 Apr 24) — no MCP label in fixture
        assert date(2026, 4, 24) in by_date
        r24 = by_date[date(2026, 4, 24)]
        assert r24.sleep_duration_hours == pytest.approx(7.58, abs=0.01)  # 27300/3600
        assert r24.bedtime == "00:26:31"
        assert r24.wake_time == "09:00:33"


# ── _build_record ───────────────────────────────────────────────────────────


class TestBuildRecord:
    """Unit tests for _build_record."""

    def test_seconds_to_hours_conversion(self):
        """sleep_total_sleep_duration is divided by 3600."""
        d = date(2026, 4, 25)
        r = _build_record(
            d,
            {
                "sleep_total_sleep_duration": 25200,  # 7h
                "sleep_deep_sleep_duration": 5400,  # 90 min
                "sleep_rem_sleep_duration": 7200,  # 120 min
            },
        )
        assert r.sleep_duration_hours == pytest.approx(7.0, abs=0.01)
        assert r.deep_sleep_min == pytest.approx(90.0, abs=0.01)
        assert r.rem_sleep_min == pytest.approx(120.0, abs=0.01)

    def test_steps_to_int(self):
        """daily_activity_steps is cast to int."""
        r = _build_record(date(2026, 4, 25), {"daily_activity_steps": 11000.0})
        assert r.steps == 11000
        assert isinstance(r.steps, int)

    def test_missing_steps_field(self):
        """When daily_activity_steps is missing, steps is None."""
        r = _build_record(date(2026, 4, 25), {})
        assert r.steps is None

    def test_zero_sleep_duration(self):
        """Zero seconds sleep is treated as an artifact and filtered out."""
        r = _build_record(date(2026, 4, 25), {"sleep_total_sleep_duration": 0})
        assert r.sleep_duration_hours is None  # Filtered as artifact (< 1h)


# ── JSON serialization round-trip ────────────────────────────────────────────


class TestJsonRoundTrip:
    """Test records_to_json → records_from_json round-trip."""

    def test_round_trip_preserves_data(self, sample_records):
        """JSON round-trip preserves all fields."""
        json_str = records_to_json(sample_records)
        restored = records_from_json(json_str)
        assert len(restored) == len(sample_records)
        for orig, rest in zip(sample_records, restored, strict=False):
            assert rest.date == orig.date
            assert rest.sleep_score == orig.sleep_score
            assert rest.sleep_duration_hours == orig.sleep_duration_hours
            assert rest.hrv_ms == orig.hrv_ms
            assert rest.steps == orig.steps

    def test_round_trip_preserves_list_fields(self, today_):
        """List fields (sources, sleep_tags, hevy_muscle_groups) survive round-trip."""
        r = DailyRecord(
            date=today_,
            sleep_score=85,
            sources=["oura", "manual"],
            sleep_tags=["magnesium", "podcast"],
            hevy_muscle_groups=["chest", "back"],
        )
        json_str = records_to_json([r])
        restored = records_from_json(json_str)
        assert restored[0].sources == ["oura", "manual"]
        assert restored[0].sleep_tags == ["magnesium", "podcast"]
        assert restored[0].hevy_muscle_groups == ["chest", "back"]

    def test_load_cache_missing_file(self):
        """load_cache returns empty list for nonexistent file."""
        records = load_cache("/tmp/nonexistent_cache_12345.json")
        assert records == []

    def test_save_and_load_cache(self):
        """Full save/load cycle."""
        records = [
            DailyRecord(date=date(2026, 4, 25), sleep_score=85),
            DailyRecord(date=date(2026, 4, 26), sleep_score=88),
        ]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            path = f.name
        try:
            save_cache(records, path)
            loaded = load_cache(path)
            assert len(loaded) == 2
            assert loaded[0].date == date(2026, 4, 25)
            assert loaded[0].sleep_score == 85
        finally:
            Path(path).unlink(missing_ok=True)
