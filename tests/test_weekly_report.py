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


def test_sleep_tag_deviations_counts_negated_baselines_and_extra_tags(tmp_path: Path) -> None:
    tags_path = tmp_path / "sleep_tags.json"
    tags_path.write_text(
        json.dumps(
            {
                "canonical_tags": ["melatonin", "noise"],
                "baseline_tags": ["melatonin"],
                "history": {
                    "2026-05-20": {"negated": ["melatonin"], "applied": ["noise"]},
                    "2026-05-21": {"negated": [], "applied": []},
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
        "negated_baselines": {"melatonin": ["2026-05-20"]},
        "applied_extras": {
            "noise": ["2026-05-20", "2026-05-21"],
            "late caffeine": ["2026-05-20"],
        },
    }
    assert weekly_report._sleep_tag_deviations(
        records, str(tmp_path / "missing.json"), date(2026, 5, 20)
    ) == {
        "negated_baselines": {},
        "applied_extras": {},
        "learned": [],
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
                "canonical_tags": ["melatonin", "late caffeine"],
                "baseline_tags": ["melatonin"],
                "history": {
                    "2026-05-20": {"negated": ["melatonin"], "applied": ["late caffeine"]}
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
    assert output["sleep_tags"]["negated_baselines"] == {"melatonin": 1}
    assert output["sleep_tags"]["applied_extras"] == {"late caffeine": 1}
    assert output["hevy"]["sessions"] == 1
    assert output["body_composition"]["latest"]["weight_kg"] == pytest.approx(77.0)
    assert "circadian" in output
