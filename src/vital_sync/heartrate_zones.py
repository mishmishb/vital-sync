"""Heart-rate zone time computation.

Mirrors the Oura app's 6-zone model: zones are percentages of an age-based
max-HR estimate. Michael's Oura app shows max HR = 187 (Tanaka: 208 - 0.7*30),
so that is the default here. Zone thresholds at 187:

    Z0 <= 91 (<=49%)    Z1 94-110 (50-59%)   Z2 112-129 (60-69%)
    Z3 131-147 (70-79%) Z4 150-166 (80-89%)  Z5 >= 168 (90-100%)

Sample timestamps are bucketed to Europe/Berlin calendar days to match the
local-day convention used across the rest of the health stack. Intervals longer
than 10 minutes between consecutive samples are treated as non-sampling gaps
(ring off / no monitoring) and excluded — they are NOT extrapolated.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")

ZONE_LABELS = ["Z0", "Z1", "Z2", "Z3", "Z4", "Z5"]
Z2PLUS_ZONES = {"Z2", "Z3", "Z4", "Z5"}

DEFAULT_MAX_HR = 187
DEFAULT_MAX_GAP_SECONDS = 600


def _zone_upper_bounds(max_hr: int) -> dict[str, int]:
    """Oura-style rounded zone ceilings in bpm.

    Oura displays integer bpm ranges: the 60% floor for max_hr=187 is
    112.2 -> 112 bpm. Rounding the percentage-derived ceiling mirrors the
    app's integer ranges (Z2 = 112-129 at max_hr 187).
    """
    return {
        "Z0": round(0.49 * max_hr),
        "Z1": round(0.59 * max_hr),
        "Z2": round(0.69 * max_hr),
        "Z3": round(0.79 * max_hr),
        "Z4": round(0.89 * max_hr),
    }


def sample_zone(bpm: float, max_hr: int = DEFAULT_MAX_HR) -> str:
    """Map a single heart-rate value to an Oura-style zone label."""
    bounds = _zone_upper_bounds(max_hr)
    for label in ("Z0", "Z1", "Z2", "Z3", "Z4"):
        if bpm <= bounds[label]:
            return label
    return "Z5"


def _to_local_day(ts: str) -> str | None:
    """Parse an ISO/UTC timestamp and return the Europe/Berlin calendar date."""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return dt.astimezone(BERLIN).strftime("%Y-%m-%d")


def compute_daily_zone_minutes(
    samples: Iterable[dict[str, Any]],
    max_hr: int = DEFAULT_MAX_HR,
    max_gap_seconds: int = DEFAULT_MAX_GAP_SECONDS,
) -> dict[str, dict[str, float]]:
    """Bucket heart-rate samples into per-day per-zone minutes.

    Args:
        samples: iterable of {"timestamp": ISO string, "bpm": number} dicts.
        max_hr: maximum heart rate used for zone thresholds.
        max_gap_seconds: intervals longer than this are skipped.

    Returns:
        {date_str: {Z0: min, Z1: min, ..., Z5: min, z2plus_min: min}}
        The interval between two consecutive samples is attributed to the zone
        of their midpoint bpm. Days with no valid intervals are absent.
    """
    ordered = sorted(samples, key=lambda s: str(s.get("timestamp", "")))
    if len(ordered) < 2:
        return {}

    result: dict[str, dict[str, float]] = {}

    def _day_bucket(day: str) -> dict[str, float]:
        if day not in result:
            result[day] = {label: 0.0 for label in ZONE_LABELS}
            result[day]["z2plus_min"] = 0.0
        return result[day]

    for i in range(len(ordered) - 1):
        t0 = datetime.fromisoformat(str(ordered[i]["timestamp"]).replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(str(ordered[i + 1]["timestamp"]).replace("Z", "+00:00"))
        gap = (t1 - t0).total_seconds()
        if gap <= 0 or gap > max_gap_seconds:
            continue
        bpm = (float(ordered[i].get("bpm", 0)) + float(ordered[i + 1].get("bpm", 0))) / 2
        zone = sample_zone(bpm, max_hr=max_hr)
        minutes = gap / 60.0
        day = _to_local_day(str(ordered[i]["timestamp"]))
        if day is None:
            continue
        bucket = _day_bucket(day)
        bucket[zone] += minutes
        if zone in Z2PLUS_ZONES:
            bucket["z2plus_min"] += minutes

    return {
        day: bucket
        for day, bucket in result.items()
        if bucket["z2plus_min"] > 0 or any(bucket[z] > 0 for z in ZONE_LABELS)
    }


def weekly_z2plus(zone_data: dict[str, dict[str, float]]) -> dict[str, float]:
    """Aggregate z2plus minutes into Mon-Sun ISO weeks keyed by Monday date.

    Args:
        zone_data: output of compute_daily_zone_minutes.

    Returns:
        {monday_date_str: total z2plus minutes for that ISO week}
    """
    weeks: dict[str, float] = defaultdict(float)
    for day, stats in zone_data.items():
        try:
            dt = datetime.strptime(day, "%Y-%m-%d").date()
        except ValueError:
            continue
        monday = (dt - timedelta(days=dt.weekday())).isoformat()
        weeks[monday] += float(stats.get("z2plus_min", 0.0))
    return dict(weeks)
