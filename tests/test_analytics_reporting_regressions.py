"""Regression tests for analytics reporting, CSV, Hevy merge, and correlations."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from vital_sync import analytics
from vital_sync.analytics import (
    BaselineStats,
    CorrelationResult,
    DailyRecord,
    DeviationAlert,
    compute_circadian_consistency,
    compute_correlations,
    compute_tag_sleep_analysis,
    detect_deviations,
    detect_deviations_adaptive,
    detect_deviations_hybrid,
    format_alerts,
    format_baseline_table,
    format_circadian_report,
    format_correlations,
    format_tag_analysis,
    merge_hevy_into_records,
    merge_records,
    parse_mynetdiary_csv,
    parse_renpho_csv,
    readiness_label,
    sleep_score_label,
)
from vital_sync.hevy_client import HevyExercise, HevySet, HevyWorkout


def _record(day: date, **kwargs) -> DailyRecord:
    return DailyRecord(date=day, **kwargs)


def _set(weight: float | None = 50, reps: int | None = 10, rpe: float | None = 8) -> HevySet:
    return HevySet(
        index=0,
        set_type="normal",
        weight_kg=weight,
        reps=reps,
        distance_meters=None,
        duration_seconds=None,
        rpe=rpe,
    )


def _workout(day: date, workout_id: str = "w1", sets: list[HevySet] | None = None) -> HevyWorkout:
    start = datetime(day.year, day.month, day.day, 18, 0, tzinfo=UTC)
    return HevyWorkout(
        id=workout_id,
        title="Bench Day",
        routine_id=None,
        description="",
        start_time=start,
        end_time=start + timedelta(minutes=45),
        updated_at=start + timedelta(minutes=46),
        created_at=start,
        exercises=[
            HevyExercise(
                index=0,
                title="Bench Press",
                exercise_template_id="",
                notes="",
                superset_id=None,
                sets=sets or [_set(80, 5, 8.5), _set(70, 8, 8.0)],
            )
        ],
    )


def test_format_baseline_table_includes_human_names_and_trend_text() -> None:
    table = format_baseline_table(
        {
            "sleep_score": BaselineStats(
                metric="sleep_score",
                n=7,
                mean=82,
                median=83,
                std=3,
                min=76,
                max=86,
                latest=85,
                trend=0.2,
                days_since_change=0,
            )
        }
    )

    assert "Sleep Score" in table
    assert "improving" in table
    assert "85" in table


def test_format_alerts_handles_empty_and_severity_icons() -> None:
    assert format_alerts([]) == "*No significant deviations detected.*"

    rendered = format_alerts(
        [
            DeviationAlert(
                metric="sleep_score",
                date=date(2026, 5, 20),
                value=70,
                baseline_mean=84,
                baseline_std=4,
                severity="major",
                direction="down",
                message="Sleep Score: 70.0",
            )
        ]
    )

    assert "MAJOR" in rendered
    assert "Sleep Score: 70.0" in rendered


def test_parse_renpho_csv_accepts_full_datetime_and_ignores_invalid_values(tmp_path: Path) -> None:
    csv_path = tmp_path / "renpho.csv"
    csv_path.write_text(
        "Date,Weight(kg),Body Fat(%),Muscle Mass(kg)\n"
        "2026-05-20 07:00:00,78.2,18.4,62.1\n"
        "not-a-date,80,20,60\n"
        "2026-05-21,bad,19.1,\n"
    )

    records = parse_renpho_csv(csv_path)

    assert [record.date for record in records] == [date(2026, 5, 20), date(2026, 5, 21)]
    assert records[0].weight_kg == 78.2
    assert records[0].body_fat_pct == 18.4
    assert records[0].muscle_mass_kg == 62.1
    assert records[1].weight_kg is None
    assert records[1].body_fat_pct == 19.1
    assert records[1].sources == ["renpho"]


def test_parse_mynetdiary_csv_accepts_alias_columns_and_skips_bad_dates(tmp_path: Path) -> None:
    csv_path = tmp_path / "mynetdiary.csv"
    csv_path.write_text(
        "Date,Calories Consumed,Protein (g),Weight\n2026-05-20,2500,180,78.4\nbad,100,10,80\n"
    )

    records = parse_mynetdiary_csv(csv_path)

    assert len(records) == 1
    assert records[0].date == date(2026, 5, 20)
    assert records[0].calories_in == 2500
    assert records[0].protein_g == 180
    assert records[0].weight_kg == 78.4
    assert records[0].sources == ["mynetdiary"]


def test_merge_records_combines_sources_and_respects_priority_with_dict_inputs() -> None:
    day = date(2026, 5, 20)
    oura = _record(day, sleep_score=80, weight_kg=77.0, sources=["oura"])
    manual = {
        "date": day.isoformat(),
        "sleep_score": 86,
        "notes": "manual correction",
        "sources": ["manual"],
    }
    renpho = _record(day, weight_kg=78.1, sources=["renpho"])

    merged = merge_records([oura], [renpho], [manual])

    assert len(merged) == 1
    record = merged[0]
    assert record.sleep_score == 86
    assert record.weight_kg == 78.1
    assert record.notes == "manual correction"
    assert record.sources == ["oura", "renpho", "manual"]


def test_merge_hevy_into_records_updates_existing_and_creates_missing_dates() -> None:
    existing_day = date(2026, 5, 20)
    missing_day = date(2026, 5, 21)
    records = [_record(existing_day, sleep_score=82, sources=["oura"])]

    merged = merge_hevy_into_records(
        [_workout(existing_day, "w1"), _workout(missing_day, "w2", [_set(None, 10, None)])],
        records,
    )

    by_date = {record.date: record for record in merged}
    assert by_date[existing_day].hevy_workouts == 1
    assert by_date[existing_day].hevy_total_volume_kg == 960
    assert by_date[existing_day].hevy_total_duration_min == 45
    assert by_date[existing_day].hevy_max_weight_kg == 80
    assert by_date[existing_day].hevy_avg_rpe == pytest.approx(8.2)
    assert "hevy" in by_date[existing_day].sources
    assert by_date[missing_day].sources == ["hevy"]
    assert by_date[missing_day].hevy_total_volume_kg is None


def test_deviation_detectors_cover_fixed_adaptive_and_hybrid_floor_paths() -> None:
    start = date(2026, 5, 1)
    baseline_records = [
        _record(start + timedelta(days=i), sleep_score=90, hrv_ms=50, steps=8000) for i in range(6)
    ]
    latest = _record(start + timedelta(days=6), sleep_score=60, hrv_ms=18, steps=500)
    records = baseline_records + [latest]

    fixed = detect_deviations(records, window_days=7, min_data_points=3)
    adaptive = detect_deviations_adaptive(records, window_days=7, min_data_points=5)
    hybrid = detect_deviations_hybrid(records, window_days=7, min_data_points=5)

    assert any(alert.metric == "sleep_score" and alert.direction == "down" for alert in fixed)
    assert any(alert.metric == "sleep_score" and alert.severity == "major" for alert in adaptive)
    assert any(alert.metric == "hrv_ms" and alert.severity == "major" for alert in hybrid)


def test_correlation_results_are_sorted_and_formatted() -> None:
    records = [
        _record(date(2026, 5, day), sleep_score=day * 10, readiness_score=day * 5)
        for day in range(1, 7)
    ]

    results = compute_correlations(
        records,
        pairs=[("sleep_score", "readiness_score", "Sleep → readiness")],
        min_points=5,
    )

    assert len(results) == 1
    assert results[0].r == pytest.approx(1.0)
    assert results[0].strength == "strong"
    assert "aligns with expectation" in results[0].interpretation
    assert "Sleep → readiness" in format_correlations(results)
    assert format_correlations([]) == "*Not enough overlapping data to compute correlations yet.*"


def test_format_tag_analysis_classifies_good_and_bad_directions() -> None:
    results = [
        {
            "tag": "melatonin",
            "metric": "sleep_score",
            "diff": 5,
            "pct_diff": 6.2,
            "n_with": 4,
            "n_without": 5,
            "mean_with": 86,
            "mean_without": 81,
        },
        {
            "tag": "late caffeine",
            "metric": "avg_hr",
            "diff": 4,
            "pct_diff": 7.0,
            "n_with": 3,
            "n_without": 6,
            "mean_with": 62,
            "mean_without": 58,
        },
    ]

    rendered = format_tag_analysis(results, top_n=2)

    assert "melatonin" in rendered
    assert "better" in rendered
    assert "late caffeine" in rendered
    assert "worse" in rendered
    assert format_tag_analysis([]) == "*Not enough tagged nights to analyze tag effects yet.*"


def test_compute_tag_sleep_analysis_uses_manager_and_filters_by_minimum_counts(
    monkeypatch,
) -> None:
    class FakeManager:
        def get_all_used_tags(self) -> list[str]:
            return ["melatonin", "noise"]

        def compute_tag_metric_comparison(self, records, tag: str, metric: str):
            if tag == "melatonin" and metric == "sleep_score":
                return {"tag": tag, "metric": metric, "n_with": 3, "n_without": 4, "pct_diff": 10}
            if tag == "noise" and metric == "sleep_score":
                return {"tag": tag, "metric": metric, "n_with": 1, "n_without": 8, "pct_diff": 99}
            return None

    monkeypatch.setattr(analytics, "SleepTagManager", FakeManager)

    results = compute_tag_sleep_analysis([_record(date(2026, 5, 20), sleep_score=80)], min_n=3)

    assert results == [
        {"tag": "melatonin", "metric": "sleep_score", "n_with": 3, "n_without": 4, "pct_diff": 10}
    ]


def test_circadian_consistency_handles_missing_invalid_and_wraparound_times() -> None:
    records = [
        _record(date(2026, 5, 18), bedtime="23:50", wake_time="07:30", sleep_duration_hours=7),
        _record(date(2026, 5, 19), bedtime="00:10", wake_time="07:45", sleep_duration_hours=7),
        _record(date(2026, 5, 23), bedtime="bad", wake_time="09:30", sleep_duration_hours=8),
        _record(date(2026, 5, 24), bedtime="23:55", wake_time="09:45", sleep_duration_hours=8),
    ]

    metrics = compute_circadian_consistency(records, window_days=14)

    assert metrics["nights"] == 4
    assert metrics["wake_time_variance_min"] is not None
    assert metrics["avg_weekday_wake"] is not None
    assert metrics["avg_weekend_wake"] is not None
    assert metrics["social_jetlag_min"] is not None
    report = format_circadian_report(metrics)

    assert "quite variable" in report
    assert "Social jetlag: 120 min" in report
    assert "Wake time variability" in report
    assert format_circadian_report({"consistency_score": None}).startswith("*Not enough")


def test_score_label_boundaries() -> None:
    assert sleep_score_label(90) == "great"
    assert sleep_score_label(85) == "good"
    assert sleep_score_label(80) == "decent"
    assert sleep_score_label(79) == "bad"
    assert readiness_label(85) == "optimal"
    assert readiness_label(70) == "good"
    assert readiness_label(60) == "fair"
    assert readiness_label(59) == "pay attention"


def test_format_correlations_orders_by_requested_top_n() -> None:
    results = [
        CorrelationResult("a", "b", "weak", 5, 0.3, "weak", "positive", "weak result"),
        CorrelationResult("c", "d", "strong", 5, 0.9, "strong", "positive", "strong result"),
    ]

    rendered = format_correlations(results, top_n=1)

    assert "weak result" in rendered
    assert "strong result" not in rendered
