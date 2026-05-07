"""Tests for Oura API v2 client — date windows, parsing, edge cases, error handling."""

from datetime import date

import pytest

from vital_sync import oura_client

# ── Helpers ───────────────────────────────────────────────────────────────────


class FixedDate(date):
    @classmethod
    def today(cls):
        return cls(2026, 4, 28)


def _sleep_period(day, duration=28800, bedtime_start=None, bedtime_end=None, **kwargs):
    """Create a minimal valid Oura sleep period dict."""
    p = {
        "day": day,
        "total_sleep_duration": duration,
        "deep_sleep_duration": 5400,
        "rem_sleep_duration": 7200,
        "efficiency": 90,
        "bedtime_start": bedtime_start or "2026-04-27T23:00:00+02:00",
        "bedtime_end": bedtime_end or "2026-04-28T07:00:00+02:00",
        "average_heart_rate": 60,
        "lowest_heart_rate": 54,
        "average_hrv": 35,
        "latency": 600,
        "time_in_bed": 30000,
    }
    p.update(kwargs)
    return p


# ── _extract_time ─────────────────────────────────────────────────────────────


class TestExtractTime:
    def test_valid_iso_with_timezone(self):
        assert oura_client._extract_time("2026-04-27T23:30:00+02:00") == "23:30:00"

    def test_valid_iso_with_Z(self):
        assert oura_client._extract_time("2026-04-27T23:30:00Z") == "23:30:00"

    def test_none_returns_none(self):
        assert oura_client._extract_time(None) is None

    def test_empty_string_returns_none(self):
        assert oura_client._extract_time("") is None

    def test_invalid_format_returns_none(self):
        assert oura_client._extract_time("not-a-datetime") is None

    def test_time_with_microseconds(self):
        result = oura_client._extract_time("2026-04-27T23:30:00.123456+02:00")
        assert result is not None


# ── sleep_periods_to_records ──────────────────────────────────────────────────


class TestSleepPeriodsToRecords:
    def test_empty_periods(self):
        records = oura_client.sleep_periods_to_records([], {})
        assert records == {}

    def test_single_period(self):
        periods = [_sleep_period("2026-04-28")]
        daily_sleep = {"2026-04-28": {"score": 85}}
        records = oura_client.sleep_periods_to_records(periods, daily_sleep)
        assert "2026-04-28" in records
        r = records["2026-04-28"]
        assert r["sleep_score"] == 85
        assert r["sleep_duration_hours"] == pytest.approx(8.0)

    def test_filters_artifact_periods(self):
        """Periods under 3600s should be filtered, creating score-only records."""
        periods = [
            _sleep_period("2026-04-28", duration=840),  # 14min artifact
        ]
        daily_sleep = {"2026-04-28": {"score": 30}}
        records = oura_client.sleep_periods_to_records(periods, daily_sleep)
        assert "2026-04-28" in records
        r = records["2026-04-28"]
        assert r["sleep_score"] == 30
        assert r.get("sleep_duration_hours") is None  # filtered

    def test_multiple_periods_same_day(self):
        """Main sleep + nap on the same day — added together."""
        periods = [
            _sleep_period("2026-04-28", duration=25200),  # 7h main
            _sleep_period("2026-04-28", duration=3600),  # 1h nap
        ]
        records = oura_client.sleep_periods_to_records(periods, {})
        r = records["2026-04-28"]
        assert r["sleep_duration_hours"] == pytest.approx(8.0)  # 7+1

    def test_readiness_and_activity_scores(self):
        periods = [_sleep_period("2026-04-28")]
        daily_sleep = {"2026-04-28": {"score": 85}}
        daily_readiness = {"2026-04-28": {"score": 80}}
        daily_activity = {"2026-04-28": {"score": 90, "steps": 10000}}
        records = oura_client.sleep_periods_to_records(
            periods, daily_sleep, daily_readiness, daily_activity
        )
        r = records["2026-04-28"]
        assert r["sleep_score"] == 85
        assert r["readiness_score"] == 80
        assert r["activity_score"] == 90
        assert r["steps"] == 10000

    def test_day_not_in_daily_sleep(self):
        """Period exists but daily_sleep doesn't have that day's score."""
        periods = [_sleep_period("2026-04-28")]
        records = oura_client.sleep_periods_to_records(periods, {})
        r = records["2026-04-28"]
        assert r.get("sleep_score") is None
        assert r["sleep_duration_hours"] is not None  # still has duration

    def test_periods_sorted_by_day(self):
        periods = [
            _sleep_period("2026-04-28"),
            _sleep_period("2026-04-26"),
        ]
        records = oura_client.sleep_periods_to_records(periods, {})
        assert list(records.keys()) == ["2026-04-26", "2026-04-28"]

    def test_null_readiness_and_activity(self):
        periods = [_sleep_period("2026-04-28")]
        records = oura_client.sleep_periods_to_records(periods, {}, None, None)
        assert "readiness_score" not in records["2026-04-28"]

    def test_period_missing_day_field(self):
        """Period without 'day' field is skipped."""
        periods = [_sleep_period("2026-04-28"), {"total_sleep_duration": 28800}]
        records = oura_client.sleep_periods_to_records(periods, {})
        assert list(records.keys()) == ["2026-04-28"]

    def test_uses_longest_period_for_timing(self):
        """The longest period's bedtime/wake should be used."""
        periods = [
            _sleep_period(
                "2026-04-28",
                duration=3600,  # 1h nap
                bedtime_start="2026-04-28T14:00:00+02:00",
                bedtime_end="2026-04-28T15:00:00+02:00",
            ),
            _sleep_period(
                "2026-04-28",
                duration=25200,  # 7h main
                bedtime_start="2026-04-28T02:26:01+02:00",
                bedtime_end="2026-04-28T10:16:58+02:00",
            ),
        ]
        records = oura_client.sleep_periods_to_records(periods, {})
        r = records["2026-04-28"]
        assert r["bedtime"] == "02:26:01"  # from the longer period

    def test_latency_handling(self):
        p = _sleep_period("2026-04-28", latency=630)
        records = oura_client.sleep_periods_to_records([p], {})
        assert records["2026-04-28"]["sleep_latency"] == pytest.approx(10.5)

    def test_null_latency(self):
        p = _sleep_period("2026-04-28", latency=None)
        records = oura_client.sleep_periods_to_records([p], {})
        assert records["2026-04-28"].get("sleep_latency") is None


# ── pull_all integration ──────────────────────────────────────────────────────


class TestPullAll:
    def test_includes_post_midnight_sleep(self, monkeypatch):
        """Regression: post-midnight sleeps need end_date extended by 1 day."""

        def fake_get(endpoint, params):
            if endpoint == "sleep":
                if params == {"start_date": "2026-04-27", "end_date": "2026-04-29"}:
                    return {"data": [_sleep_period("2026-04-28")]}
                return {"data": []}
            if endpoint == "daily_sleep":
                return {"data": [{"day": "2026-04-28", "score": 85}]}
            if endpoint == "daily_readiness":
                return {"data": [{"day": "2026-04-28", "score": 75}]}
            if endpoint == "daily_activity":
                return {"data": []}
            raise AssertionError(f"unexpected endpoint: {endpoint}")

        monkeypatch.setattr(oura_client, "date", FixedDate)
        monkeypatch.setattr(oura_client, "_get", fake_get)

        records = oura_client.pull_all(days=1)
        assert "2026-04-28" in records
        assert records["2026-04-28"]["sleep_duration_hours"] == pytest.approx(8.0)

    def test_pulls_multiple_days(self, monkeypatch):
        def fake_get(endpoint, params):
            if endpoint == "sleep":
                return {
                    "data": [
                        _sleep_period("2026-04-27"),
                        _sleep_period("2026-04-28"),
                    ]
                }
            if endpoint == "daily_sleep":
                return {
                    "data": [
                        {"day": "2026-04-27", "score": 79},
                        {"day": "2026-04-28", "score": 85},
                    ]
                }
            if endpoint == "daily_readiness":
                return {
                    "data": [
                        {"day": "2026-04-27", "score": 80},
                        {"day": "2026-04-28", "score": 75},
                    ]
                }
            if endpoint == "daily_activity":
                return {"data": []}
            raise AssertionError(f"unexpected endpoint: {endpoint}")

        monkeypatch.setattr(oura_client, "date", FixedDate)
        monkeypatch.setattr(oura_client, "_get", fake_get)

        records = oura_client.pull_all(days=2)
        assert len(records) == 2
        assert records["2026-04-27"]["sleep_score"] == 79
        assert records["2026-04-28"]["sleep_score"] == 85
