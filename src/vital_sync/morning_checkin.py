"""Morning check-in context builder for LLM-based health briefings.

Produces JSON with all data an LLM needs to build a morning health update.
Works standalone (CLI) or as a Hermes cron job pre-processor.

Usage:
    python -m vital_sync.morning_checkin [--cache PATH]
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path

from vital_sync.analytics import DailyRecord, load_cache, merge_hevy_into_records, save_cache
from vital_sync.hevy_client import HevyWorkout, sync_workouts
from vital_sync.oura_client import (
    fetch_daily_activity,
    fetch_daily_readiness,
    fetch_daily_sleep,
    fetch_sleep,
    fetch_workouts,
    pull_all,
)


def _select_primary_sleep_period(sleep_periods: list[dict], target_day: str) -> dict | None:
    matching_periods = [
        period
        for period in sleep_periods
        if period.get("day") == target_day or period.get("bedtime_end", "").startswith(target_day)
    ]
    if not matching_periods:
        return None

    return max(matching_periods, key=lambda period: period.get("total_sleep_duration", 0) or 0)


def _build_sleep_data(
    *,
    today: str,
    yesterday: str,
    sleep_periods: list[dict],
    daily_sleep: dict,
    daily_readiness: dict,
    daily_activity: dict,
) -> dict:
    primary_sleep = _select_primary_sleep_period(sleep_periods, today)
    today_has_data = primary_sleep is not None or today in daily_sleep

    if not today_has_data:
        return {"oura_missing": True}

    if primary_sleep is None:
        sleep_data = {
            "duration_h": 0,
            "bed": "?",
            "wake": "?",
            "efficiency": 0,
            "avg_hr": 0,
            "lowest_hr": 0,
            "hrv": 0,
            "deep_sleep_min": 0,
            "rem_sleep_min": 0,
            "latency_min": None,
        }
    else:
        duration_h = (primary_sleep.get("total_sleep_duration", 0) or 0) / 3600
        sleep_data = {
            "duration_h": round(duration_h, 2),
            "bed": (
                primary_sleep.get("bedtime_start", "")[11:19]
                if primary_sleep.get("bedtime_start")
                else "?"
            ),
            "wake": (
                primary_sleep.get("bedtime_end", "")[11:19]
                if primary_sleep.get("bedtime_end")
                else "?"
            ),
            "efficiency": primary_sleep.get("efficiency", 0),
            "avg_hr": primary_sleep.get("average_heart_rate", 0),
            "lowest_hr": primary_sleep.get("lowest_heart_rate", 0),
            "hrv": primary_sleep.get("average_hrv", 0),
            "deep_sleep_min": int((primary_sleep.get("deep_sleep_duration", 0) or 0) / 60),
            "rem_sleep_min": int((primary_sleep.get("rem_sleep_duration", 0) or 0) / 60),
            "latency_min": (
                int((primary_sleep.get("latency", 0) or 0) / 60)
                if primary_sleep.get("latency")
                else None
            ),
        }

    sleep_data["sleep_score"] = daily_sleep.get(today, {}).get("score")
    sleep_data["readiness"] = daily_readiness.get(today, {}).get("score")
    sleep_data["activity_score"] = daily_activity.get(yesterday, {}).get("score")
    return sleep_data


def _dedupe_tags(tags: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped = []
    for tag in tags:
        if tag not in seen:
            seen.add(tag)
            deduped.append(tag)
    return deduped


def _build_effective_tags(
    *, tag_data: dict, record_tags: list[str] | None, target_day: str
) -> tuple[list[str], list[str]]:
    baseline = tag_data.get("baseline_tags", [])
    history = tag_data.get("history", {}).get(target_day, {})
    negated = history.get("negated", [])
    applied = history.get("applied", [])

    effective_tags = [tag for tag in baseline if tag not in negated]
    effective_tags.extend(applied)
    effective_tags.extend(tag for tag in (record_tags or []) if tag not in negated)
    return _dedupe_tags(effective_tags), negated if negated else []


def _seconds_to_minutes(value: int | float | None) -> int | None:
    if value is None:
        return None
    return int(round(float(value) / 60))


def _build_oura_workout_summary(workout: dict) -> dict:
    start = datetime.fromisoformat(workout["start_datetime"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(workout["end_datetime"].replace("Z", "+00:00"))
    duration_min = int(round((end - start).total_seconds() / 60))
    return {
        "activity": workout.get("activity"),
        "intensity": workout.get("intensity"),
        "duration_min": duration_min,
        "calories": round(workout.get("calories", 0)),
        "distance_m": round(workout.get("distance", 0)),
    }


def _normalise_activity_name(value: str | None) -> str:
    if not value:
        return ""
    return "".join(ch for ch in value.lower() if ch.isalnum())


_NON_STRENGTH_OURA_ACTIVITIES = {
    "walking",
    "cycling",
    "running",
    "swimming",
    "housework",
    "dance",
    "hiking",
    "rowing",
    "elliptical",
    "stairclimbing",
    "yoga",
    "pilates",
}

_STRENGTH_ACTIVITY_KEYWORDS = (
    "strength",
    "weight",
    "resistance",
    "gym",
    "crossfit",
    "functional",
    "bodybuilding",
)


def _oura_workout_is_strength_like(workout: dict) -> bool:
    activity = _normalise_activity_name(workout.get("activity"))
    label = _normalise_activity_name(workout.get("label"))
    combined = f"{activity} {label}"
    if any(keyword in combined for keyword in _STRENGTH_ACTIVITY_KEYWORDS):
        return True
    if activity in _NON_STRENGTH_OURA_ACTIVITIES:
        return False

    distance = float(workout.get("distance") or 0)
    return distance <= 250


def _workout_times_overlap(oura_workout: dict, hevy_workout: HevyWorkout) -> bool:
    oura_start = datetime.fromisoformat(oura_workout["start_datetime"].replace("Z", "+00:00"))
    oura_end = datetime.fromisoformat(oura_workout["end_datetime"].replace("Z", "+00:00"))
    latest_start = max(oura_start, hevy_workout.start_time)
    earliest_end = min(oura_end, hevy_workout.end_time)
    overlap_seconds = (earliest_end - latest_start).total_seconds()
    if overlap_seconds > 0:
        return True

    start_delta_seconds = abs((oura_start - hevy_workout.start_time).total_seconds())
    end_delta_seconds = abs((oura_end - hevy_workout.end_time).total_seconds())
    return start_delta_seconds <= 900 and end_delta_seconds <= 900


def _is_duplicate_of_hevy_workout(workout: dict, hevy_workouts: list[HevyWorkout]) -> bool:
    if not _oura_workout_is_strength_like(workout):
        return False
    return any(_workout_times_overlap(workout, hevy_workout) for hevy_workout in hevy_workouts)


def _build_activity_data(
    *,
    yesterday: str,
    daily_activity: dict,
    workouts: list[dict],
    hevy_workouts: list[HevyWorkout] | None = None,
) -> dict:
    yesterday_activity = daily_activity.get(yesterday, {})
    yesterday_hevy_workouts = [
        workout
        for workout in (hevy_workouts or [])
        if workout.start_time.date().isoformat() == yesterday
    ]
    yesterday_oura_workouts = [workout for workout in workouts if workout.get("day") == yesterday]

    filtered_oura_workouts = []
    suppressed_oura_duplicates = 0
    for workout in yesterday_oura_workouts:
        if _is_duplicate_of_hevy_workout(workout, yesterday_hevy_workouts):
            suppressed_oura_duplicates += 1
            continue
        filtered_oura_workouts.append(_build_oura_workout_summary(workout))

    return {
        "score": yesterday_activity.get("score"),
        "steps": yesterday_activity.get("steps"),
        "active_calories": yesterday_activity.get("active_calories"),
        "equivalent_walking_distance_m": yesterday_activity.get("equivalent_walking_distance"),
        "medium_activity_min": _seconds_to_minutes(yesterday_activity.get("medium_activity_time")),
        "high_activity_min": _seconds_to_minutes(yesterday_activity.get("high_activity_time")),
        "inactivity_alerts": yesterday_activity.get("inactivity_alerts"),
        "contributors": yesterday_activity.get("contributors") or {},
        "oura_workouts": filtered_oura_workouts,
        "suppressed_oura_duplicates": suppressed_oura_duplicates,
    }


def _get_cache_path() -> Path:
    return Path(
        os.environ.get(
            "VITAL_SYNC_CACHE",
            str(Path.home() / ".vital_sync" / "cache.json"),
        )
    )


def _get_tags_path() -> Path:
    return Path(
        os.environ.get(
            "VITAL_SYNC_SLEEP_TAGS",
            str(Path.home() / ".vital_sync" / "sleep_tags.json"),
        )
    )


def main() -> None:
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    cache_path = _get_cache_path()

    # ---- Oura live pull ----
    sleep_periods = fetch_sleep(days=2)
    daily_sleep = fetch_daily_sleep(days=2)
    daily_readiness = fetch_daily_readiness(days=2)
    daily_activity = fetch_daily_activity(days=2)
    oura_workouts = fetch_workouts(days=2)

    sleep_data = _build_sleep_data(
        today=today,
        yesterday=yesterday,
        sleep_periods=sleep_periods,
        daily_sleep=daily_sleep,
        daily_readiness=daily_readiness,
        daily_activity=daily_activity,
    )

    # ---- Update cache with Oura data ----
    old_records = load_cache(str(cache_path))
    old_by_date = {r.date.isoformat(): r for r in old_records}
    oura_records = pull_all(days=2)
    preserve = [
        "mood",
        "anxiety",
        "irritability",
        "task_init",
        "task_switch",
        "stop_working",
        "social_patience",
        "appetite",
        "evening_crash",
        "bp_sys",
        "bp_dia",
        "morning_grogginess",
        "hevy_workouts",
        "hevy_total_volume_kg",
        "hevy_total_duration_min",
        "hevy_muscle_groups",
        "hevy_max_weight_kg",
        "hevy_avg_rpe",
        "notes",
        "sleep_tags",
        "negated_baseline_tags",
        "calories_in",
        "protein_g",
        "body_fat_pct",
        "weight_kg",
        "muscle_mass_kg",
    ]
    merged = []
    for day, oura_r in sorted(oura_records.items()):
        r = DailyRecord(date=date.fromisoformat(day))
        for k, v in oura_r.items():
            if k != "date" and v is not None:
                setattr(r, k, v)
        if day in old_by_date:
            old = old_by_date[day]
            for attr in preserve:
                old_val = getattr(old, attr, None)
                if old_val is not None and old_val != [] and old_val != "":
                    setattr(r, attr, old_val)
            old_sources = getattr(old, "sources", []) or []
            if "oura" not in old_sources:
                old_sources.append("oura")
            r.sources = old_sources
        merged.append(r)
    oura_days = set(oura_records.keys())
    for old in old_records:
        if old.date.isoformat() not in oura_days:
            merged.append(old)
    merged.sort(key=lambda r: r.date)

    # ---- Hevy sync + merge ----
    hevy_workouts = sync_workouts()
    new_workouts = [w for w in hevy_workouts if w.created_at.isoformat()[:10] >= yesterday]
    merged = merge_hevy_into_records(hevy_workouts, merged)
    save_cache(merged, str(cache_path))

    activity_data = _build_activity_data(
        yesterday=yesterday,
        daily_activity=daily_activity,
        workouts=oura_workouts,
        hevy_workouts=hevy_workouts,
    )

    # ---- Sleep tags ----
    tags_path = _get_tags_path()
    effective_tags: list[str] = []
    negated_tags: list[str] = []
    today_record_tags: list[str] = []
    for record in merged:
        if record.date.isoformat() == today:
            today_record_tags = getattr(record, "sleep_tags", []) or []
            break
    if tags_path.exists():
        with open(tags_path) as f:
            tag_data = json.load(f)
        effective_tags, negated_tags = _build_effective_tags(
            tag_data=tag_data,
            record_tags=today_record_tags,
            target_day=today,
        )
    else:
        effective_tags = _dedupe_tags(today_record_tags)

    # ---- Gap check (last 30 days) ----
    gaps = []
    for i in range(30):
        d = (date.today() - timedelta(days=i)).isoformat()
        has_sleep = any(r.date.isoformat() == d and r.sleep_score is not None for r in merged)
        if not has_sleep:
            gaps.append(d)
    if sleep_data.get("oura_missing") and today in gaps:
        gaps.remove(today)

    # ---- Yesterday's steps ----
    yesterday_steps = None
    for r in merged:
        if r.date.isoformat() == yesterday:
            yesterday_steps = getattr(r, "steps", None)
            break

    # ---- 7-day trend ----
    trend = []
    for i in range(7):
        d = (date.today() - timedelta(days=i)).isoformat()
        for r in merged:
            if r.date.isoformat() == d:
                trend.append(
                    {
                        "date": d,
                        "sleep_score": r.sleep_score,
                        "readiness": r.readiness_score,
                        "sleep_duration_h": round(r.sleep_duration_hours, 1)
                        if r.sleep_duration_hours
                        else None,
                        "hrv": r.hrv_ms,
                        "rhr": r.avg_hr,
                    }
                )
                break
        else:
            trend.append({"date": d, "no_data": True})

    # ---- Hevy recent (14 days) ----
    hevy_recent = []
    for i in range(14):
        d = (date.today() - timedelta(days=i)).isoformat()
        for r in merged:
            if r.date.isoformat() == d:
                w = getattr(r, "hevy_workouts", None)
                if w:
                    hevy_recent.append(
                        {
                            "date": d,
                            "workouts": w,
                            "volume_kg": getattr(r, "hevy_total_volume_kg", None),
                            "duration_min": getattr(r, "hevy_total_duration_min", None),
                            "muscle_groups": getattr(r, "hevy_muscle_groups", []),
                        }
                    )
                break

    output = {
        "today": today,
        "yesterday": yesterday,
        "sleep": sleep_data,
        "hevy_new_workouts": len(new_workouts),
        "effective_tags": effective_tags,
        "negated_tags": negated_tags,
        "gaps": gaps,
        "activity": activity_data,
        "yesterday_steps": yesterday_steps,
        "trend_7d": trend,
        "hevy_recent_14d": hevy_recent,
    }
    print(json.dumps(output, indent=2, default=str))


if __name__ == "__main__":
    main()
