"""Morning check-in context builder for LLM-based health briefings.

Produces JSON with all data an LLM needs to build a morning health update.
Works standalone (CLI) or as a Hermes cron job pre-processor.

Usage:
    python -m vital_sync.morning_checkin [--cache PATH]
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path

from vital_sync import paths
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
from vital_sync.sleep_tags import (
    build_sleep_tags_for_day,
    migrate_legacy_sleep_tags,
    normalise_canonical_tags,
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

    # If neither score is available, Oura hasn't synced meaningful data for today.
    # A nap or partial segment with no computed scores does not count as valid data.
    if sleep_data["sleep_score"] is None and sleep_data["readiness"] is None:
        return {"oura_missing": True}

    return sleep_data


def _parse_clock_time(value: str | None) -> time | None:
    if not value:
        return None
    normalised = value.strip()
    if not normalised:
        return None
    if "T" in normalised:
        normalised = normalised.split("T", 1)[1]
    if "+" in normalised:
        normalised = normalised.split("+", 1)[0]
    if len(normalised) == 5:
        normalised = f"{normalised}:00"
    try:
        return time.fromisoformat(normalised)
    except ValueError:
        return None


def _early_wake_cutoff(config: dict | None, default: str) -> str | None:
    early_wake_config = (config or {}).get("sleep_tags", {}).get("early_wake", {})
    if early_wake_config.get("enabled") is not True:
        return None
    return early_wake_config.get("before", default)


def _wake_is_before_cutoff(record: DailyRecord, cutoff: str) -> bool:
    wake_time = _parse_clock_time(record.wake_time)
    cutoff_time = _parse_clock_time(cutoff)
    return wake_time is not None and cutoff_time is not None and wake_time < cutoff_time


def _ensure_early_wake_canonical_tag(tag_data: dict) -> bool:
    changed = normalise_canonical_tags(tag_data)
    canonical_tags = tag_data.setdefault("canonical_tags", {})
    description = "auto-added when wake time is before configured cutoff"
    if canonical_tags.get("early wake") == description:
        return changed
    canonical_tags["early wake"] = description
    return True


def _auto_add_early_wake_tag(
    *,
    record: DailyRecord | None,
    tag_data: dict,
    target_day: str,
    config: dict | None = None,
    cutoff: str = "09:30:00",
) -> bool:
    if record is None:
        return False

    configured_cutoff = _early_wake_cutoff(config, cutoff)
    if configured_cutoff is None or not _wake_is_before_cutoff(record, configured_cutoff):
        return False

    changed = False
    if "early wake" not in record.sleep_tags:
        record.sleep_tags.append("early wake")
        changed = True

    changed = _ensure_early_wake_canonical_tag(tag_data) or changed
    day_tags, seeded = build_sleep_tags_for_day(
        tag_data=tag_data,
        record_tags=record.sleep_tags,
        target_day=target_day,
    )
    changed = changed or seeded
    if "early wake" not in day_tags:
        day_tags.append("early wake")
        tag_data.setdefault("tags_by_date", {})[target_day] = day_tags
        changed = True

    return changed


def _build_sleep_tags_for_day(
    *, tag_data: dict, record_tags: list[str] | None, target_day: str
) -> tuple[list[str], bool]:
    return build_sleep_tags_for_day(tag_data, target_day, record_tags)


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
        "calories": round(workout.get("calories") or 0),
        "distance_m": round(workout.get("distance") or 0),
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
    return paths.cache_path()


def _get_tags_path() -> Path:
    return paths.sleep_tags_path()


def _get_config_path() -> Path:
    return paths.config_path()


def _load_config() -> dict:
    config_path = _get_config_path()
    if not config_path.exists():
        return {}
    with open(config_path) as f:
        return json.load(f)


def _process_sleep_tags(merged: list[DailyRecord], today: str, cache_path: Path) -> list[str]:
    """Handle sleep tag migration, early-wake auto-tag, and effective tags.

    Returns the effective tags for today.
    """
    config = _load_config()
    tags_path = _get_tags_path()
    tag_data: dict = {"baseline_tags": [], "canonical_tags": {}, "tags_by_date": {}}
    if tags_path.exists():
        with open(tags_path) as f:
            tag_data = json.load(f)

    if tags_path.exists() and migrate_legacy_sleep_tags(
        tag_data, [record.date.isoformat() for record in merged]
    ):
        tags_path.parent.mkdir(parents=True, exist_ok=True)
        tags_path.write_text(json.dumps(tag_data, indent=2) + "\n")

    today_record: DailyRecord | None = None
    for record in merged:
        if record.date.isoformat() == today:
            today_record = record
            break

    if _auto_add_early_wake_tag(
        record=today_record, tag_data=tag_data, target_day=today, config=config
    ):
        tags_path.parent.mkdir(parents=True, exist_ok=True)
        tags_path.write_text(json.dumps(tag_data, indent=2) + "\n")

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    save_cache(merged, str(cache_path))

    effective_tags: list[str] = []
    today_record_tags = getattr(today_record, "sleep_tags", []) if today_record else []
    if tags_path.exists() or today_record_tags:
        effective_tags, tags_changed = build_sleep_tags_for_day(
            tag_data=tag_data,
            record_tags=today_record_tags,
            target_day=today,
        )
        if tags_changed:
            tags_path.parent.mkdir(parents=True, exist_ok=True)
            tags_path.write_text(json.dumps(tag_data, indent=2) + "\n")

    return effective_tags


def main() -> None:
    paths.ensure_data_files()
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

    # ---- Sleep tags ----
    effective_tags = _process_sleep_tags(merged, today, cache_path)

    # ---- Activity summary ----
    activity_data = _build_activity_data(
        yesterday=yesterday,
        daily_activity=daily_activity,
        workouts=oura_workouts,
        hevy_workouts=hevy_workouts,
    )

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
        "gaps": gaps,
        "activity": activity_data,
        "yesterday_steps": yesterday_steps,
        "trend_7d": trend,
        "hevy_recent_14d": hevy_recent,
    }
    print(json.dumps(output, indent=2, default=str))


if __name__ == "__main__":
    main()
