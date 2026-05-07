"""Morning check-in context builder for LLM-based health briefings.

Produces JSON with all data an LLM needs to build a morning health update.
Works standalone (CLI) or as a Hermes cron job pre-processor.

Usage:
    python -m vital_sync.morning_checkin [--cache PATH]
"""

from __future__ import annotations

import json
import os
from datetime import date, timedelta
from pathlib import Path

from vital_sync.analytics import DailyRecord, load_cache, merge_hevy_into_records, save_cache
from vital_sync.hevy_client import sync_workouts
from vital_sync.oura_client import (
    fetch_daily_activity,
    fetch_daily_readiness,
    fetch_daily_sleep,
    fetch_sleep,
    pull_all,
)


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

    today_sleep = [
        s
        for s in sleep_periods
        if s.get("day") == today or s.get("bedtime_end", "").startswith(today)
    ]
    today_has_data = bool(today_sleep) or today in daily_sleep

    sleep_data: dict = {}
    if today_has_data:
        if today_sleep:
            sp = today_sleep[0]
            duration_h = sp.get("total_sleep_duration", 0) / 3600
            sleep_data = {
                "duration_h": round(duration_h, 2),
                "bed": sp.get("bedtime_start", "")[11:19] if sp.get("bedtime_start") else "?",
                "wake": sp.get("bedtime_end", "")[11:19] if sp.get("bedtime_end") else "?",
                "efficiency": sp.get("efficiency", 0),
                "avg_hr": sp.get("average_heart_rate", 0),
                "lowest_hr": sp.get("lowest_heart_rate", 0),
                "hrv": sp.get("average_hrv", 0),
                "deep_sleep_min": int(
                    sum(p.get("deep_sleep_duration", 0) for p in today_sleep) / 60
                ),
                "rem_sleep_min": int(
                    sum(p.get("rem_sleep_duration", 0) for p in today_sleep) / 60
                ),
                "latency_min": int(sp.get("latency", 0) / 60) if sp.get("latency") else None,
            }
        else:
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
        sleep_data["sleep_score"] = daily_sleep.get(today, {}).get("score")
        sleep_data["readiness"] = daily_readiness.get(today, {}).get("score")
        sleep_data["activity_score"] = daily_activity.get(today, {}).get("score")
    else:
        sleep_data = {"oura_missing": True}

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
    workouts = sync_workouts()
    new_workouts = [w for w in workouts if w.created_at.isoformat()[:10] >= yesterday]
    merged = merge_hevy_into_records(workouts, merged)
    save_cache(merged, str(cache_path))

    # ---- Sleep tags ----
    tags_path = _get_tags_path()
    effective_tags: list[str] = []
    negated_tags: list[str] = []
    if tags_path.exists():
        with open(tags_path) as f:
            tag_data = json.load(f)
        baseline = tag_data.get("baseline_tags", [])
        history = tag_data.get("history", {}).get(today, {})
        negated = history.get("negated", [])
        applied = history.get("applied", [])
        effective_tags = [t for t in baseline if t not in negated] + applied
        negated_tags = negated if negated else []

    # ---- Gap check (last 30 days) ----
    gaps = []
    for i in range(30):
        d = (date.today() - timedelta(days=i)).isoformat()
        has_sleep = any(r.date.isoformat() == d and r.sleep_score is not None for r in merged)
        if not has_sleep:
            gaps.append(d)
    if not today_has_data and today in gaps:
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
        "yesterday_steps": yesterday_steps,
        "trend_7d": trend,
        "hevy_recent_14d": hevy_recent,
    }
    print(json.dumps(output, indent=2, default=str))


if __name__ == "__main__":
    main()
