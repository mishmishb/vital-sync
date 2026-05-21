from datetime import UTC, date, datetime
from pathlib import Path

from vital_sync import morning_checkin as mc
from vital_sync import weekly_report as wr
from vital_sync.hevy_client import HevyWorkout


def _hevy_workout(
    *, title: str, start_time: str, end_time: str, created_at: str | None = None
) -> HevyWorkout:
    start = datetime.fromisoformat(start_time)
    end = datetime.fromisoformat(end_time)
    created = datetime.fromisoformat(created_at) if created_at else start.astimezone(UTC)
    return HevyWorkout(
        id=f"{title}-{start_time}",
        title=title,
        routine_id=None,
        description="",
        start_time=start,
        end_time=end,
        updated_at=end.astimezone(UTC),
        created_at=created,
        exercises=[],
    )


def test_build_sleep_data_uses_primary_sleep_period_and_yesterday_activity() -> None:
    sleep_periods = [
        {
            "day": "2026-05-16",
            "type": "rest",
            "bedtime_start": "2026-05-15T21:57:27.000+02:00",
            "bedtime_end": "2026-05-15T22:40:57.000+02:00",
            "total_sleep_duration": 1512,
            "efficiency": 59,
            "average_heart_rate": 79.0,
            "lowest_heart_rate": 77,
            "average_hrv": 19,
            "deep_sleep_duration": 60,
            "rem_sleep_duration": 30,
            "latency": 600,
        },
        {
            "day": "2026-05-16",
            "type": "long_sleep",
            "bedtime_start": "2026-05-15T23:42:28.000+02:00",
            "bedtime_end": "2026-05-16T09:28:19.000+02:00",
            "total_sleep_duration": 31860,
            "efficiency": 91,
            "average_heart_rate": 59.375,
            "lowest_heart_rate": 54,
            "average_hrv": 40,
            "deep_sleep_duration": 7050,
            "rem_sleep_duration": 8460,
            "latency": 510,
        },
    ]
    daily_sleep = {"2026-05-16": {"score": 94}}
    daily_readiness = {"2026-05-16": {"score": 84}}
    daily_activity = {
        "2026-05-15": {"score": 98},
        "2026-05-16": {"score": None},
    }

    sleep_data = mc._build_sleep_data(
        today="2026-05-16",
        yesterday="2026-05-15",
        sleep_periods=sleep_periods,
        daily_sleep=daily_sleep,
        daily_readiness=daily_readiness,
        daily_activity=daily_activity,
    )

    assert sleep_data == {
        "duration_h": 8.85,
        "bed": "23:42:28",
        "wake": "09:28:19",
        "efficiency": 91,
        "avg_hr": 59.375,
        "lowest_hr": 54,
        "hrv": 40,
        "deep_sleep_min": 117,
        "rem_sleep_min": 141,
        "latency_min": 8,
        "sleep_score": 94,
        "readiness": 84,
        "activity_score": 98,
    }


def test_build_effective_tags_merges_history_and_record_tags_without_duplicates() -> None:
    tag_data = {
        "baseline_tags": ["sleep mask", "podcast"],
        "history": {
            "2026-05-16": {
                "applied": ["holiday"],
                "negated": ["podcast"],
            }
        },
    }

    effective_tags, negated_tags = mc._build_effective_tags(
        tag_data=tag_data,
        record_tags=["on call", "melatonin", "holiday"],
        target_day="2026-05-16",
    )

    assert effective_tags == ["sleep mask", "holiday", "on call", "melatonin"]
    assert negated_tags == ["podcast"]


def test_build_effective_tags_respects_dated_baseline_start() -> None:
    tag_data = {
        "baseline_tags": ["sleep mask"],
        "baseline_effective_from": {"melatonin": "2026-05-22"},
        "history": {
            "2026-05-23": {
                "applied": [],
                "negated": ["melatonin"],
            }
        },
    }

    before_start, before_negated = mc._build_effective_tags(
        tag_data=tag_data,
        record_tags=None,
        target_day="2026-05-21",
    )
    on_start, on_start_negated = mc._build_effective_tags(
        tag_data=tag_data,
        record_tags=None,
        target_day="2026-05-22",
    )
    negated_day, negated_day_negated = mc._build_effective_tags(
        tag_data=tag_data,
        record_tags=None,
        target_day="2026-05-23",
    )

    assert before_start == ["sleep mask"]
    assert before_negated == []
    assert on_start == ["sleep mask", "melatonin"]
    assert on_start_negated == []
    assert negated_day == ["sleep mask"]
    assert negated_day_negated == ["melatonin"]


def test_sleep_tag_deviations_use_active_baselines_for_each_date(tmp_path: Path) -> None:
    tags_path = tmp_path / "sleep_tags.json"
    tags_path.write_text(
        """
{
  "baseline_tags": ["sleep mask"],
  "baseline_effective_from": {"melatonin": "2026-05-22"},
  "history": {
    "2026-05-22": {"applied": [], "negated": ["melatonin"]},
    "2026-05-21": {"applied": ["melatonin"], "negated": []}
  }
}
""".strip()
    )
    records = [
        wr.DailyRecord(date=date(2026, 5, 21), sleep_tags=["melatonin"]),
        wr.DailyRecord(date=date(2026, 5, 22), negated_baseline_tags=["melatonin"]),
    ]

    summary = wr._sleep_tag_deviations(records, str(tags_path), window_start=date(2026, 5, 20))

    assert summary["applied_extras"] == {"melatonin": ["2026-05-21"]}
    assert summary["negated_baselines"] == {"melatonin": ["2026-05-22"]}


def test_build_activity_data_uses_daily_activity_as_primary_context() -> None:
    daily_activity = {
        "2026-05-15": {
            "score": 98,
            "steps": 6516,
            "active_calories": 620,
            "equivalent_walking_distance": 12168,
            "medium_activity_time": 2400,
            "high_activity_time": 180,
            "inactivity_alerts": 2,
            "contributors": {
                "meet_daily_targets": 100,
                "move_every_hour": 78,
                "recovery_time": 87,
                "stay_active": 89,
                "training_frequency": 100,
                "training_volume": 100,
            },
        }
    }
    workouts = [
        {
            "day": "2026-05-15",
            "activity": "walking",
            "intensity": "moderate",
            "calories": 47.7,
            "distance": 585.4,
            "start_datetime": "2026-05-15T17:48:00.000+02:00",
            "end_datetime": "2026-05-15T18:00:00.000+02:00",
        },
        {
            "day": "2026-05-15",
            "activity": "houseWork",
            "intensity": "moderate",
            "calories": 52.2,
            "distance": 6.3,
            "start_datetime": "2026-05-15T12:37:00.000+02:00",
            "end_datetime": "2026-05-15T12:48:00.000+02:00",
        },
        {
            "day": "2026-05-14",
            "activity": "walking",
            "intensity": "moderate",
            "calories": 99.0,
            "distance": 999.0,
            "start_datetime": "2026-05-14T17:00:00.000+02:00",
            "end_datetime": "2026-05-14T17:30:00.000+02:00",
        },
    ]

    activity_data = mc._build_activity_data(
        yesterday="2026-05-15",
        daily_activity=daily_activity,
        workouts=workouts,
    )

    assert activity_data == {
        "score": 98,
        "steps": 6516,
        "active_calories": 620,
        "equivalent_walking_distance_m": 12168,
        "medium_activity_min": 40,
        "high_activity_min": 3,
        "inactivity_alerts": 2,
        "contributors": {
            "meet_daily_targets": 100,
            "move_every_hour": 78,
            "recovery_time": 87,
            "stay_active": 89,
            "training_frequency": 100,
            "training_volume": 100,
        },
        "oura_workouts": [
            {
                "activity": "walking",
                "intensity": "moderate",
                "duration_min": 12,
                "calories": 48,
                "distance_m": 585,
            },
            {
                "activity": "houseWork",
                "intensity": "moderate",
                "duration_min": 11,
                "calories": 52,
                "distance_m": 6,
            },
        ],
        "suppressed_oura_duplicates": 0,
    }


def test_build_activity_data_suppresses_oura_strength_duplicate_when_hevy_overlaps() -> None:
    daily_activity = {
        "2026-05-15": {
            "score": 92,
            "steps": 8000,
            "active_calories": 540,
            "equivalent_walking_distance": 9000,
            "medium_activity_time": 1800,
            "high_activity_time": 600,
            "inactivity_alerts": 1,
            "contributors": {},
        }
    }
    workouts = [
        {
            "day": "2026-05-15",
            "activity": "strength_training",
            "intensity": "moderate",
            "calories": 410,
            "distance": 0,
            "source": "confirmed",
            "start_datetime": "2026-05-15T18:00:00+02:00",
            "end_datetime": "2026-05-15T19:05:00+02:00",
        },
        {
            "day": "2026-05-15",
            "activity": "walking",
            "intensity": "moderate",
            "calories": 95,
            "distance": 1500,
            "source": "confirmed",
            "start_datetime": "2026-05-15T20:00:00+02:00",
            "end_datetime": "2026-05-15T20:25:00+02:00",
        },
    ]
    hevy_workouts = [
        _hevy_workout(
            title="Upper Body",
            start_time="2026-05-15T18:02:00+02:00",
            end_time="2026-05-15T19:03:00+02:00",
        )
    ]

    activity_data = mc._build_activity_data(
        yesterday="2026-05-15",
        daily_activity=daily_activity,
        workouts=workouts,
        hevy_workouts=hevy_workouts,
    )

    assert activity_data["oura_workouts"] == [
        {
            "activity": "walking",
            "intensity": "moderate",
            "duration_min": 25,
            "calories": 95,
            "distance_m": 1500,
        }
    ]
    assert activity_data["suppressed_oura_duplicates"] == 1


def test_build_activity_data_keeps_non_strength_oura_workout_even_if_hevy_exists_same_day() -> (
    None
):
    daily_activity = {"2026-05-15": {"score": 88, "contributors": {}}}
    workouts = [
        {
            "day": "2026-05-15",
            "activity": "cycling",
            "intensity": "moderate",
            "calories": 210,
            "distance": 7200,
            "source": "confirmed",
            "start_datetime": "2026-05-15T17:00:00+02:00",
            "end_datetime": "2026-05-15T17:40:00+02:00",
        }
    ]
    hevy_workouts = [
        _hevy_workout(
            title="Legs",
            start_time="2026-05-15T19:00:00+02:00",
            end_time="2026-05-15T20:00:00+02:00",
        )
    ]

    activity_data = mc._build_activity_data(
        yesterday="2026-05-15",
        daily_activity=daily_activity,
        workouts=workouts,
        hevy_workouts=hevy_workouts,
    )

    assert activity_data["oura_workouts"] == [
        {
            "activity": "cycling",
            "intensity": "moderate",
            "duration_min": 40,
            "calories": 210,
            "distance_m": 7200,
        }
    ]
    assert activity_data["suppressed_oura_duplicates"] == 0
