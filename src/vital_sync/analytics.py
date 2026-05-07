"""
Health data analytics module for Oura Ring, Renpho scale, and MyNetDiary data.

Provides:
- Rolling statistics (mean, median, std, min, max, trend)
- Deviation detection with configurable thresholds
- Time-series trend analysis
- Data merging from multiple sources
"""

from __future__ import annotations

import csv
import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

# ── Thresholds for deviation detection ───────────────────────────────────────

# (metric_name, relative_threshold, absolute_threshold, direction)
# relative: fraction of baseline change to trigger
# absolute: raw value change to trigger
# direction: "either", "up", "down"
DEVIATION_RULES: dict[str, tuple[float, float | None, str]] = {
    "sleep_score": (0.08, 5.0, "either"),
    "readiness_score": (0.08, 5.0, "either"),
    "activity_score": (0.08, 5.0, "either"),
    "sleep_duration_hours": (0.10, 0.5, "either"),
    "sleep_efficiency": (0.05, 4.0, "either"),
    "deep_sleep_min": (0.15, 15.0, "either"),
    "rem_sleep_min": (0.15, 15.0, "either"),
    "avg_hr": (0.08, 4.0, "either"),
    "lowest_hr": (0.08, 3.0, "either"),
    "hrv_ms": (0.15, 5.0, "either"),
    "steps": (0.20, 1500.0, "either"),
    "spo2": (0.01, 1.0, "down"),  # only alert on drop
    "body_fat_pct": (0.05, 1.0, "either"),
    "weight_kg": (0.02, 1.0, "either"),
    "muscle_mass_kg": (0.03, 1.0, "either"),
    "calories_in": (0.20, 300.0, "either"),
    "protein_g": (0.20, 20.0, "either"),
}


# ── Data structures ──────────────────────────────────────────────────────────


@dataclass
class DailyRecord:
    date: date
    sleep_score: float | None = None
    readiness_score: float | None = None
    activity_score: float | None = None
    sleep_duration_hours: float | None = None
    sleep_efficiency: float | None = None
    deep_sleep_min: float | None = None
    rem_sleep_min: float | None = None
    avg_hr: float | None = None
    lowest_hr: float | None = None
    hrv_ms: float | None = None
    steps: int | None = None
    stress_summary: str | None = None
    spo2: float | None = None
    body_fat_pct: float | None = None
    weight_kg: float | None = None
    muscle_mass_kg: float | None = None
    calories_in: float | None = None
    protein_g: float | None = None
    mood: float | None = None
    anxiety: float | None = None
    irritability: float | None = None
    task_init: float | None = None
    task_switch: float | None = None
    stop_working: float | None = None
    social_patience: float | None = None
    appetite: float | None = None
    evening_crash: str | None = None
    bp_sys: int | None = None
    bp_dia: int | None = None
    morning_grogginess: float | None = None  # 1-10 scale
    bedtime: str | None = None  # ISO time string "HH:MM"
    wake_time: str | None = None  # ISO time string "HH:MM"
    # ── Hevy workout fields ──────────────────────────────────────────────────────────
    hevy_workouts: int | None = None  # number of workouts that day
    hevy_total_volume_kg: float | None = None
    hevy_total_duration_min: float | None = None
    hevy_muscle_groups: list[str] = field(default_factory=list)  # trained muscles that day
    hevy_max_weight_kg: float | None = None  # heaviest lift of the day
    hevy_avg_rpe: float | None = None  # average RPE across all sets
    notes: str = ""
    sources: list[str] = field(default_factory=list)
    sleep_tags: list[str] = field(default_factory=list)
    negated_baseline_tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = {}
        for k, v in self.__dict__.items():
            if isinstance(v, date):
                d[k] = v.isoformat()
            else:
                d[k] = v
        return d


@dataclass
class BaselineStats:
    metric: str
    n: int
    mean: float
    median: float
    std: float
    min: float
    max: float
    latest: float | None
    trend: float | None  # simple linear slope over window
    days_since_change: int | None


@dataclass
class DeviationAlert:
    metric: str
    date: date
    value: float
    baseline_mean: float | None
    baseline_std: float | None
    severity: str  # "minor", "moderate", "major"
    direction: str  # "up", "down"
    message: str


# ── Core math utilities ──────────────────────────────────────────────────────


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _median(values: Sequence[float]) -> float:
    s = sorted(values)
    n = len(s)
    if n % 2:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / 2


def _std(values: Sequence[float]) -> float:
    m = _mean(values)
    variance = sum((x - m) ** 2 for x in values) / len(values)
    return math.sqrt(variance)


def _linear_slope(values: Sequence[float]) -> float | None:
    """Simple least-squares slope over a sequence (days 0..n-1)."""
    n = len(values)
    if n < 3:
        return None
    x_mean = (n - 1) / 2
    y_mean = _mean(values)
    num = sum((i - x_mean) * (v - y_mean) for i, v in enumerate(values))
    den = sum((i - x_mean) ** 2 for i in range(n))
    if den == 0:
        return None
    return num / den


def _record_date(r: Any) -> date | None:
    """Extract a date from a DailyRecord or dict."""
    if isinstance(r, dict):
        d = r.get("date")
        if isinstance(d, date):
            return d
        if isinstance(d, str):
            try:
                return date.fromisoformat(d)
            except ValueError:
                try:
                    return datetime.strptime(d, "%m/%d/%Y").date()
                except ValueError:
                    return None
        return None
    return r.date


# ── Oura MCP data conversion ─────────────────────────────────────────────────


def oura_mcp_to_records(mcp_result: dict[str, Any]) -> list[DailyRecord]:
    """Convert Oura MCP query_metrics result into DailyRecord list.

    Handles:
    - Multiple sleep SEGMENTS per MCP date label (detected by repeated anchor keys)
    - Wake-date reassignment: Oura's app labels sleep by the day you wake up.
      The MCP labels by sleep onset date. This corrects to wake-date convention.
    - Segment merging: multiple segments waking on the same date are merged,
      summing durations and using the earliest bedtime / latest wake time.
    """
    text = mcp_result.get("result", "")
    if not text:
        return []

    # Step 1: Parse raw segments with their MCP label dates
    raw_segments, daily_metrics = _parse_segments(text)

    # Step 2: Build DailyRecord per segment, assign wake date
    records: list[DailyRecord] = []
    for seg in raw_segments:
        wake_date = _wake_date_from_segment(seg)
        r = _build_record(wake_date, seg)
        records.append(r)

    # Step 3: Fill any remaining daily metrics not yet assigned to a record
    _DAILY_TO_ATTR = {
        "daily_sleep_score": "sleep_score",
        "daily_readiness_score": "readiness_score",
        "daily_activity_score": "activity_score",
        "daily_spo2_spo2_percentage": "spo2",
        "daily_activity_steps": "steps",
        "daily_stress_day_summary": "stress_summary",
    }
    for mcp_date_str, daily in daily_metrics.items():
        mcp_date = date.fromisoformat(mcp_date_str)
        # Find a record with this wake date that's missing daily scores
        for r in records:
            if r.date == mcp_date:
                for mcp_key, val in daily.items():
                    attr = _DAILY_TO_ATTR.get(mcp_key, mcp_key)
                    if getattr(r, attr, None) is None and val is not None:
                        # Special cast: steps must be int
                        if attr == "steps" and isinstance(val, float):
                            val = int(val)
                        setattr(r, attr, val)
                break
        else:
            # No record has this wake date — create one so we don't lose the score
            if daily:
                r = DailyRecord(date=mcp_date)
                for mcp_key, val in daily.items():
                    attr = _DAILY_TO_ATTR.get(mcp_key, mcp_key)
                    if attr == "steps" and isinstance(val, float):
                        val = int(val)
                    setattr(r, attr, val)
                r.sources = ["oura"]
                records.append(r)

    # Step 4: Merge records with the same date
    return _merge_wake_date_records(records)


def _parse_segments(text: str) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Parse Oura MCP text into per-segment dicts and per-date daily metrics.

    Returns:
        segments: list of per-sleep-period data dicts
        daily_metrics: {mcp_date_str: {attr_name: value}} — daily scores/activity
                       that should be applied to the record whose wake_date matches

    Detects segment boundaries when any sleep_* metric repeats within the same
    MCP date label. Daily metrics (daily_*) are tracked separately and not
    carried with reassigned segments.
    """
    segments: list[dict[str, Any]] = []
    daily_metrics: dict[str, dict[str, Any]] = {}
    current_segment: dict[str, Any] = {}
    current_mcp_date: date | None = None
    current_daily: dict[str, Any] = {}
    # All sleep-specific metrics trigger segment boundaries when repeated
    _SEGMENT_ANCHOR_PREFIXES = ("sleep_",)

    def _flush_segment():
        nonlocal current_segment
        if current_segment:
            if current_mcp_date:
                current_segment["_mcp_label_date"] = current_mcp_date.isoformat()
            segments.append(current_segment)
            current_segment = {}

    def _flush_date():
        nonlocal current_daily
        if current_mcp_date and current_daily:
            daily_metrics[current_mcp_date.isoformat()] = current_daily
            current_daily = {}

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue

        # Date header: "2026-04-22:" or "2026-04-22T...:"
        date_match = re.match(r"(\d{4}-\d{2}-\d{2})(?:T\d{2}:\d{2})?:", line)
        if date_match:
            _flush_segment()
            _flush_date()
            current_mcp_date = date.fromisoformat(date_match.group(1))
            continue

        # Metric line: "key: value [unit]"
        metric_match = re.match(r"([\w_]+):\s+(.+)", line)
        if metric_match and current_mcp_date is not None:
            key = metric_match.group(1)
            val_str = metric_match.group(2).strip()

            # Parse value: try numeric, fallback to string
            # BUT: preserve ISO datetime strings (sleep_bedtime_start, sleep_bedtime_end)
            # which would otherwise be falsely parsed as numbers (e.g. "2026" from "2026-04-24T...")
            _ISO_DT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")
            is_iso_dt = bool(_ISO_DT_RE.match(val_str))
            if not is_iso_dt:
                num_match = re.match(r"^([\d.]+)(?:\s+.*)?$", val_str)
                if num_match:
                    try:
                        val = float(num_match.group(1))
                    except ValueError:
                        val = val_str
                else:
                    val = val_str
            else:
                val = val_str

            # Daily metrics (daily_*) are per-date, tracked separately
            if key.startswith("daily_"):
                current_daily[key] = val
            else:
                # Detect segment boundary: any repeated sleep_* metric
                if key.startswith(_SEGMENT_ANCHOR_PREFIXES) and key in current_segment:
                    _flush_segment()
                current_segment[key] = val

    _flush_segment()
    _flush_date()
    return segments, daily_metrics


def _wake_date_from_segment(seg: dict[str, Any]) -> date:
    """Extract the wake date from a segment's bedtime_end.

    Oura convention: sleep is attributed to the day you WAKE UP.
    Uses bedtime_end (ISO datetime) to determine the calendar date.
    Falls back to the MCP label date if bedtime_end is unavailable.
    """
    bedtime_end = seg.get("sleep_bedtime_end")
    if bedtime_end and isinstance(bedtime_end, str):
        # Strip trailing unit annotation e.g. " (OURA)"
        bedtime_end = re.sub(r"\s*\(.*\)\s*$", "", bedtime_end)
        try:
            dt = datetime.fromisoformat(bedtime_end.replace("Z", "+00:00"))
            return dt.date()
        except (ValueError, TypeError):
            pass
    # Fallback: use the MCP label date
    mcp_date_str = seg.get("_mcp_label_date")
    if mcp_date_str:
        return date.fromisoformat(mcp_date_str)
    return date.today()


def _merge_wake_date_records(records: list[DailyRecord]) -> list[DailyRecord]:
    """Merge DailyRecords that share the same wake date.

    When multiple sleep segments end on the same calendar day (e.g., woke up
    briefly then went back to sleep), they're merged into one record:
    - Sleep duration: summed
    - Deep/REM: summed (if available)
    - Bedtime: earliest
    - Wake time: latest
    - Heart rate / HRV: weighted average by duration (or first available)
    - Sleep score: from the longest segment (or first with a score)
    """
    if not records:
        return records

    from collections import defaultdict

    by_date: dict[date, list[DailyRecord]] = defaultdict(list)
    for r in records:
        by_date[r.date].append(r)

    merged: list[DailyRecord] = []
    for d, group in sorted(by_date.items()):
        if len(group) == 1:
            merged.append(group[0])
            continue

        # Multiple segments on same wake date — merge them
        base = DailyRecord(date=d)
        # Sum durations
        base.sleep_duration_hours = (
            sum(r.sleep_duration_hours for r in group if r.sleep_duration_hours is not None)
            or None
        )
        base.deep_sleep_min = (
            sum(r.deep_sleep_min for r in group if r.deep_sleep_min is not None) or None
        )
        base.rem_sleep_min = (
            sum(r.rem_sleep_min for r in group if r.rem_sleep_min is not None) or None
        )

        # Earliest bedtime, latest wake time
        bedtimes = [r.bedtime for r in group if r.bedtime]
        waketimes = [r.wake_time for r in group if r.wake_time]
        base.bedtime = min(bedtimes) if bedtimes else None
        base.wake_time = max(waketimes) if waketimes else None

        # Score: from the segment with the longest duration
        scored = [r for r in group if r.sleep_score is not None]
        if scored:
            longest = max(scored, key=lambda r: r.sleep_duration_hours or 0)
            base.sleep_score = longest.sleep_score

        # Readiness / activity: from first segment that has them
        for attr in (
            "readiness_score",
            "activity_score",
            "spo2",
            "steps",
            "stress_summary",
            "sleep_efficiency",
        ):
            for r in group:
                val = getattr(r, attr)
                if val is not None:
                    setattr(base, attr, val)
                    break

        # HR / HRV: weighted average by duration
        hrs = [
            (r.avg_hr, r.sleep_duration_hours)
            for r in group
            if r.avg_hr is not None and r.sleep_duration_hours
        ]
        if hrs:
            total_dur = sum(d for _, d in hrs)
            base.avg_hr = sum(h * d for h, d in hrs) / total_dur if total_dur > 0 else hrs[0][0]

        hrvs = [
            (r.hrv_ms, r.sleep_duration_hours)
            for r in group
            if r.hrv_ms is not None and r.sleep_duration_hours
        ]
        if hrvs:
            total_dur = sum(d for _, d in hrvs)
            base.hrv_ms = sum(h * d for h, d in hrvs) / total_dur if total_dur > 0 else hrvs[0][0]

        lowest_hrs = [r.lowest_hr for r in group if r.lowest_hr is not None]
        base.lowest_hr = min(lowest_hrs) if lowest_hrs else None

        # Sources
        all_sources = set()
        for r in group:
            if r.sources:
                all_sources.update(r.sources)
        base.sources = sorted(all_sources) if all_sources else ["oura"]

        merged.append(base)

    return merged


def _build_record(d: date, data: dict[str, Any]) -> DailyRecord:
    r = DailyRecord(date=d)
    r.sleep_score = data.get("daily_sleep_score")
    r.readiness_score = data.get("daily_readiness_score")
    r.activity_score = data.get("daily_activity_score")
    r.spo2 = data.get("daily_spo2_spo2_percentage")
    r.steps = int(data["daily_activity_steps"]) if "daily_activity_steps" in data else None
    r.stress_summary = data.get("daily_stress_day_summary")

    if "sleep_total_sleep_duration" in data:
        r.sleep_duration_hours = data["sleep_total_sleep_duration"] / 3600
    if "sleep_deep_sleep_duration" in data:
        r.deep_sleep_min = data["sleep_deep_sleep_duration"] / 60
    if "sleep_rem_sleep_duration" in data:
        r.rem_sleep_min = data["sleep_rem_sleep_duration"] / 60
    if "sleep_efficiency" in data:
        r.sleep_efficiency = data["sleep_efficiency"]

    # Capture sleep timing for circadian analysis
    if "sleep_bedtime_start" in data:
        try:
            bt_str = re.sub(r"\s*\(.*\)\s*$", "", str(data["sleep_bedtime_start"]))
            bedtime = datetime.fromisoformat(bt_str.replace("Z", "+00:00"))
            r.bedtime = bedtime.time().isoformat()
        except (ValueError, TypeError):
            pass
    if "sleep_bedtime_end" in data:
        try:
            we_str = re.sub(r"\s*\(.*\)\s*$", "", str(data["sleep_bedtime_end"]))
            wake_time = datetime.fromisoformat(we_str.replace("Z", "+00:00"))
            r.wake_time = wake_time.time().isoformat()
        except (ValueError, TypeError):
            pass

    r.avg_hr = data.get("sleep_average_heart_rate")
    r.lowest_hr = data.get("sleep_lowest_heart_rate")
    r.hrv_ms = data.get("sleep_average_hrv")

    # Filter out sleep artifacts (< 1 hour / 3600s)
    # Only null sleep-derived fields. daily_sleep_score is independently reported
    # by Oura and reflects the previous night's quality, not the artifact.
    if r.sleep_duration_hours is not None and r.sleep_duration_hours < 1.0:
        r.sleep_duration_hours = None
        r.deep_sleep_min = None
        r.rem_sleep_min = None
        r.sleep_efficiency = None
        r.bedtime = None
        r.wake_time = None
        r.avg_hr = None
        r.lowest_hr = None
        r.hrv_ms = None

    r.sources = ["oura"]
    return r


# ── Statistics computation ───────────────────────────────────────────────────


def compute_baseline(
    records: Sequence[DailyRecord],
    metric: str,
    window_days: int = 7,
    end_date: date | None = None,
    min_date: date | None = None,
) -> BaselineStats | None:
    """Compute baseline stats for a metric over the last `window_days`."""
    if end_date is None:
        dates = [_record_date(r) for r in records]
        dates = [d for d in dates if d is not None]
        end_date = max(dates, default=date.today())
    start_date = end_date - timedelta(days=window_days - 1)

    values = []
    for r in records:
        if r is None:
            continue
        r_date = _record_date(r)
        if r_date is None:
            continue
        if min_date is not None and r_date < min_date:
            continue
        if start_date <= r_date <= end_date:
            if isinstance(r, dict):
                v = r.get(metric)
            else:
                v = getattr(r, metric, None)
            if v is not None:
                values.append(float(v))

    if not values:
        return None

    trend_values = values[-7:]
    trend = _linear_slope(trend_values)

    m = _mean(values)
    s = _std(values)
    days_since = None
    for i, v in enumerate(reversed(values)):
        if abs(v - m) <= 0.5 * s:
            days_since = i
            break

    return BaselineStats(
        metric=metric,
        n=len(values),
        mean=round(m, 2),
        median=round(_median(values), 2),
        std=round(s, 2),
        min=round(min(values), 2),
        max=round(max(values), 2),
        latest=round(values[-1], 2) if values else None,
        trend=round(trend, 4) if trend is not None else None,
        days_since_change=days_since,
    )


def compute_all_baselines(
    records: Sequence[DailyRecord],
    metrics: Sequence[str] | None = None,
    window_days: int = 7,
    end_date: date | None = None,
    min_date: date | None = None,
) -> dict[str, BaselineStats]:
    """Compute baselines for all metrics with available data."""
    if metrics is None:
        metrics = [
            "sleep_score",
            "readiness_score",
            "activity_score",
            "sleep_duration_hours",
            "sleep_efficiency",
            "deep_sleep_min",
            "rem_sleep_min",
            "avg_hr",
            "lowest_hr",
            "hrv_ms",
            "steps",
            "spo2",
            "body_fat_pct",
            "weight_kg",
            "muscle_mass_kg",
            "calories_in",
            "protein_g",
            "mood",
            "anxiety",
            "irritability",
            "task_init",
            "task_switch",
            "stop_working",
            "social_patience",
            "appetite",
        ]
    result = {}
    for m in metrics:
        stats = compute_baseline(records, m, window_days, end_date, min_date)
        if stats:
            result[m] = stats
    return result


# ── Deviation detection ──────────────────────────────────────────────────────


def detect_deviations(
    records: Sequence[DailyRecord],
    baselines: dict[str, BaselineStats] | None = None,
    window_days: int = 7,
    min_data_points: int = 3,
) -> list[DeviationAlert]:
    """Detect significant deviations from baseline in the most recent records."""
    if not records:
        return []

    if baselines is None:
        baselines = compute_all_baselines(records, window_days=window_days)

    alerts: list[DeviationAlert] = []
    latest = max(_record_date(r) for r in records if _record_date(r) is not None)  # pyright: ignore[reportArgumentType]

    for metric, rule in DEVIATION_RULES.items():
        rel_thresh, abs_thresh, direction = rule
        baseline = baselines.get(metric)
        if baseline is None or baseline.n < min_data_points:
            continue

        latest_value = None
        for r in reversed(records):
            r_date = _record_date(r)
            if r_date is None:
                continue
            if r_date == latest:
                if isinstance(r, dict):
                    v = r.get(metric)
                else:
                    v = getattr(r, metric, None)
                if v is not None:
                    latest_value = float(v)
                    break

        if latest_value is None:
            continue

        mean = baseline.mean
        std = baseline.std
        diff = latest_value - mean
        rel_change = abs(diff / mean) if mean != 0 else 0
        abs_change = abs(diff)

        triggered = False
        if rel_change >= rel_thresh:
            triggered = True
        if abs_thresh is not None and abs_change >= abs_thresh:
            triggered = True

        if not triggered:
            continue

        dir_str = "up" if diff > 0 else "down"
        if direction != "either" and dir_str != direction:
            continue

        z = abs(diff / std) if std > 0 else 0
        if z >= 2.0:
            severity = "major"
        elif z >= 1.5:
            severity = "moderate"
        else:
            severity = "minor"

        name_map = {
            "sleep_score": "Sleep Score",
            "readiness_score": "Readiness Score",
            "activity_score": "Activity Score",
            "sleep_duration_hours": "Sleep Duration",
            "sleep_efficiency": "Sleep Efficiency",
            "deep_sleep_min": "Deep Sleep",
            "rem_sleep_min": "REM Sleep",
            "avg_hr": "Avg Sleep HR",
            "lowest_hr": "Lowest HR",
            "hrv_ms": "HRV",
            "steps": "Steps",
            "spo2": "SpO2",
            "body_fat_pct": "Body Fat %",
            "weight_kg": "Weight",
            "muscle_mass_kg": "Muscle Mass",
            "calories_in": "Calories",
            "protein_g": "Protein",
        }
        name = name_map.get(metric, metric)

        msg = f"{name}: {latest_value:.1f} ({dir_str} from baseline {mean:.1f} \u00b1{std:.1f})"
        alerts.append(
            DeviationAlert(
                metric=metric,
                date=latest,
                value=latest_value,
                baseline_mean=mean,
                baseline_std=std,
                severity=severity,
                direction=dir_str,
                message=msg,
            )
        )

    severity_order = {"major": 0, "moderate": 1, "minor": 2}
    alerts.sort(key=lambda a: severity_order[a.severity])
    return alerts


# ── Adaptive deviation detection ───────────────────────────────────────────────────────────────────

# Adaptive thresholds: use user's personal rolling mean + std instead of fixed cutoffs.
# Trigger when latest value is > 1.5 std from personal baseline.
# Direction-aware: only alert on changes that are unfavorable.

ADAPTIVE_METRICS: dict[str, tuple[str, float]] = {
    # metric -> (direction_to_alert, min_std_threshold)
    # direction_to_alert: "above" = alert when value > mean + z*std
    #                   "below" = alert when value < mean - z*std
    #                   "either" = alert on both sides
    "sleep_score": ("below", 1.5),
    "readiness_score": ("below", 1.5),
    "activity_score": ("below", 2.0),  # activity drops are less urgent
    "sleep_duration_hours": ("below", 1.5),
    "sleep_efficiency": ("below", 1.5),
    "deep_sleep_min": ("below", 1.5),
    "rem_sleep_min": ("below", 1.5),
    "avg_hr": ("above", 1.5),  # higher HR is worse
    "lowest_hr": ("above", 1.5),
    "hrv_ms": ("below", 1.5),
    "steps": ("either", 2.0),
    "spo2": ("below", 2.0),
    "body_fat_pct": ("above", 1.5),
    "weight_kg": ("either", 2.0),
    "muscle_mass_kg": ("below", 1.5),
    "calories_in": ("either", 2.0),
    "protein_g": ("either", 2.0),
    "mood": ("below", 1.5),
    "anxiety": ("above", 1.5),
    "irritability": ("above", 1.5),
    "task_init": ("below", 1.5),
    "task_switch": ("below", 1.5),
    "stop_working": ("below", 1.5),
    "social_patience": ("below", 1.5),
    "appetite": ("either", 2.0),
}


# ── Hybrid thresholds: absolute floors for obviously bad values ──────────────
# These trigger regardless of personal baseline, preventing the "boiling frog"
# problem where a sustained decline slowly normalises in the adaptive window.
# Format: metric -> (condition, threshold, severity)
# condition: "below" = alert when value < threshold, "above" = value > threshold

HYBRID_FLOORS: dict[str, tuple[str, float, str]] = {
    "sleep_score": ("below", 80.0, "major"),
    "readiness_score": ("below", 60.0, "moderate"),
    "sleep_duration_hours": ("below", 5.0, "major"),
    "sleep_efficiency": ("below", 70.0, "moderate"),
    "deep_sleep_min": ("below", 30.0, "moderate"),
    "rem_sleep_min": ("below", 30.0, "moderate"),
    "avg_hr": ("above", 90.0, "moderate"),
    "lowest_hr": ("above", 80.0, "moderate"),
    "hrv_ms": ("below", 20.0, "major"),
    "spo2": ("below", 92.0, "major"),
    "steps": ("below", 2000.0, "minor"),
    "body_fat_pct": ("above", 25.0, "minor"),
    "mood": ("below", 3.0, "moderate"),
    "anxiety": ("above", 7.0, "moderate"),
    "irritability": ("above", 7.0, "moderate"),
    "task_init": ("below", 3.0, "moderate"),
    "task_switch": ("below", 3.0, "moderate"),
}


def detect_deviations_hybrid(
    records: Sequence[DailyRecord],
    baselines: dict[str, BaselineStats] | None = None,
    window_days: int = 14,
    min_data_points: int = 5,
) -> list[DeviationAlert]:
    """
    Hybrid deviation detection.
    1. Absolute floors: always alert on obviously bad values (prevents boiling-frog drift).
    2. Adaptive z-scores: alert on unusual deviations from personal rolling baseline.
    If both trigger for the same metric, the more severe alert wins.
    """
    if not records:
        return []

    if baselines is None:
        baselines = compute_all_baselines(records, window_days=window_days)

    latest = max(_record_date(r) for r in records if _record_date(r) is not None)  # pyright: ignore[reportArgumentType]

    # ── Step 1: adaptive alerts ─────────────────────────────────────────────────
    adaptive_alerts = detect_deviations_adaptive(
        records, baselines=baselines, window_days=window_days, min_data_points=min_data_points
    )
    alerts_by_metric: dict[str, DeviationAlert] = {a.metric: a for a in adaptive_alerts}

    # ── Step 2: absolute floor checks ──────────────────────────────────────────────
    severity_rank = {"major": 0, "moderate": 1, "minor": 2}

    total_records = sum(1 for r in records if r is not None)
    if total_records < min_data_points:
        # Skip floor checks — not enough data to be confident
        result = list(alerts_by_metric.values())
        result.sort(key=lambda a: severity_rank[a.severity])
        return result

    for metric, (condition, threshold, floor_severity) in HYBRID_FLOORS.items():
        latest_value = None
        for r in reversed(records):
            r_date = _record_date(r)
            if r_date is None:
                continue
            if r_date == latest:
                if isinstance(r, dict):
                    v = r.get(metric)
                else:
                    v = getattr(r, metric, None)
                if v is not None:
                    latest_value = float(v)
                    break

        if latest_value is None:
            continue

        triggered = (condition == "below" and latest_value < threshold) or (
            condition == "above" and latest_value > threshold
        )
        if not triggered:
            continue

        # Build floor alert message
        name_map = {
            "sleep_score": "Sleep Score",
            "readiness_score": "Readiness Score",
            "sleep_duration_hours": "Sleep Duration",
            "sleep_efficiency": "Sleep Efficiency",
            "deep_sleep_min": "Deep Sleep",
            "rem_sleep_min": "REM Sleep",
            "avg_hr": "Avg Sleep HR",
            "lowest_hr": "Lowest HR",
            "hrv_ms": "HRV",
            "spo2": "SpO2",
            "steps": "Steps",
            "body_fat_pct": "Body Fat %",
            "mood": "Mood",
            "anxiety": "Anxiety",
            "irritability": "Irritability",
            "task_init": "Task Initiation",
            "task_switch": "Task Switching",
        }
        name = name_map.get(metric, metric)
        dir_str = "below" if condition == "below" else "above"

        baseline = baselines.get(metric)
        if baseline and baseline.n >= min_data_points:
            msg = f"{name}: {latest_value:.1f} ({dir_str} absolute floor {threshold:.1f} and your baseline {baseline.mean:.1f} ±{baseline.std:.1f})"
        else:
            msg = f"{name}: {latest_value:.1f} ({dir_str} absolute floor {threshold:.1f})"

        floor_alert = DeviationAlert(
            metric=metric,
            date=latest,
            value=latest_value,
            baseline_mean=baseline.mean if baseline else None,
            baseline_std=baseline.std if baseline else None,
            severity=floor_severity,
            direction=dir_str,
            message=msg,
        )

        existing = alerts_by_metric.get(metric)
        if existing is None or severity_rank[floor_severity] < severity_rank[existing.severity]:
            alerts_by_metric[metric] = floor_alert

    # ── Step 3: sort and return ─────────────────────────────────────────────
    result = list(alerts_by_metric.values())
    result.sort(key=lambda a: severity_rank[a.severity])
    return result


def detect_deviations_adaptive(
    records: Sequence[DailyRecord],
    baselines: dict[str, BaselineStats] | None = None,
    window_days: int = 14,
    min_data_points: int = 5,
) -> list[DeviationAlert]:
    """
    Detect deviations using adaptive (personalized) thresholds.
    Alerts when a metric deviates > z_std from the user's own rolling baseline.
    """
    if not records:
        return []

    if baselines is None:
        baselines = compute_all_baselines(records, window_days=window_days)

    alerts: list[DeviationAlert] = []
    latest = max(_record_date(r) for r in records if _record_date(r) is not None)  # pyright: ignore[reportArgumentType]

    for metric, (alert_dir, z_thresh) in ADAPTIVE_METRICS.items():
        baseline = baselines.get(metric)
        if baseline is None or baseline.n < min_data_points:
            continue

        latest_value = None
        for r in reversed(records):
            r_date = _record_date(r)
            if r_date is None:
                continue
            if r_date == latest:
                if isinstance(r, dict):
                    v = r.get(metric)
                else:
                    v = getattr(r, metric, None)
                if v is not None:
                    latest_value = float(v)
                    break

        if latest_value is None:
            continue

        mean = baseline.mean
        std = baseline.std
        if std == 0:
            continue

        diff = latest_value - mean
        z = diff / std

        triggered = False
        dir_str = "up" if diff > 0 else "down"

        if (
            alert_dir == "above"
            and z > z_thresh
            or alert_dir == "below"
            and z < -z_thresh
            or alert_dir == "either"
            and abs(z) > z_thresh
        ):
            triggered = True

        if not triggered:
            continue

        if z >= 2.0 or z <= -2.0:
            severity = "major"
        elif z >= 1.5 or z <= -1.5:
            severity = "moderate"
        else:
            severity = "minor"

        name_map = {
            "sleep_score": "Sleep Score",
            "readiness_score": "Readiness Score",
            "activity_score": "Activity Score",
            "sleep_duration_hours": "Sleep Duration",
            "sleep_efficiency": "Sleep Efficiency",
            "deep_sleep_min": "Deep Sleep",
            "rem_sleep_min": "REM Sleep",
            "avg_hr": "Avg Sleep HR",
            "lowest_hr": "Lowest HR",
            "hrv_ms": "HRV",
            "steps": "Steps",
            "spo2": "SpO2",
            "body_fat_pct": "Body Fat %",
            "weight_kg": "Weight",
            "muscle_mass_kg": "Muscle Mass",
            "calories_in": "Calories",
            "protein_g": "Protein",
            "mood": "Mood",
            "anxiety": "Anxiety",
            "irritability": "Irritability",
            "task_init": "Task Initiation",
            "task_switch": "Task Switching",
            "stop_working": "Stop Working",
            "social_patience": "Social Patience",
            "appetite": "Appetite",
        }
        name = name_map.get(metric, metric)

        msg = f"{name}: {latest_value:.1f} ({dir_str} from your baseline {mean:.1f} ±{std:.1f}, z={z:+.2f})"
        alerts.append(
            DeviationAlert(
                metric=metric,
                date=latest,
                value=latest_value,
                baseline_mean=mean,
                baseline_std=std,
                severity=severity,
                direction=dir_str,
                message=msg,
            )
        )

    severity_order = {"major": 0, "moderate": 1, "minor": 2}
    alerts.sort(key=lambda a: severity_order[a.severity])
    return alerts


# ── Trend descriptions ───────────────────────────────────────────────────────


def describe_trend(stats: BaselineStats) -> str:
    """Human-friendly description of a metric's trend."""
    if stats.trend is None or stats.n < 3:
        return "stable (insufficient data)"

    weekly = stats.trend * 7
    metric = stats.metric

    good_up = {
        "sleep_score",
        "readiness_score",
        "activity_score",
        "sleep_duration_hours",
        "sleep_efficiency",
        "deep_sleep_min",
        "rem_sleep_min",
        "hrv_ms",
        "steps",
        "spo2",
        "muscle_mass_kg",
        "protein_g",
    }
    good_down = {"avg_hr", "lowest_hr", "body_fat_pct", "weight_kg", "anxiety", "irritability"}

    direction = (
        "improving"
        if (weekly > 0 and metric in good_up) or (weekly < 0 and metric in good_down)
        else "declining"
        if (weekly < 0 and metric in good_up) or (weekly > 0 and metric in good_down)
        else "changing"
    )

    magnitude = abs(weekly)
    if magnitude < 0.01:
        return "stable"
    elif magnitude < 0.5:
        return f"slightly {direction} ({weekly:+.2f}/week)"
    elif magnitude < 2.0:
        return f"{direction} ({weekly:+.2f}/week)"
    else:
        return f"sharply {direction} ({weekly:+.2f}/week)"


# ── Dashboard markdown generation ────────────────────────────────────────────


def format_baseline_table(baselines: dict[str, BaselineStats]) -> str:
    """Format baseline stats as a markdown table."""
    lines = [
        "| Metric | Mean | Median | Std | Min | Max | Latest | Trend |",
        "|--------|------|--------|-----|-----|-----|--------|-------|",
    ]
    name_map = {
        "sleep_score": "Sleep Score",
        "readiness_score": "Readiness",
        "activity_score": "Activity",
        "sleep_duration_hours": "Sleep (h)",
        "sleep_efficiency": "Efficiency (%)",
        "deep_sleep_min": "Deep (min)",
        "rem_sleep_min": "REM (min)",
        "avg_hr": "Avg HR",
        "lowest_hr": "Low HR",
        "hrv_ms": "HRV (ms)",
        "steps": "Steps",
        "spo2": "SpO2 (%)",
        "body_fat_pct": "Body Fat %",
        "weight_kg": "Weight (kg)",
        "muscle_mass_kg": "Muscle (kg)",
        "calories_in": "Calories",
        "protein_g": "Protein (g)",
    }

    for metric, stats in sorted(baselines.items()):
        name = name_map.get(metric, metric)
        trend_str = describe_trend(stats) if stats.trend is not None else "—"
        lines.append(
            f"| {name} | {stats.mean} | {stats.median} | {stats.std} | "
            f"{stats.min} | {stats.max} | {stats.latest or '—'} | {trend_str} |"
        )

    return "\n".join(lines)


def format_alerts(alerts: list[DeviationAlert]) -> str:
    """Format alerts as a markdown list."""
    if not alerts:
        return "*No significant deviations detected.*"

    emoji = {"major": "⚠️", "moderate": "⚠", "minor": "●"}
    lines = []
    for a in alerts:
        lines.append(f"- {emoji[a.severity]} **{a.severity.upper()}**: {a.message}")
    return "\n".join(lines)


# ── Renpho CSV parser ────────────────────────────────────────────────────────


def parse_renpho_csv(path: str | Path) -> list[DailyRecord]:
    """Parse Renpho CSV export into DailyRecord list."""
    records: list[DailyRecord] = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            date_str = row.get("Date", row.get("date", ""))
            if not date_str:
                continue
            try:
                d = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S").date()
            except ValueError:
                try:
                    d = datetime.strptime(date_str, "%Y-%m-%d").date()
                except ValueError:
                    continue

            r = DailyRecord(date=d, sources=["renpho"])
            for key, attr in [
                ("Weight(kg)", "weight_kg"),
                ("Weight", "weight_kg"),
                ("Body Fat(%)", "body_fat_pct"),
                ("Body Fat", "body_fat_pct"),
                ("Muscle Mass(kg)", "muscle_mass_kg"),
                ("Muscle Mass", "muscle_mass_kg"),
            ]:
                if attr and key in row:
                    try:
                        setattr(r, attr, float(row[key]))
                    except (ValueError, TypeError):
                        pass
            records.append(r)

    return records


# ── MyNetDiary CSV parser ────────────────────────────────────────────────────


def parse_mynetdiary_csv(path: str | Path) -> list[DailyRecord]:
    """Parse MyNetDiary CSV export into DailyRecord list."""
    records: list[DailyRecord] = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            date_str = row.get("Date", row.get("date", ""))
            if not date_str:
                continue
            try:
                d = datetime.strptime(date_str, "%Y-%m-%d").date()
            except ValueError:
                continue

            r = DailyRecord(date=d, sources=["mynetdiary"])
            for key, attr in [
                ("Weight", "weight_kg"),
                ("Calories", "calories_in"),
                ("Protein", "protein_g"),
                ("CaloriesIn", "calories_in"),
                ("Calories Consumed", "calories_in"),
                ("Protein (g)", "protein_g"),
            ]:
                if key in row:
                    try:
                        val = float(row[key])
                        setattr(r, attr, val)
                    except (ValueError, TypeError):
                        pass
            records.append(r)

    return records


# ── Record merging ───────────────────────────────────────────────────────────


def merge_records(
    *record_lists: Sequence[DailyRecord],
    priority: Sequence[str] = ("manual", "renpho", "oura", "mynetdiary"),
) -> list[DailyRecord]:
    """Merge multiple record lists, with later sources overriding earlier ones by priority."""
    by_date: dict[date, dict[str, Any]] = {}

    for records in record_lists:
        for r in records:
            if r is None:
                continue
            if isinstance(r, dict):
                # Handle raw cache dicts — convert to DailyRecord
                r_date = date.fromisoformat(r["date"]) if isinstance(r["date"], str) else r["date"]
                rd = DailyRecord(date=r_date)
                for k, v in r.items():
                    if k != "date" and hasattr(rd, k):
                        setattr(rd, k, v)
                r = rd
            if r.date not in by_date:
                by_date[r.date] = {}
            d = by_date[r.date]
            for attr, val in r.to_dict().items():
                if attr == "date":
                    continue
                if attr == "sources":
                    existing = d.get("sources", [])
                    if isinstance(val, list):
                        for s in val:
                            if s not in existing:
                                existing.append(s)
                    d["sources"] = existing
                    continue
                if val is not None:
                    current_source = d.get("_last_source", "")
                    new_source = r.sources[0] if r.sources else ""
                    curr_idx = (
                        list(priority).index(current_source) if current_source in priority else 999
                    )
                    new_idx = list(priority).index(new_source) if new_source in priority else 999
                    if attr not in d or d[attr] is None or new_idx <= curr_idx:
                        d[attr] = val
                        d["_last_source"] = new_source

    result = []
    for d, data in sorted(by_date.items()):
        data.pop("_last_source", None)
        r = DailyRecord(date=d)
        for attr, val in data.items():
            if hasattr(r, attr):
                setattr(r, attr, val)
        result.append(r)

    return result


# ── Correlation detection ──────────────────────────────────────────────────────────

# Pairs of metrics to check for correlations. Each tuple is (metric_a, metric_b, description)
CORRELATION_PAIRS: list[tuple[str, str, str]] = [
    ("sleep_score", "readiness_score", "Sleep quality → next-day recovery"),
    ("sleep_duration_hours", "readiness_score", "Sleep length → recovery"),
    ("sleep_score", "hrv_ms", "Sleep quality → HRV"),
    ("steps", "readiness_score", "Activity → next-day recovery"),
    ("steps", "sleep_score", "Activity → sleep quality"),
    ("avg_hr", "readiness_score", "Sleep HR → recovery (inverse expected)"),
    ("hrv_ms", "readiness_score", "HRV → recovery"),
    ("deep_sleep_min", "readiness_score", "Deep sleep → recovery"),
    ("rem_sleep_min", "readiness_score", "REM sleep → recovery"),
    ("sleep_efficiency", "readiness_score", "Sleep efficiency → recovery"),
    ("morning_grogginess", "sleep_score", "Morning grogginess → sleep quality (inverse expected)"),
    ("morning_grogginess", "readiness_score", "Morning grogginess → readiness (inverse expected)"),
    ("morning_grogginess", "hrv_ms", "Morning grogginess → HRV (inverse expected)"),
    ("mood", "anxiety", "Mood → anxiety (inverse expected)"),
    ("mood", "irritability", "Mood → irritability (inverse expected)"),
    ("mood", "task_init", "Mood → task initiation"),
    ("calories_in", "sleep_score", "Calorie intake → sleep quality"),
    ("protein_g", "sleep_score", "Protein intake → sleep quality"),
    ("weight_kg", "body_fat_pct", "Weight → body fat %"),
    ("muscle_mass_kg", "body_fat_pct", "Muscle mass → body fat % (inverse expected)"),
    # ── Hevy correlations ────────────────────────────────────────────────────────
    ("hevy_total_volume_kg", "sleep_score", "Training volume → sleep quality (inverse expected)"),
    (
        "hevy_total_volume_kg",
        "readiness_score",
        "Training volume → next-day recovery (inverse expected)",
    ),
    ("hevy_total_volume_kg", "hrv_ms", "Training volume → HRV (inverse expected)"),
    (
        "hevy_total_duration_min",
        "sleep_score",
        "Training duration → sleep quality (inverse expected)",
    ),
    ("hevy_avg_rpe", "sleep_score", "Training intensity (RPE) → sleep quality (inverse expected)"),
    ("hevy_avg_rpe", "readiness_score", "Training intensity (RPE) → recovery (inverse expected)"),
    ("hevy_max_weight_kg", "sleep_score", "Max lift → sleep quality"),
    ("hevy_workouts", "steps", "Gym sessions → daily steps"),
    ("hevy_total_volume_kg", "morning_grogginess", "Training volume → morning grogginess"),
    ("hevy_total_volume_kg", "avg_hr", "Training volume → sleep HR"),
]


def merge_hevy_into_records(workouts: list, records: list[DailyRecord]) -> list[DailyRecord]:
    """
    Merge Hevy workout summaries into existing DailyRecords by date.
    `workouts` should be a list of HevyWorkout objects (from hevy_client).
    """
    if not workouts:
        return records

    # Group workouts by date
    by_date: dict[date, list] = {}
    for w in workouts:
        d = w.start_time.date()
        by_date.setdefault(d, []).append(w)

    record_by_date = {r.date: r for r in records}

    for d, day_workouts in by_date.items():
        total_vol = sum(w.total_volume for w in day_workouts)
        total_dur = sum(w.duration_minutes for w in day_workouts)
        all_muscles: set[str] = set()
        max_weight = 0.0
        total_rpe = 0.0
        rpe_count = 0

        for w in day_workouts:
            all_muscles.update(w.muscle_groups)
            for ex in w.exercises:
                for s in ex.sets:
                    if s.weight_kg is not None and s.weight_kg > max_weight:
                        max_weight = s.weight_kg
                    if s.rpe is not None:
                        total_rpe += s.rpe
                        rpe_count += 1

        avg_rpe = total_rpe / rpe_count if rpe_count > 0 else None

        if d in record_by_date:
            r = record_by_date[d]
            r.hevy_workouts = len(day_workouts)
            r.hevy_total_volume_kg = round(total_vol, 1) if total_vol > 0 else None
            r.hevy_total_duration_min = round(total_dur, 1) if total_dur > 0 else None
            r.hevy_muscle_groups = sorted(all_muscles)
            r.hevy_max_weight_kg = max_weight if max_weight > 0 else None
            r.hevy_avg_rpe = round(avg_rpe, 1) if avg_rpe is not None else None
            if "hevy" not in r.sources:
                r.sources.append("hevy")
        else:
            new_r = DailyRecord(
                date=d,
                hevy_workouts=len(day_workouts),
                hevy_total_volume_kg=round(total_vol, 1) if total_vol > 0 else None,
                hevy_total_duration_min=round(total_dur, 1) if total_dur > 0 else None,
                hevy_muscle_groups=sorted(all_muscles),
                hevy_max_weight_kg=max_weight if max_weight > 0 else None,
                hevy_avg_rpe=round(avg_rpe, 1) if avg_rpe is not None else None,
                sources=["hevy"],
            )
            records.append(new_r)
            record_by_date[d] = new_r

    records.sort(key=lambda r: r.date)
    return records


def _pearson_corr(x: Sequence[float], y: Sequence[float]) -> float | None:
    """Compute Pearson correlation coefficient between two equal-length sequences."""
    if len(x) != len(y) or len(x) < 3:
        return None
    n = len(x)
    mean_x = sum(x) / n
    mean_y = sum(y) / n
    num = sum((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, y, strict=False))
    den_x = sum((xi - mean_x) ** 2 for xi in x)
    den_y = sum((yi - mean_y) ** 2 for yi in y)
    if den_x == 0 or den_y == 0:
        return None
    return num / math.sqrt(den_x * den_y)


@dataclass
class CorrelationResult:
    metric_a: str
    metric_b: str
    description: str
    n: int
    r: float
    strength: str  # "negligible", "weak", "moderate", "strong"
    direction: str  # "positive", "negative"
    interpretation: str


def compute_correlations(
    records: Sequence[DailyRecord],
    pairs: Sequence[tuple[str, str, str]] | None = None,
    min_points: int = 5,
) -> list[CorrelationResult]:
    """Find statistically meaningful correlations between metric pairs."""
    if pairs is None:
        pairs = CORRELATION_PAIRS

    results: list[CorrelationResult] = []

    for metric_a, metric_b, description in pairs:
        # Build aligned lists (same dates only)
        a_vals = []
        b_vals = []
        for r in records:
            va = getattr(r, metric_a, None)
            vb = getattr(r, metric_b, None)
            if va is not None and vb is not None:
                a_vals.append(float(va))
                b_vals.append(float(vb))

        if len(a_vals) < min_points:
            continue

        r_val = _pearson_corr(a_vals, b_vals)
        if r_val is None:
            continue

        abs_r = abs(r_val)
        if abs_r < 0.2:
            strength = "negligible"
        elif abs_r < 0.4:
            strength = "weak"
        elif abs_r < 0.6:
            strength = "moderate"
        else:
            strength = "strong"

        direction = "positive" if r_val > 0 else "negative"

        # Human-friendly interpretation
        if strength == "negligible":
            interp = f"No meaningful relationship ({r_val:+.2f})"
        else:
            # Check if direction matches expectation
            expected_inverse = "inverse expected" in description
            matches = (direction == "negative" and expected_inverse) or (
                direction == "positive" and not expected_inverse
            )
            if matches:
                interp = f"{strength.title()} {direction} correlation ({r_val:+.2f}) — aligns with expectation"
            else:
                interp = f"{strength.title()} {direction} correlation ({r_val:+.2f}) — opposite of expectation"

        results.append(
            CorrelationResult(
                metric_a=metric_a,
                metric_b=metric_b,
                description=description,
                n=len(a_vals),
                r=round(r_val, 3),
                strength=strength,
                direction=direction,
                interpretation=interp,
            )
        )

    # Sort by absolute correlation strength (strongest first)
    results.sort(key=lambda c: abs(c.r), reverse=True)
    return results


def format_correlations(results: list[CorrelationResult], top_n: int = 8) -> str:
    """Format correlation results as a markdown list."""
    if not results:
        return "*Not enough overlapping data to compute correlations yet.*"

    lines = []
    for r in results[:top_n]:
        emoji = {"strong": "🔍", "moderate": "👁️", "weak": "•", "negligible": "○"}
        lines.append(
            f"- {emoji.get(r.strength, '•')} **{r.description}**: {r.interpretation} (n={r.n})"
        )
    return "\n".join(lines)


# ── Tag-based sleep analysis ──────────────────────────────────────────────────────────────────────────────

from vital_sync.sleep_tags import (
    SleepTagManager,  # local import to avoid circular issues at module load
)

TAG_METRICS = [
    "sleep_score",
    "readiness_score",
    "sleep_duration_hours",
    "hrv_ms",
    "rem_sleep_min",
    "deep_sleep_min",
    "avg_hr",
]


def compute_tag_sleep_analysis(
    records: Sequence[DailyRecord], min_n: int = 3
) -> list[dict[str, Any]]:
    """
    Compare sleep metrics on nights with vs without each tag.
    Returns list of dicts with tag, metric, means, and diff.
    """
    manager = SleepTagManager()
    used_tags = manager.get_all_used_tags()
    if not used_tags:
        return []

    results: list[dict[str, Any]] = []
    for tag in used_tags:
        for metric in TAG_METRICS:
            comp = manager.compute_tag_metric_comparison(list(records), tag, metric)
            if comp and comp["n_with"] >= min_n and comp["n_without"] >= min_n:
                results.append(comp)
    # Sort by absolute pct_diff descending
    results.sort(key=lambda x: abs(x.get("pct_diff", 0)), reverse=True)
    return results


def format_tag_analysis(results: list[dict[str, Any]], top_n: int = 6) -> str:
    """Format tag analysis as human-friendly markdown."""
    if not results:
        return "*Not enough tagged nights to analyze tag effects yet.*"

    metric_names = {
        "sleep_score": "sleep score",
        "readiness_score": "readiness",
        "sleep_duration_hours": "sleep duration",
        "hrv_ms": "HRV",
        "rem_sleep_min": "REM sleep",
        "deep_sleep_min": "deep sleep",
        "avg_hr": "avg sleep HR",
    }

    lines = []
    for r in results[:top_n]:
        tag = r["tag"]
        metric = metric_names.get(r["metric"], r["metric"])
        diff = r["diff"]
        pct = r["pct_diff"]
        n_with = r["n_with"]
        n_without = r["n_without"]

        # Determine if the diff is "good" or "bad"
        good_up = {
            "sleep_score",
            "readiness_score",
            "sleep_duration_hours",
            "hrv_ms",
            "rem_sleep_min",
            "deep_sleep_min",
        }
        good_down = {"avg_hr"}

        direction = "higher" if diff > 0 else "lower"
        if (r["metric"] in good_up and diff > 0) or (r["metric"] in good_down and diff < 0):
            verdict = "better"
        else:
            verdict = "worse"

        lines.append(
            f"- **{tag}** → {metric} {direction} by {abs(pct)}% "
            f"({r['mean_with']} vs {r['mean_without']}, n={n_with}/{n_without}) — {verdict}"
        )
    return "\n".join(lines)


# ── JSON serialization for caching ───────────────────────────────────────────


def records_to_json(records: Sequence[DailyRecord]) -> str:
    return json.dumps([r.to_dict() for r in records], indent=2)


def records_from_json(text: str) -> list[DailyRecord]:
    data = json.loads(text)
    records = []
    for d in data:
        r = DailyRecord(date=date.fromisoformat(d.pop("date")))
        for attr, val in d.items():
            if hasattr(r, attr):
                setattr(r, attr, val)
        records.append(r)
    return records


def save_cache(records: Sequence[DailyRecord], path: str | Path) -> None:
    Path(path).write_text(records_to_json(records), encoding="utf-8")


def load_cache(path: str | Path) -> list[DailyRecord]:
    p = Path(path)
    if not p.exists():
        return []
    return records_from_json(p.read_text(encoding="utf-8"))


# ── Human-friendly score labels ──────────────────────────────────────────────


def sleep_score_label(score: float) -> str:
    if score >= 90:
        return "great"
    elif score >= 85:
        return "good"
    elif score >= 80:
        return "decent"
    else:
        return "bad"


def readiness_label(score: float) -> str:
    if score >= 85:
        return "optimal"
    elif score >= 70:
        return "good"
    elif score >= 60:
        return "fair"
    else:
        return "pay attention"


def activity_label(score: float) -> str:
    if score >= 85:
        return "optimal"
    elif score >= 70:
        return "good"
    elif score >= 60:
        return "fair"
    else:
        return "pay attention"


# ── Circadian consistency metrics ───────────────────────────────────────────────────────────────────────────

from datetime import time


def compute_circadian_consistency(
    records: Sequence[DailyRecord],
    window_days: int = 14,
) -> dict[str, Any]:
    """
    Compute circadian consistency metrics from sleep data.
    Returns bedtime variance, wake time variance, and social jetlag index.
    """
    if not records:
        return {}

    end_date = max(r.date for r in records)
    start_date = end_date - timedelta(days=window_days - 1)

    bedtimes: list[time] = []
    wake_times: list[time] = []
    weekday_wake: list[time] = []
    weekend_wake: list[time] = []

    for r in records:
        if start_date <= r.date <= end_date:
            if r.bedtime:
                try:
                    bedtimes.append(datetime.strptime(r.bedtime, "%H:%M").time())
                except ValueError:
                    pass
            if r.wake_time:
                try:
                    t = datetime.strptime(r.wake_time, "%H:%M").time()
                    wake_times.append(t)
                    # Weekday vs weekend (Monday=0, Sunday=6)
                    if r.date.weekday() < 5:
                        weekday_wake.append(t)
                    else:
                        weekend_wake.append(t)
                except ValueError:
                    pass

    # If we have no timing data, return empty
    if not bedtimes and not wake_times:
        return {
            "bedtime_variance_min": None,
            "wake_time_variance_min": None,
            "social_jetlag_min": None,
            "avg_weekday_wake": None,
            "avg_weekend_wake": None,
            "consistency_score": None,
            "nights": len(
                [r for r in records if start_date <= r.date <= end_date and r.sleep_duration_hours]
            ),
        }

    def _time_to_minutes(t: time) -> int:
        return t.hour * 60 + t.minute

    def _variance_minutes(times: list[time]) -> float | None:
        if len(times) < 2:
            return None
        mins = [_time_to_minutes(t) for t in times]
        # Handle wraparound at midnight (e.g., 23:30 vs 00:30)
        # Use circular statistics for times
        angles = [m / 1440 * 2 * math.pi for m in mins]
        sin_sum = sum(math.sin(a) for a in angles)
        cos_sum = sum(math.cos(a) for a in angles)
        r = math.sqrt(sin_sum**2 + cos_sum**2) / len(angles)
        # Circular standard deviation in minutes
        if r >= 1:
            return 0.0
        circ_std = math.sqrt(-2 * math.log(r)) * 1440 / (2 * math.pi)
        return round(circ_std, 1)

    def _mean_time(times: list[time]) -> time | None:
        if not times:
            return None
        mins = [_time_to_minutes(t) for t in times]
        angles = [m / 1440 * 2 * math.pi for m in mins]
        sin_sum = sum(math.sin(a) for a in angles)
        cos_sum = sum(math.cos(a) for a in angles)
        mean_angle = math.atan2(sin_sum, cos_sum)
        mean_mins = (mean_angle / (2 * math.pi)) * 1440
        if mean_mins < 0:
            mean_mins += 1440
        return time(hour=int(mean_mins // 60), minute=int(mean_mins % 60))

    bedtime_var = _variance_minutes(bedtimes)
    wake_var = _variance_minutes(wake_times)
    avg_weekday = _mean_time(weekday_wake)
    avg_weekend = _mean_time(weekend_wake)

    social_jetlag = None
    if avg_weekday and avg_weekend:
        diff = abs(_time_to_minutes(avg_weekend) - _time_to_minutes(avg_weekday))
        if diff > 720:
            diff = 1440 - diff
        social_jetlag = diff

    # Consistency score: 100 = perfectly consistent, 0 = highly variable
    # Based on circular std of wake times (target < 30 min = good)
    consistency = None
    if wake_var is not None:
        consistency = max(0, min(100, round(100 - (wake_var / 60) * 100, 1)))

    return {
        "bedtime_variance_min": bedtime_var,
        "wake_time_variance_min": wake_var,
        "social_jetlag_min": social_jetlag,
        "avg_weekday_wake": avg_weekday.isoformat() if avg_weekday else None,
        "avg_weekend_wake": avg_weekend.isoformat() if avg_weekend else None,
        "consistency_score": consistency,
        "nights": len(
            [r for r in records if start_date <= r.date <= end_date and r.sleep_duration_hours]
        ),
    }


def format_circadian_report(metrics: dict[str, Any]) -> str:
    """Format circadian consistency metrics as a brief narrative."""
    if metrics.get("consistency_score") is None:
        return "*Not enough sleep timing data for circadian analysis yet.*"

    lines = []
    score = metrics["consistency_score"]
    if score >= 80:
        lines.append(f"Your sleep timing is very consistent ({score}/100).")
    elif score >= 60:
        lines.append(f"Your sleep timing is moderately consistent ({score}/100).")
    else:
        lines.append(f"Your sleep timing is quite variable ({score}/100).")

    if metrics.get("social_jetlag_min") is not None:
        sj = metrics["social_jetlag_min"]
        if sj > 60:
            lines.append(
                f"Social jetlag: {sj} min difference between weekday and weekend wake times."
            )
        else:
            lines.append("Social jetlag is minimal.")

    if metrics.get("wake_time_variance_min") is not None:
        wv = metrics["wake_time_variance_min"]
        lines.append(f"Wake time variability: ~{wv} min standard deviation.")

    return " ".join(lines)


if __name__ == "__main__":
    test_records = [
        DailyRecord(
            date=date.today() - timedelta(days=2), sleep_score=78, readiness_score=76, hrv_ms=35
        ),
        DailyRecord(
            date=date.today() - timedelta(days=1), sleep_score=82, readiness_score=80, hrv_ms=38
        ),
        DailyRecord(date=date.today(), sleep_score=68, readiness_score=65, hrv_ms=28),
    ]
    baselines = compute_all_baselines(test_records, window_days=3)
    alerts = detect_deviations(test_records, baselines)
    print("Baselines:")
    for m, s in baselines.items():
        print(f"  {m}: mean={s.mean}, trend={s.trend}")
    print("\nAlerts:")
    for a in alerts:
        print(f"  [{a.severity}] {a.message}")
