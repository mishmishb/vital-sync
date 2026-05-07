"""
Test fixtures for vital-sync analytics tests.

Provides:
- sample_records: list of DailyRecord objects with known values
- sample_records_dicts: same data as plain dicts (simulating cache load)
- oura_mcp_output: mock Oura MCP query_metrics result
- Known data for baseline computation tests
"""

from datetime import date, timedelta

import pytest

from vital_sync.analytics import DailyRecord

# ── Helper to create a DailyRecord quickly ───────────────────────────────────


def make_record(
    d: date,
    sleep_score: float | None = None,
    readiness_score: float | None = None,
    activity_score: float | None = None,
    sleep_duration_hours: float | None = None,
    sleep_efficiency: float | None = None,
    deep_sleep_min: float | None = None,
    rem_sleep_min: float | None = None,
    avg_hr: float | None = None,
    lowest_hr: float | None = None,
    hrv_ms: float | None = None,
    steps: int | None = None,
    spo2: float | None = None,
    body_fat_pct: float | None = None,
    weight_kg: float | None = None,
    muscle_mass_kg: float | None = None,
    calories_in: float | None = None,
    protein_g: float | None = None,
    mood: float | None = None,
    anxiety: float | None = None,
    irritability: float | None = None,
    bedtime: str | None = None,
    wake_time: str | None = None,
    sources: list[str] | None = None,
    sleep_tags: list[str] | None = None,
    **kwargs,
) -> DailyRecord:
    """Create a DailyRecord with minimal boilerplate."""
    r = DailyRecord(date=d)
    if sleep_score is not None:
        r.sleep_score = sleep_score
    if readiness_score is not None:
        r.readiness_score = readiness_score
    if activity_score is not None:
        r.activity_score = activity_score
    if sleep_duration_hours is not None:
        r.sleep_duration_hours = sleep_duration_hours
    if sleep_efficiency is not None:
        r.sleep_efficiency = sleep_efficiency
    if deep_sleep_min is not None:
        r.deep_sleep_min = deep_sleep_min
    if rem_sleep_min is not None:
        r.rem_sleep_min = rem_sleep_min
    if avg_hr is not None:
        r.avg_hr = avg_hr
    if lowest_hr is not None:
        r.lowest_hr = lowest_hr
    if hrv_ms is not None:
        r.hrv_ms = hrv_ms
    if steps is not None:
        r.steps = steps
    if spo2 is not None:
        r.spo2 = spo2
    if body_fat_pct is not None:
        r.body_fat_pct = body_fat_pct
    if weight_kg is not None:
        r.weight_kg = weight_kg
    if muscle_mass_kg is not None:
        r.muscle_mass_kg = muscle_mass_kg
    if calories_in is not None:
        r.calories_in = calories_in
    if protein_g is not None:
        r.protein_g = protein_g
    if mood is not None:
        r.mood = mood
    if anxiety is not None:
        r.anxiety = anxiety
    if irritability is not None:
        r.irritability = irritability
    if bedtime is not None:
        r.bedtime = bedtime
    if wake_time is not None:
        r.wake_time = wake_time
    if sources is not None:
        r.sources = sources
    if sleep_tags is not None:
        r.sleep_tags = sleep_tags
    for k, v in kwargs.items():
        if hasattr(r, k):
            setattr(r, k, v)
    return r


def record_to_dict(r: DailyRecord) -> dict:
    """Convert a DailyRecord to a plain dict (simulating cache JSON)."""
    return r.to_dict()


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def today_():
    """Return a fixed 'today' for deterministic tests."""
    return date(2026, 4, 26)


@pytest.fixture
def sample_records(today_):
    """
    10-day series of DailyRecord objects with known values.
    Days: April 17-26, 2026.
    sleep_scores: gradually declining with one outlier for deviation detection.
    """
    base = today_
    records = []
    sleep_scores = [88, 87, 86, 85, 84, 82, 80, 79, 65, 62]  # sharp drop at end
    hrv_values = [48, 47, 46, 45, 44, 42, 41, 40, 35, 30]
    steps_values = [12000, 11500, 11000, 10500, 10000, 9500, 9000, 8500, 8000, 7500]

    for i in range(10):
        d = base - timedelta(days=9 - i)
        r = make_record(
            d=d,
            sleep_score=sleep_scores[i],
            readiness_score=80 - i,
            activity_score=95 - i,
            sleep_duration_hours=7.5 - i * 0.15,
            sleep_efficiency=92 - i * 0.5,
            deep_sleep_min=120 - i * 3,
            rem_sleep_min=100 - i * 2,
            avg_hr=55 + i,
            lowest_hr=50 + i,
            hrv_ms=hrv_values[i],
            steps=steps_values[i],
            spo2=97.5 - i * 0.05,
        )
        records.append(r)
    return records


@pytest.fixture
def sample_records_dicts(sample_records):
    """Same data as sample_records but as plain dicts (simulating cache.json)."""
    return [record_to_dict(r) for r in sample_records]


@pytest.fixture
def flat_sleep_records(today_):
    """Records with stable sleep values — should NOT trigger deviations."""
    base = today_
    records = []
    for i in range(14):
        d = base - timedelta(days=13 - i)
        records.append(
            make_record(
                d=d,
                sleep_score=85,
                readiness_score=80,
                sleep_duration_hours=7.5,
                sleep_efficiency=92,
                hrv_ms=45,
                steps=10000,
                spo2=98.0,
            )
        )
    return records


@pytest.fixture
def single_record():
    """Just one record — edge case for baseline."""
    return [make_record(d=date(2026, 4, 26), sleep_score=85, hrv_ms=45)]


@pytest.fixture
def empty_records():
    """Empty record list."""
    return []


@pytest.fixture
def all_none_records(today_):
    """Records where all metrics are None."""
    records = []
    for i in range(10):
        d = today_ - timedelta(days=9 - i)
        records.append(make_record(d=d))  # no metrics set
    return records


@pytest.fixture
def oura_mcp_output():
    """Simulate an Oura MCP query_metrics result for one night.

    NOTE: The metric regex requires trailing text after values
    (e.g., unit descriptions). Oura MCP output includes these.
    """
    return {
        "result": (
            "2026-04-25:\n"
            "daily_sleep_score: 85 score\n"
            "daily_readiness_score: 82 score\n"
            "daily_activity_score: 95 score\n"
            "daily_spo2_spo2_percentage: 97.5 %\n"
            "daily_activity_steps: 11000 steps\n"
            "daily_stress_day_summary: normal\n"
            "sleep_total_sleep_duration: 27000 seconds\n"
            "sleep_deep_sleep_duration: 5400 seconds\n"
            "sleep_rem_sleep_duration: 7200 seconds\n"
            "sleep_efficiency: 92 %\n"
            "sleep_bedtime_start: 2026-04-24T23:30:00Z\n"
            "sleep_bedtime_end: 2026-04-25T07:00:00Z\n"
            "sleep_average_heart_rate: 55 bpm\n"
            "sleep_lowest_heart_rate: 48 bpm\n"
            "sleep_average_hrv: 42 ms\n"
        )
    }


@pytest.fixture
def oura_mcp_output_short_sleep():
    """Oura MCP output with a 14-minute (840s) sleep artifact."""
    return {
        "result": (
            "2026-04-25:\n"
            "daily_sleep_score: 30 score\n"
            "sleep_total_sleep_duration: 840 seconds\n"  # 14 minutes — not a real night
            "sleep_bedtime_start: 2026-04-24T23:30:00Z\n"
            "sleep_bedtime_end: 2026-04-24T23:44:00Z\n"
        )
    }


@pytest.fixture
def oura_mcp_output_multiple_durations() -> dict:
    """Simulate real Oura bug: multiple sleep durations per date.

    Oura API returns both a short period (nap/artifact) and the main sleep.
    The parser must select the LONGEST duration, not the last one.
    Bedtime/deep/REM metrics are paired with the real (long) segment.
    """
    return {
        "result": (
            "2026-04-24:\n"
            "daily_sleep_score: 84 score\n"
            # Real sleep (7.5h) — with full metrics
            "sleep_total_sleep_duration: 27000 seconds\n"
            "sleep_deep_sleep_duration: 5400 seconds\n"
            "sleep_rem_sleep_duration: 7200 seconds\n"
            "sleep_efficiency: 92 %\n"
            "sleep_bedtime_start: 2026-04-23T23:30:00Z\n"
            "sleep_bedtime_end: 2026-04-24T07:00:00Z\n"
            "sleep_average_heart_rate: 55 bpm\n"
            "sleep_lowest_heart_rate: 48 bpm\n"
            "sleep_average_hrv: 42 ms\n"
            # Artifact (14min) — no extra metrics
            "sleep_total_sleep_duration: 840 seconds\n"
            "sleep_bedtime_start: 2026-04-25T01:11:00Z\n"
            "sleep_bedtime_end: 2026-04-25T01:39:00Z\n"
        )
    }


@pytest.fixture
def oura_mcp_output_multi_day():
    """Oura MCP output spanning multiple days."""
    return {
        "result": (
            "2026-04-23:\n"
            "daily_sleep_score: 88 score\n"
            "sleep_total_sleep_duration: 28800 seconds\n"
            "2026-04-24:\n"
            "daily_sleep_score: 82 score\n"
            "sleep_total_sleep_duration: 25200 seconds\n"
            "2026-04-25:\n"
            "daily_sleep_score: 85 score\n"
            "sleep_total_sleep_duration: 27000 seconds\n"
        )
    }


@pytest.fixture
def oura_mcp_output_multi_segment_cross_midnight():
    """Real Oura MCP output where segments cross midnight date boundaries.

    The MCP labels sleep by onset date, but Oura's app uses wake date.
    This fixture reproduces the exact April 22-24 data from Michael's app:
    - MCP labels a segment under Apr 22, but it wakes Apr 23 → belongs to Apr 23
    - MCP labels a segment under Apr 23, but it wakes Apr 24 → belongs to Apr 24
    """
    return {
        "result": (
            "2026-04-23:\n"
            "daily_sleep_score: 77 score\n"
            "daily_activity_steps: 7239 count\n"
            "sleep_bedtime_end: 2026-04-24T09:00:33.000+02:00\n"
            "sleep_bedtime_start: 2026-04-24T00:26:31.000+02:00\n"
            "sleep_total_sleep_duration: 27300 seconds\n"  # 7.58h — wakes Apr 24!
            "2026-04-22:\n"
            "daily_sleep_score: 80 score\n"
            "daily_activity_steps: 5938 count\n"
            "sleep_bedtime_end: 2026-04-23T08:32:34.000+02:00\n"
            "sleep_bedtime_start: 2026-04-22T23:43:28.000+02:00\n"
            "sleep_total_sleep_duration: 26340 seconds\n"  # 7.32h — wakes Apr 23!
            "sleep_bedtime_end: 2026-04-22T10:05:22.000+02:00\n"
            "sleep_bedtime_start: 2026-04-22T02:52:27.000+02:00\n"
            "sleep_total_sleep_duration: 23070 seconds\n"  # 6.41h — wakes Apr 22!
        )
    }
