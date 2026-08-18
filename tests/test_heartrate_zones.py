"""
Tests for heart-rate zone time computation.

Zone model mirrors the Oura app: 6 zones defined as percentages of max HR.
- Z0: <=49% of max HR
- Z1: 50-59% of max HR
- Z2: 60-69% of max HR
- Z3: 70-79% of max HR
- Z4: 80-89% of max HR
- Z5: 90-100% of max HR

Michael's Oura app max HR = 187 (Tanaka for age 30) — thresholds:
Z0 <92, Z1 94-110, Z2 112-129, Z3 131-147, Z4 150-166, Z5 >=168.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from vital_sync.heartrate_zones import (
    ZONE_LABELS,
    compute_daily_zone_minutes,
    sample_zone,
    weekly_z2plus,
)


def ts(dt: str) -> datetime:
    return datetime.fromisoformat(dt.replace("Z", "+00:00"))


class TestSampleZone:
    """Mapping a single bpm value to a zone at max_hr=187."""

    def test_zone0_below_49_percent(self):
        assert sample_zone(91, max_hr=187) == "Z0"

    def test_zone1_50_to_59_percent(self):
        assert sample_zone(94, max_hr=187) == "Z1"
        assert sample_zone(110, max_hr=187) == "Z1"

    def test_zone2_60_to_69_percent(self):
        assert sample_zone(112, max_hr=187) == "Z2"
        assert sample_zone(129, max_hr=187) == "Z2"

    def test_zone3_70_to_79_percent(self):
        assert sample_zone(131, max_hr=187) == "Z3"
        assert sample_zone(147, max_hr=187) == "Z3"

    def test_zone4_80_to_89_percent(self):
        assert sample_zone(150, max_hr=187) == "Z4"
        assert sample_zone(166, max_hr=187) == "Z4"

    def test_zone5_90_percent_and_above(self):
        assert sample_zone(168, max_hr=187) == "Z5"
        assert sample_zone(187, max_hr=187) == "Z5"

    def test_boundary_60_percent_is_zone2(self):
        # 0.60 * 187 = 112.2 — the Z2 floor is 112 bpm
        assert sample_zone(112, max_hr=187) == "Z2"

    def test_custom_max_hr_recomputes_thresholds(self):
        # max_hr=190: 60% floor = 114 bpm. Z1 ceiling = round(0.59*190) = 112
        assert sample_zone(112, max_hr=190) == "Z1"
        assert sample_zone(114, max_hr=190) == "Z2"


class TestComputeDailyZoneMinutes:
    """Interval aggregation between consecutive samples."""

    def test_single_sample_no_intervals(self):
        samples = [{"timestamp": "2026-08-12T10:00:00Z", "bpm": 120}]
        result = compute_daily_zone_minutes(samples)
        assert result == {}

    def test_two_samples_same_day(self):
        samples = [
            {"timestamp": "2026-08-12T10:00:00Z", "bpm": 120},  # Z2
            {"timestamp": "2026-08-12T10:10:00Z", "bpm": 120},  # Z2
        ]
        result = compute_daily_zone_minutes(samples, max_hr=187)
        assert "2026-08-12" in result
        day = result["2026-08-12"]
        assert day["Z2"] == pytest.approx(10.0)
        assert day["Z1"] == 0
        assert day["z2plus_min"] == pytest.approx(10.0)

    def test_gap_over_10_minutes_skipped(self):
        samples = [
            {"timestamp": "2026-08-12T10:00:00Z", "bpm": 120},
            {"timestamp": "2026-08-12T10:11:00Z", "bpm": 120},
        ]
        result = compute_daily_zone_minutes(samples, max_hr=187)
        assert "2026-08-12" not in result

    def test_midpoint_classification_across_boundary(self):
        # 110 (Z1) and 130 (Z3) → midpoint 120 = Z2: 10 minutes in Z2
        samples = [
            {"timestamp": "2026-08-12T10:00:00Z", "bpm": 110},
            {"timestamp": "2026-08-12T10:10:00Z", "bpm": 130},
        ]
        result = compute_daily_zone_minutes(samples, max_hr=187)
        assert result["2026-08-12"]["Z2"] == pytest.approx(10.0)
        assert result["2026-08-12"]["z2plus_min"] == pytest.approx(10.0)

    def test_multiple_zones_separate_days(self):
        samples = [
            {"timestamp": "2026-08-12T10:00:00Z", "bpm": 140},  # Z3
            {"timestamp": "2026-08-12T10:05:00Z", "bpm": 140},  # Z3
            {"timestamp": "2026-08-13T09:00:00Z", "bpm": 120},  # Z2
            {"timestamp": "2026-08-13T09:05:00Z", "bpm": 120},  # Z2
        ]
        result = compute_daily_zone_minutes(samples, max_hr=187)
        assert result["2026-08-12"]["Z3"] == pytest.approx(5.0)
        assert result["2026-08-13"]["Z2"] == pytest.approx(5.0)

    def test_z2plus_sums_all_zones_above_floor(self):
        # Midpoints: (120+140)/2 = 130 -> Z3, (140+160)/2 = 150 -> Z4
        samples = [
            {"timestamp": "2026-08-12T10:00:00Z", "bpm": 120},  # Z2
            {"timestamp": "2026-08-12T10:05:00Z", "bpm": 140},  # Z3
            {"timestamp": "2026-08-12T10:10:00Z", "bpm": 160},  # Z4
        ]
        result = compute_daily_zone_minutes(samples, max_hr=187)
        assert result["2026-08-12"]["z2plus_min"] == pytest.approx(10.0)
        assert result["2026-08-12"]["Z2"] == 0.0
        assert result["2026-08-12"]["Z3"] == pytest.approx(5.0)
        assert result["2026-08-12"]["Z4"] == pytest.approx(5.0)

    def test_zone_labels_complete(self):
        assert ZONE_LABELS == ["Z0", "Z1", "Z2", "Z3", "Z4", "Z5"]


class TestWeeklyZ2plus:
    """Mon-Sun weekly aggregation of z2plus minutes."""

    def _zone_data(self) -> dict[str, dict[str, float]]:
        # Mon 2026-08-10 .. Sun 2026-08-16: 100 + 50 = 150 min
        monday = {"z2plus_min": 100.0}
        sunday = {"z2plus_min": 50.0}
        return {
            "2026-08-10": monday,
            "2026-08-13": {"z2plus_min": 0.0},
            "2026-08-16": sunday,
            "2026-08-17": {"z2plus_min": 60.0},  # next ISO week
        }

    def test_weekly_totals_by_iso_week(self):
        weeks = weekly_z2plus(self._zone_data())
        assert "2026-08-10" in weeks  # Monday anchor
        assert weeks["2026-08-10"] == pytest.approx(150.0)
        assert "2026-08-17" in weeks
        assert weeks["2026-08-17"] == pytest.approx(60.0)

    def test_empty_input(self):
        assert weekly_z2plus({}) == {}

    def test_days_without_z2plus_key(self):
        data = {"2026-08-10": {"Z1": 5.0}}
        weeks = weekly_z2plus(data)
        assert weeks["2026-08-10"] == 0.0

    def test_weeks_are_mon_sun(self):
        # 2026-08-08 is a Saturday: belongs to Mon 2026-08-03 week
        data = {"2026-08-08": {"z2plus_min": 30.0}}
        weeks = weekly_z2plus(data)
        assert "2026-08-03" in weeks
        assert weeks["2026-08-03"] == pytest.approx(30.0)
