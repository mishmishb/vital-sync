"""Characterisation tests for wake-date record merging."""

from datetime import date

import pytest

from vital_sync.analytics import DailyRecord, _merge_wake_date_records


def _record(day: date, **kwargs) -> DailyRecord:
    return DailyRecord(date=day, **kwargs)


def test_empty_input_returns_empty_list() -> None:
    assert _merge_wake_date_records([]) == []


def test_single_record_is_preserved_without_copying() -> None:
    record = _record(date(2026, 5, 20), sleep_score=82, sources=["oura"])

    merged = _merge_wake_date_records([record])

    assert merged == [record]
    assert merged[0] is record


def test_multiple_records_on_same_wake_date_are_merged() -> None:
    day = date(2026, 5, 20)
    first = _record(
        day,
        sleep_duration_hours=2.0,
        deep_sleep_min=25,
        rem_sleep_min=15,
        bedtime="23:10:00",
        wake_time="02:00:00",
        sleep_score=55,
        avg_hr=60,
        hrv_ms=30,
        lowest_hr=52,
        sources=["oura"],
    )
    longest = _record(
        day,
        sleep_duration_hours=5.0,
        deep_sleep_min=55,
        rem_sleep_min=75,
        bedtime="02:15:00",
        wake_time="08:30:00",
        sleep_score=88,
        avg_hr=54,
        hrv_ms=45,
        lowest_hr=47,
        sources=["manual", "oura"],
    )

    merged = _merge_wake_date_records([longest, first])

    assert len(merged) == 1
    record = merged[0]
    assert record.date == day
    assert record.sleep_duration_hours == 7.0
    assert record.deep_sleep_min == 80
    assert record.rem_sleep_min == 90
    assert record.bedtime == "02:15:00"
    assert record.wake_time == "08:30:00"
    assert record.sleep_score == 88
    assert record.avg_hr == pytest.approx(((5.0 * 54) + (2.0 * 60)) / 7.0)
    assert record.hrv_ms == pytest.approx(((5.0 * 45) + (2.0 * 30)) / 7.0)
    assert record.lowest_hr == 47
    assert record.sources == ["manual", "oura"]


def test_merge_sums_sleep_metrics_while_ignoring_none_values() -> None:
    day = date(2026, 5, 21)

    merged = _merge_wake_date_records(
        [
            _record(day, sleep_duration_hours=None, deep_sleep_min=10, rem_sleep_min=None),
            _record(day, sleep_duration_hours=3.5, deep_sleep_min=None, rem_sleep_min=40),
            _record(day, sleep_duration_hours=1.0, deep_sleep_min=5, rem_sleep_min=10),
        ]
    )

    record = merged[0]
    assert record.sleep_duration_hours == 4.5
    assert record.deep_sleep_min == 15
    assert record.rem_sleep_min == 50


def test_merge_keeps_none_when_all_summed_metrics_are_missing() -> None:
    day = date(2026, 5, 22)

    record = _merge_wake_date_records([_record(day), _record(day)])[0]

    assert record.sleep_duration_hours is None
    assert record.deep_sleep_min is None
    assert record.rem_sleep_min is None


def test_merge_copies_first_non_null_daily_values() -> None:
    day = date(2026, 5, 23)
    first = _record(day, readiness_score=None, activity_score=71, sleep_efficiency=None)
    second = _record(
        day,
        readiness_score=78,
        activity_score=75,
        spo2=97.4,
        steps=9100,
        stress_summary="normal",
        sleep_efficiency=88,
    )

    record = _merge_wake_date_records([first, second])[0]

    assert record.readiness_score == 78
    assert record.activity_score == 71
    assert record.spo2 == 97.4
    assert record.steps == 9100
    assert record.stress_summary == "normal"
    assert record.sleep_efficiency == 88


def test_merge_uses_first_scored_segment_when_scored_durations_are_missing() -> None:
    day = date(2026, 5, 24)

    record = _merge_wake_date_records(
        [
            _record(day, sleep_score=81, sleep_duration_hours=None),
            _record(day, sleep_score=75, sleep_duration_hours=None),
        ]
    )[0]

    assert record.sleep_score == 81


def test_merge_defaults_sources_to_oura_when_segments_have_no_sources() -> None:
    day = date(2026, 5, 25)

    record = _merge_wake_date_records([_record(day), _record(day)])[0]

    assert record.sources == ["oura"]


def test_merge_keeps_multiple_dates_separate_and_sorted() -> None:
    older = _record(date(2026, 5, 20), sleep_score=80)
    newest_a = _record(date(2026, 5, 22), sleep_score=82, sleep_duration_hours=2)
    newest_b = _record(date(2026, 5, 22), sleep_score=90, sleep_duration_hours=7)
    middle = _record(date(2026, 5, 21), sleep_score=81)

    merged = _merge_wake_date_records([newest_a, older, newest_b, middle])

    assert [record.date for record in merged] == [
        date(2026, 5, 20),
        date(2026, 5, 21),
        date(2026, 5, 22),
    ]
    assert merged[-1].sleep_score == 90


def test_merge_invariants_for_none_heavy_segments() -> None:
    dates = [date(2026, 5, 26), date(2026, 5, 27)]
    records = [
        _record(dates[1], sleep_duration_hours=4, sources=["hevy"]),
        _record(dates[0], sleep_duration_hours=None, avg_hr=52, sources=[]),
        _record(dates[1], sleep_duration_hours=2, avg_hr=58, sources=["oura"]),
        _record(dates[0], sleep_duration_hours=1, avg_hr=48, sources=["oura"]),
    ]

    merged = _merge_wake_date_records(records)

    assert [record.date for record in merged] == sorted(set(dates))
    assert len({record.date for record in merged}) == len(merged)
    assert merged[0].sleep_duration_hours == 1
    assert merged[0].avg_hr == 48
    assert merged[1].sleep_duration_hours == 6
    assert 52 <= merged[1].avg_hr <= 58
    assert set(merged[1].sources) == {"hevy", "oura"}
