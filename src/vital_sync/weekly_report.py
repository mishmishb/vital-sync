"""Weekly health trend report context builder.

Loads all data sources, computes all statistics, and outputs a structured JSON
summary. The calling LLM only needs to do narrative interpretation.

Usage:
    python -m vital_sync.weekly_report                    # last 14 days vs previous 7
    python -m vital_sync.weekly_report --json             # JSON only, no print statements
"""

from __future__ import annotations

import json
import math
import os
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from vital_sync.analytics import (
    DailyRecord,
    compute_all_baselines,
    compute_correlations,
    detect_deviations_hybrid,
    load_cache,
)


def _safe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _circular_mean(times_minutes: list[float]) -> float | None:
    """Circular mean of times in minutes from midnight (0–1440)."""
    if not times_minutes:
        return None
    angles = [(t / 1440) * 2 * math.pi for t in times_minutes]
    sin_sum = sum(math.sin(a) for a in angles)
    cos_sum = sum(math.cos(a) for a in angles)
    mean_angle = math.atan2(sin_sum / len(angles), cos_sum / len(angles))
    mean_time = (mean_angle / (2 * math.pi)) * 1440
    if mean_time < 0:
        mean_time += 1440
    return mean_time


def _circular_std(times_minutes: list[float]) -> float | None:
    """Circular standard deviation in minutes."""
    if not times_minutes or len(times_minutes) < 2:
        return None
    angles = [(t / 1440) * 2 * math.pi for t in times_minutes]
    sin_sum = sum(math.sin(a) for a in angles)
    cos_sum = sum(math.cos(a) for a in angles)
    R = math.sqrt(sin_sum**2 + cos_sum**2) / len(angles)
    if R >= 1.0:
        return 0.0
    return (math.sqrt(-2 * math.log(R)) / (2 * math.pi)) * 1440


def _time_to_minutes(ts: str | None) -> float | None:
    """Convert 'HH:MM' or ISO time string to minutes from midnight."""
    if not ts:
        return None
    if "T" in ts:
        ts = ts.split("T")[1][:5]
    if ":" not in ts or len(ts) < 5:
        return None
    try:
        h, m = ts.split(":")[:2]
        return int(h) * 60 + int(m)
    except (ValueError, TypeError):
        return None


def _sleep_tag_deviations(
    records: list[DailyRecord],
    tags_path: str,
    window_start: date,
) -> dict[str, Any]:
    """Summarise sleep tag deviations for the window."""
    if not os.path.exists(tags_path):
        return {"negated_baselines": {}, "applied_extras": {}, "learned": []}

    with open(tags_path) as f:
        tag_data = json.load(f)

    baselines = set(tag_data.get("baseline_tags", []))
    history = tag_data.get("history", {})

    negated: dict[str, list[str]] = defaultdict(list)
    applied_extras: dict[str, list[str]] = defaultdict(list)

    for r in records:
        r_date = r.date.isoformat() if hasattr(r, "date") else str(r.date)
        if r_date < window_start.isoformat():
            continue
        day_history = history.get(r_date, {})
        for tag in day_history.get("negated", []):
            negated[tag].append(r_date)
        for tag in day_history.get("applied", []):
            applied_extras[tag].append(r_date)

    for r in records:
        r_date = r.date.isoformat() if hasattr(r, "date") else str(r.date)
        if r_date < window_start.isoformat():
            continue
        manual_tags = getattr(r, "sleep_tags", None)
        if manual_tags and isinstance(manual_tags, list):
            for tag in manual_tags:
                if tag not in baselines:
                    if r_date not in applied_extras.get(tag, []):
                        applied_extras[tag].append(r_date)

    return {
        "negated_baselines": {k: v for k, v in negated.items() if k in baselines},
        "applied_extras": {k: v for k, v in applied_extras.items()},
    }


def _hevy_summary(
    workouts_raw: list[dict], window_start: date, window_end: date
) -> dict[str, Any]:
    """Summarise Hevy training data for the window from raw workout JSON."""
    total_volume = 0.0
    total_sessions = 0
    total_duration = 0.0
    muscle_hits: dict[str, int] = defaultdict(int)
    dates_trained: set[str] = set()

    for w in workouts_raw:
        start_str = w.get("start_time", "")
        if not start_str:
            continue
        try:
            w_date = date.fromisoformat(start_str[:10])
        except (ValueError, TypeError):
            continue
        if not (window_start <= w_date <= window_end):
            continue

        total_sessions += 1
        dates_trained.add(w_date.isoformat())

        for ex in w.get("exercises", []):
            for s in ex.get("sets", []):
                reps = _safe_float(s.get("reps", 0)) or 0
                weight = _safe_float(s.get("weight_kg", 0)) or 0
                total_volume += reps * weight
            mg = ex.get("muscle_group", "")
            if mg:
                muscle_hits[mg.lower()] += 1

        end_str = w.get("end_time", "")
        if start_str and end_str:
            try:
                s = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                e = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                total_duration += (e - s).total_seconds() / 60
            except (ValueError, TypeError):
                pass

    push_groups = {"chest", "shoulders", "triceps"}
    pull_groups = {"back", "biceps", "traps"}
    legs_groups = {"quadriceps", "hamstrings", "glutes", "calves"}
    core_groups = {"abs", "lower back"}

    pattern_hits = {"push": 0, "pull": 0, "legs": 0, "core": 0}
    for group, count in muscle_hits.items():
        gl = group.lower()
        if any(p in gl for p in push_groups):
            pattern_hits["push"] += count
        elif any(p in gl for p in pull_groups):
            pattern_hits["pull"] += count
        elif any(p in gl for p in legs_groups):
            pattern_hits["legs"] += count
        elif any(p in gl for p in core_groups):
            pattern_hits["core"] += count

    total_hits = sum(pattern_hits.values()) or 1
    pattern_pct = {k: round(v / total_hits * 100) for k, v in pattern_hits.items()}

    return {
        "sessions": total_sessions,
        "volume_kg": round(total_volume),
        "duration_min": round(total_duration),
        "days_trained": len(dates_trained),
        "avg_volume_per_session": round(total_volume / total_sessions) if total_sessions else 0,
        "muscle_group_distribution": dict(muscle_hits),
        "movement_pattern_balance": pattern_pct,
    }


def _body_composition(records: list[DailyRecord]) -> dict[str, Any]:
    """Extract body composition data from Renpho fields."""
    weights = []
    bf_pcts = []
    muscle_kgs = []
    latest = {}
    for r in records:
        w = _safe_float(getattr(r, "weight_kg", None))
        bf = _safe_float(getattr(r, "body_fat_pct", None))
        mm = _safe_float(getattr(r, "muscle_mass_kg", None))
        if w:
            weights.append(w)
            latest["weight_kg"] = w
        if bf:
            bf_pcts.append(bf)
            latest["body_fat_pct"] = bf
        if mm:
            muscle_kgs.append(mm)
            latest["muscle_mass_kg"] = mm

    return {
        "latest": latest if latest else None,
        "weight_avg": round(wm, 1) if (wm := _mean(weights)) is not None else None,
        "body_fat_avg": round(bm, 1) if (bm := _mean(bf_pcts)) is not None else None,
        "muscle_mass_avg": round(mm, 1) if (mm := _mean(muscle_kgs)) is not None else None,
    }


def _circadian_stats(records: list[DailyRecord]) -> dict[str, Any]:
    """Compute circadian consistency from bed/wake times."""
    bedtimes: list[float] = []
    waketimes: list[float] = []
    weekday_wakes: list[float] = []
    weekend_wakes: list[float] = []

    for r in records:
        r_date = r.date if hasattr(r, "date") else None
        if r_date is None:
            continue
        bed_str = getattr(r, "bedtime_start", None)
        wake_str = getattr(r, "bedtime_end", None)
        bed_m = _time_to_minutes(bed_str)
        wake_m = _time_to_minutes(wake_str)
        if bed_m is not None:
            bedtimes.append(bed_m)
        if wake_m is not None:
            waketimes.append(wake_m)
            if r_date.weekday() < 5:
                weekday_wakes.append(wake_m)
            else:
                weekend_wakes.append(wake_m)

    mean_wake_weekday = _circular_mean(weekday_wakes) if weekday_wakes else None
    mean_wake_weekend = _circular_mean(weekend_wakes) if weekend_wakes else None

    social_jetlag = None
    if mean_wake_weekday is not None and mean_wake_weekend is not None:
        diff = abs(mean_wake_weekend - mean_wake_weekday)
        if diff > 720:
            diff = 1440 - diff
        social_jetlag = round(diff)

    bed_std = _circular_std(bedtimes)
    wake_std = _circular_std(waketimes)
    avg_std = ((bed_std or 0) + (wake_std or 0)) / 2
    consistency = max(0, round(100 - avg_std / 2))

    return {
        "bedtime_std_min": round(bed_std) if bed_std is not None else None,
        "waketime_std_min": round(wake_std) if wake_std is not None else None,
        "social_jetlag_min": social_jetlag,
        "consistency_score": consistency,
    }


def _get_data_paths():
    """Resolve data paths from env vars or defaults."""
    data_dir = Path(os.environ.get("VITAL_SYNC_DATA_DIR", str(Path.home() / ".vital_sync")))
    cache_path = data_dir / "cache.json"
    hevy_cache_dir = data_dir / "hevy_cache"
    tags_path = data_dir / "sleep_tags.json"
    return data_dir, cache_path, hevy_cache_dir, tags_path


def main() -> None:
    today = date.today()
    window_end = today - timedelta(days=1)  # up to yesterday
    window_start = window_end - timedelta(days=13)  # 14-day window
    comparison_start = window_start - timedelta(days=7)

    data_dir, cache_path, hevy_cache_dir, tags_path = _get_data_paths()
    hevy_path = hevy_cache_dir / "workouts.json"

    # Load and filter
    all_records = load_cache(str(cache_path))
    records_14d = [r for r in all_records if window_start <= r.date <= window_end]
    records_prev = [r for r in all_records if comparison_start <= r.date < window_start]

    # Load Hevy (raw JSON)
    hevy_workouts_raw = []
    if hevy_path.exists():
        with open(hevy_path) as f:
            hevy_workouts_raw = json.load(f)

    # Compute all baselines
    baselines = compute_all_baselines(records_14d, window_days=14, end_date=window_end)
    baselines_prev = compute_all_baselines(
        records_prev, window_days=7, end_date=window_start - timedelta(days=1)
    )

    # Detect deviations
    alerts = detect_deviations_hybrid(records_14d, baselines)

    # Correlations
    metric_pairs = [
        ("sleep_score", "readiness_score", "Sleep quality → next-day readiness"),
        ("sleep_duration_hours", "sleep_score", "Duration → sleep quality"),
        ("sleep_duration_hours", "readiness_score", "Duration → readiness"),
        ("hevy_total_volume_kg", "sleep_score", "Training volume → sleep quality"),
        ("hevy_total_volume_kg", "readiness_score", "Training volume → readiness"),
        ("steps", "sleep_score", "Steps → sleep quality"),
        ("avg_hr", "sleep_score", "Sleep HR → sleep quality"),
        ("hrv_ms", "readiness_score", "HRV → readiness"),
        ("sleep_score", "mood", "Sleep → next-day mood"),
        ("readiness_score", "task_init", "Readiness → task initiation"),
    ]
    correlations = compute_correlations(records_14d, metric_pairs)

    # Tag deviations
    tag_info = _sleep_tag_deviations(records_14d, str(tags_path), window_start)

    # Hevy summary
    hevy = _hevy_summary(hevy_workouts_raw, window_start, window_end)

    # Body composition
    body = _body_composition(records_14d)

    # Circadian consistency
    circadian = _circadian_stats(records_14d)

    # Build comparison deltas
    comparison = {}
    key_metrics = [
        "sleep_score",
        "readiness_score",
        "activity_score",
        "sleep_duration_hours",
        "sleep_efficiency",
        "deep_sleep_min",
        "rem_sleep_min",
        "avg_hr",
        "hrv_ms",
        "steps",
    ]
    for metric in key_metrics:
        curr = baselines.get(metric)
        prev = baselines_prev.get(metric)
        if curr and prev and curr.mean is not None and prev.mean is not None:
            delta = curr.mean - prev.mean
            comparison[metric] = {
                "current_avg": round(curr.mean, 1),
                "previous_avg": round(prev.mean, 1),
                "delta": round(delta, 1),
                "trend": "up" if delta > 0 else "down" if delta < 0 else "flat",
            }

    interesting = {k: v for k, v in baselines.items() if v.mean is not None and k in key_metrics}

    # Raw daily values
    daily_values = []
    for r in sorted(records_14d, key=lambda r: r.date):
        entry: dict[str, Any] = {"date": r.date.isoformat()}
        for metric in key_metrics:
            val = getattr(r, metric, None)
            if val is not None:
                entry[metric] = round(float(val), 1) if isinstance(val, int | float) else val
        tags = getattr(r, "sleep_tags", None)
        if tags:
            entry["sleep_tags"] = tags
        negated = getattr(r, "negated_baseline_tags", None)
        if negated:
            entry["negated_tags"] = negated
        w = getattr(r, "hevy_workouts", None)
        if w:
            entry["hevy_workouts"] = w
            vol = getattr(r, "hevy_total_volume_kg", None)
            if vol:
                entry["hevy_volume_kg"] = vol
        daily_values.append(entry)

    significant_alerts = [a for a in alerts if a.severity in ("major", "moderate")]

    output = {
        "report_period": f"{window_start.isoformat()} to {window_end.isoformat()}",
        "days_in_window": len(records_14d),
        "baselines": {
            metric: {
                "mean": round(bs.mean, 1) if bs.mean is not None else None,
                "median": round(bs.median, 1) if bs.median is not None else None,
                "std": round(bs.std, 1) if bs.std is not None else None,
                "min": round(bs.min, 1) if bs.min is not None else None,
                "max": round(bs.max, 1) if bs.max is not None else None,
                "latest": round(bs.latest, 1) if bs.latest is not None else None,
                "trend": bs.trend,
            }
            for metric, bs in interesting.items()
        },
        "daily_values": daily_values,
        "comparison_vs_previous_week": comparison,
        "alerts": [
            {
                "metric": a.metric,
                "date": a.date.isoformat(),
                "value": a.value,
                "severity": a.severity,
                "direction": a.direction,
                "message": a.message,
            }
            for a in significant_alerts[:10]
        ],
        "correlations": [
            {
                "pair": f"{c.metric_a} ↔ {c.metric_b}",
                "description": c.description,
                "r": round(c.r, 3) if c.r is not None else None,
                "strength": c.strength,
                "n": c.n,
            }
            for c in sorted(correlations, key=lambda x: abs(x.r or 0), reverse=True)[:8]
        ],
        "sleep_tags": {
            "negated_baselines": {
                tag: len(dates) for tag, dates in tag_info["negated_baselines"].items()
            },
            "applied_extras": {
                tag: len(dates) for tag, dates in tag_info["applied_extras"].items()
            },
        },
        "hevy": hevy,
        "body_composition": body,
        "circadian": circadian,
    }

    print(json.dumps(output, indent=2, default=str))


if __name__ == "__main__":
    main()
