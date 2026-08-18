"""Focused tests for weekly report helper and JSON output behaviour."""

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from vital_sync import weekly_report
from vital_sync.analytics import DailyRecord


class FixedDate(date):
    @classmethod
    def today(cls) -> "FixedDate":
        return cls(2026, 5, 28)


def _record(day: date, **kwargs) -> DailyRecord:
    record = DailyRecord(date=day, **kwargs)
    return record


def test_safe_float_accepts_numeric_values_and_rejects_bad_input() -> None:
    assert weekly_report._safe_float("78.4") == pytest.approx(78.4)
    assert weekly_report._safe_float(10) == pytest.approx(10.0)
    assert weekly_report._safe_float(None) is None
    assert weekly_report._safe_float("bad") is None


def test_circular_time_stats_handle_midnight_wraparound() -> None:
    mean = weekly_report._circular_mean([23 * 60 + 50, 10])
    std = weekly_report._circular_std([23 * 60 + 50, 10])

    assert mean is not None
    assert min(mean, 1440 - mean) == pytest.approx(0, abs=11)
    assert std is not None
    assert std < 20
    assert weekly_report._circular_mean([]) is None
    assert weekly_report._circular_std([60]) is None


def test_time_to_minutes_accepts_plain_and_iso_times() -> None:
    assert weekly_report._time_to_minutes("07:45") == 465
    assert weekly_report._time_to_minutes("2026-05-20T23:30:00+02:00") == 1410
    assert weekly_report._time_to_minutes(None) is None
    assert weekly_report._time_to_minutes("not-a-time") is None


def test_sleep_tag_deviations_summarises_flat_tags_against_baseline(tmp_path: Path) -> None:
    tags_path = tmp_path / "sleep_tags.json"
    tags_path.write_text(
        json.dumps(
            {
                "canonical_tags": {"melatonin": "", "noise": ""},
                "baseline_tags": ["melatonin"],
                "tags_by_date": {
                    "2026-05-20": ["noise"],
                    "2026-05-21": ["melatonin", "noise"],
                },
            }
        )
    )
    records = [
        _record(date(2026, 5, 19), sleep_tags=["ignored"]),
        _record(date(2026, 5, 20), sleep_tags=["late caffeine"]),
        _record(date(2026, 5, 21), sleep_tags=["noise"]),
    ]

    result = weekly_report._sleep_tag_deviations(records, str(tags_path), date(2026, 5, 20))

    assert result == {
        "removed_baselines": {"melatonin": ["2026-05-20"]},
        "extra_tags": {
            "noise": ["2026-05-20", "2026-05-21"],
            "late caffeine": ["2026-05-20"],
        },
    }
    assert weekly_report._sleep_tag_deviations(
        records, str(tmp_path / "missing.json"), date(2026, 5, 20)
    ) == {
        "removed_baselines": {},
        "extra_tags": {},
    }


def test_hevy_summary_filters_dates_and_calculates_patterns() -> None:
    workouts = [
        {
            "start_time": "2026-05-20T18:00:00+00:00",
            "end_time": "2026-05-20T19:00:00+00:00",
            "exercises": [
                {
                    "muscle_group": "chest",
                    "sets": [{"weight_kg": "80", "reps": "5"}, {"weight_kg": 60, "reps": 8}],
                },
                {"muscle_group": "back", "sets": [{"weight_kg": 70, "reps": 6}]},
            ],
        },
        {"start_time": "bad", "exercises": []},
        {"start_time": "2026-04-01T18:00:00+00:00", "exercises": []},
    ]

    summary = weekly_report._hevy_summary(workouts, date(2026, 5, 18), date(2026, 5, 21))

    assert summary["sessions"] == 1
    assert summary["volume_kg"] == 1300
    assert summary["duration_min"] == 60
    assert summary["days_trained"] == 1
    assert summary["avg_volume_per_session"] == 1300
    assert summary["muscle_group_distribution"] == {"chest": 1, "back": 1}
    assert summary["movement_pattern_balance"]["push"] == 50
    assert summary["movement_pattern_balance"]["pull"] == 50


def test_body_composition_uses_latest_non_empty_values_and_averages() -> None:
    result = weekly_report._body_composition(
        [
            _record(date(2026, 5, 20), weight_kg=78.0, body_fat_pct=18.2),
            _record(date(2026, 5, 21), weight_kg=77.6, muscle_mass_kg=62.4),
        ]
    )

    assert result == {
        "latest": {"weight_kg": 77.6, "body_fat_pct": 18.2, "muscle_mass_kg": 62.4},
        "weight_avg": 77.8,
        "body_fat_avg": 18.2,
        "muscle_mass_avg": 62.4,
    }
    assert weekly_report._body_composition([]) == {
        "latest": None,
        "weight_avg": None,
        "body_fat_avg": None,
        "muscle_mass_avg": None,
    }


def test_circadian_stats_reads_sleep_timing_fields_with_weekday_weekend_split() -> None:
    records = [
        _record(date(2026, 5, 20)),
        _record(date(2026, 5, 24)),
    ]
    object.__setattr__(records[0], "bedtime_start", "2026-05-19T23:30:00+02:00")
    object.__setattr__(records[0], "bedtime_end", "2026-05-20T07:30:00+02:00")
    object.__setattr__(records[1], "bedtime_start", "2026-05-24T00:10:00+02:00")
    object.__setattr__(records[1], "bedtime_end", "2026-05-24T09:30:00+02:00")

    stats = weekly_report._circadian_stats(records)

    assert stats["bedtime_std_min"] is not None
    assert stats["waketime_std_min"] is not None
    assert stats["social_jetlag_min"] == 120
    assert 0 <= stats["consistency_score"] <= 100


def test_main_outputs_structured_weekly_json(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setattr(weekly_report, "date", FixedDate)
    data_dir = tmp_path / "data"
    hevy_cache_dir = data_dir / "hevy_cache"
    hevy_cache_dir.mkdir(parents=True)
    tags_path = data_dir / "sleep_tags.json"
    tags_path.write_text(
        json.dumps(
            {
                "canonical_tags": {"melatonin": "", "late caffeine": ""},
                "baseline_tags": ["melatonin"],
                "tags_by_date": {
                    "2026-05-20": ["late caffeine"],
                },
            }
        )
    )
    (hevy_cache_dir / "workouts.json").write_text(
        json.dumps(
            [
                {
                    "start_time": "2026-05-20T18:00:00+00:00",
                    "end_time": "2026-05-20T18:45:00+00:00",
                    "exercises": [
                        {"muscle_group": "chest", "sets": [{"weight_kg": 80, "reps": 5}]}
                    ],
                }
            ]
        )
    )
    records = []
    start = date(2026, 5, 7)
    for offset in range(21):
        day = start + timedelta(days=offset)
        records.append(
            _record(
                day,
                sleep_score=80 + (offset % 5),
                readiness_score=75 + (offset % 4),
                activity_score=70 + (offset % 3),
                sleep_duration_hours=7 + (offset % 2) * 0.5,
                sleep_efficiency=88,
                deep_sleep_min=80 + offset,
                rem_sleep_min=90 + offset,
                avg_hr=55 + (offset % 3),
                hrv_ms=40 + offset,
                steps=8000 + offset * 100,
                weight_kg=78 - offset * 0.05,
                body_fat_pct=18.5,
                muscle_mass_kg=62.0,
                sleep_tags=["late caffeine"] if day == date(2026, 5, 20) else [],
                hevy_workouts=1 if day == date(2026, 5, 20) else None,
                hevy_total_volume_kg=400 if day == date(2026, 5, 20) else None,
            )
        )
    records[-1].sleep_score = 50
    records[-1].readiness_score = 50
    monkeypatch.setattr(weekly_report, "load_cache", lambda _path: records)
    monkeypatch.setattr(
        weekly_report,
        "_get_data_paths",
        lambda: (data_dir, data_dir / "cache.json", hevy_cache_dir, tags_path),
    )

    weekly_report.main()

    output = json.loads(capsys.readouterr().out)
    assert output["report_period"] == "2026-05-14 to 2026-05-27"
    assert output["days_in_window"] == 14
    assert "sleep_score" in output["baselines"]
    assert output["comparison_vs_previous_week"]["sleep_score"]["trend"] in {"up", "down", "flat"}
    assert output["sleep_tags"]["removed_baselines"] == {"melatonin": 1}
    assert output["sleep_tags"]["extra_tags"] == {"late caffeine": 1}
    assert output["hevy"]["sessions"] == 1
    assert output["body_composition"]["latest"]["weight_kg"] == pytest.approx(77.0)
    assert "circadian" in output
    assert "zone_time" in output
    assert output["zone_time"]["goal_min"] == 150


def test_zone_time_summary_current_week_progress_and_previous_weeks() -> None:
    # Window ends Sunday 2026-08-16. Records Mon-Sun that week: 100 + 50 = 150.
    records = [
        _record(date(2026, 8, 10), hr_zone_min={"z2plus_min": 100.0, "Z2": 100.0}),
        _record(date(2026, 8, 13), hr_zone_min={"z2plus_min": 0.0}),
        _record(date(2026, 8, 16), hr_zone_min={"z2plus_min": 50.0, "Z3": 50.0}),
        _record(date(2026, 8, 17), hr_zone_min={"z2plus_min": 60.0, "Z2": 60.0}),
    ]
    summary = weekly_report._zone_time_summary(records, window_end=date(2026, 8, 16))

    assert summary["goal_min"] == 150
    assert summary["current_week"]["week_start"] == "2026-08-10"
    assert summary["current_week"]["z2plus_min"] == pytest.approx(150.0)
    assert summary["current_week"]["remaining_min"] == pytest.approx(0.0)
    assert summary["current_week"]["goal_met"] is True
    # 2026-08-17 starts the NEXT ISO week — after window end, not a previous week.
    assert summary["previous_weeks"] == []
    assert summary["streak_weeks_met"] == 0  # no COMPLETE previous week met


def test_zone_time_summary_week_below_goal_reports_remaining() -> None:
    records = [
        _record(date(2026, 8, 10), hr_zone_min={"z2plus_min": 40.0, "Z2": 40.0}),
    ]
    summary = weekly_report._zone_time_summary(records, window_end=date(2026, 8, 16))

    assert summary["current_week"]["z2plus_min"] == pytest.approx(40.0)
    assert summary["current_week"]["remaining_min"] == pytest.approx(110.0)
    assert summary["current_week"]["goal_met"] is False


def test_zone_time_summary_no_zone_data_returns_empty_shape() -> None:
    records = [_record(date(2026, 8, 10))]
    summary = weekly_report._zone_time_summary(records, window_end=date(2026, 8, 16))
    assert summary["current_week"] is None
    assert summary["daily_z2plus"] == []


def test_zone_time_summary_vo2max_latest() -> None:
    records = [
        _record(date(2026, 8, 1), vo2max=40.0),
        _record(date(2026, 8, 18), vo2max=42.0),
        _record(date(2026, 8, 17)),
    ]
    summary = weekly_report._zone_time_summary(records, window_end=date(2026, 8, 18))
    assert summary["vo2max_latest"] == {"date": "2026-08-18", "vo2max": 42.0}


def test_zone_time_summary_vo2max_uses_all_records_scope() -> None:
    records = [_record(date(2026, 8, 10))]
    all_records = records + [_record(date(2026, 8, 18), vo2max=42.0)]
    summary = weekly_report._zone_time_summary(
        records, window_end=date(2026, 8, 16), all_records=all_records
    )
    assert summary["vo2max_latest"] == {"date": "2026-08-18", "vo2max": 42.0}


def test_zone_time_summary_no_vo2max_returns_none() -> None:
    records = [_record(date(2026, 8, 17))]
    summary = weekly_report._zone_time_summary(records, window_end=date(2026, 8, 18))
    assert summary["vo2max_latest"] is None


def test_zone_time_summary_streak_counts_completed_weeks_only() -> None:
    # Two full previous weeks met; current week (Mon 2026-08-10) in progress.
    records = [
        _record(date(2026, 7, 27), hr_zone_min={"z2plus_min": 160.0}),
        _record(date(2026, 8, 2), hr_zone_min={"z2plus_min": 120.0}),
        _record(date(2026, 8, 3), hr_zone_min={"z2plus_min": 170.0}),
        _record(date(2026, 8, 9), hr_zone_min={"z2plus_min": 200.0}),
    ]
    summary = weekly_report._zone_time_summary(records, window_end=date(2026, 8, 16))
    # weeks met strictly before current week's Monday (2026-08-10):
    # 2026-07-27 (160 >= 150) and 2026-08-03 (170+200) -> 2
    assert summary["streak_weeks_met"] == 2
