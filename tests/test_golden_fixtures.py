"""Golden fixture tests for Oura and Hevy conversion paths."""

import json
from pathlib import Path

import pytest

from vital_sync.analytics import DailyRecord, merge_hevy_into_records
from vital_sync.hevy_client import _parse_workout, export_for_cache
from vital_sync.oura_client import sleep_periods_to_records

FIXTURES = Path(__file__).parent / "fixtures"


def test_oura_sleep_period_fixture_converts_segments_and_daily_metrics() -> None:
    periods = json.loads((FIXTURES / "oura_sleep_periods.json").read_text())
    daily_sleep = {"2026-05-20": {"score": 86}, "2026-05-21": {"score": 40}}
    daily_readiness = {"2026-05-20": {"score": 79}}
    daily_activity = {"2026-05-20": {"score": 88, "steps": 10420}}

    records = sleep_periods_to_records(periods, daily_sleep, daily_readiness, daily_activity)

    assert list(records) == ["2026-05-20", "2026-05-21"]
    assert records["2026-05-20"] == {
        "date": "2026-05-20",
        "sources": ["oura"],
        "sleep_duration_hours": pytest.approx(8.0),
        "deep_sleep_min": pytest.approx(100.0),
        "rem_sleep_min": pytest.approx(135.0),
        "sleep_efficiency": 91,
        "bedtime": "23:30:00",
        "wake_time": "06:45:00",
        "avg_hr": 54,
        "lowest_hr": 47,
        "hrv_ms": 48,
        "sleep_latency": pytest.approx(10.0),
        "time_in_bed": 27000,
        "sleep_score": 86,
        "readiness_score": 79,
        "activity_score": 88,
        "steps": 10420,
    }
    assert records["2026-05-21"] == {
        "date": "2026-05-21",
        "sources": ["oura"],
        "sleep_score": 40,
    }


def test_hevy_workout_fixture_parses_exports_and_merges_into_daily_records(monkeypatch) -> None:
    monkeypatch.setattr(
        "vital_sync.hevy_client._template_lookup",
        {"bench-template": {"primary": "chest", "secondary": ["triceps"], "title": "Bench"}},
    )
    raw_workout = json.loads((FIXTURES / "hevy_workouts.json").read_text())[0]

    workout = _parse_workout(raw_workout)
    exported = export_for_cache([workout])
    merged = merge_hevy_into_records(
        [workout], [DailyRecord(date=workout.start_time.date(), sleep_score=82, sources=["oura"])]
    )

    assert workout.id == "hevy-golden-1"
    assert workout.total_volume == 880
    assert workout.duration_minutes == pytest.approx(45)
    assert exported == [
        {
            "date": "2026-05-20",
            "workout_id": "hevy-golden-1",
            "workout_title": "Push Day",
            "exercise": "Bench Press (Barbell)",
            "muscle_groups": ["chest", "triceps"],
            "set_type": "warmup",
            "weight_kg": 60,
            "reps": 8,
            "rpe": 6,
            "volume": 480,
            "est_1rm": pytest.approx(76),
        },
        {
            "date": "2026-05-20",
            "workout_id": "hevy-golden-1",
            "workout_title": "Push Day",
            "exercise": "Bench Press (Barbell)",
            "muscle_groups": ["chest", "triceps"],
            "set_type": "normal",
            "weight_kg": 80,
            "reps": 5,
            "rpe": 8.5,
            "volume": 400,
            "est_1rm": pytest.approx(93.3333333333),
        },
    ]
    assert merged[0].sleep_score == 82
    assert merged[0].hevy_workouts == 1
    assert merged[0].hevy_total_volume_kg == 880
    assert merged[0].hevy_total_duration_min == 45
    assert merged[0].hevy_muscle_groups == ["chest", "triceps"]
    assert merged[0].hevy_max_weight_kg == 80
    assert merged[0].hevy_avg_rpe == pytest.approx(7.2)
    assert merged[0].sources == ["oura", "hevy"]
