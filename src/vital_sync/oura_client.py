"""
Oura API v2 direct client.

Uses Personal Access Token (no OAuth needed).
API docs: https://cloud.ouraring.com/v2/docs

Endpoints:
- /v2/usercollection/sleep          — sleep periods (duration, stages, timing)
- /v2/usercollection/daily_sleep     — daily sleep scores
- /v2/usercollection/daily_readiness — readiness scores
- /v2/usercollection/daily_activity  — activity scores
"""

from __future__ import annotations

import json
import os
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

OURA_BASE = "https://api.ouraring.com/v2/usercollection"


def _get_token() -> str:
    """Read Oura PAT from environment or .env file."""
    token = os.environ.get("OURA_API_KEY", "")
    if token:
        return token
    # Check local .env first, then ~/.hermes/.env for Hermes compatibility
    for env_path in (Path(".env"), Path.home() / ".hermes" / ".env"):
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("OURA_API_KEY="):
                    token = line.split("=", 1)[1].strip().strip('"').strip("'")
                    return token
    raise RuntimeError("OURA_API_KEY not found in env or .env file")


def _get(endpoint: str, params: dict[str, str] | None = None) -> dict[str, Any]:
    """Make an authenticated GET request to the Oura API."""
    url = f"{OURA_BASE}/{endpoint}"
    if params:
        from urllib.parse import urlencode

        url += "?" + urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {_get_token()}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def fetch_sleep(days: int = 7) -> list[dict[str, Any]]:
    """Fetch sleep periods for the last N days.

    Returns list of sleep period dicts. Each dict has:
    - day: wake-date (ISO date string)
    - type: "long_sleep", "sleep" (nap), "restless", etc.
    - period: segment index within the night
    - total_sleep_duration, deep_sleep_duration, rem_sleep_duration (seconds)
    - efficiency, latency, time_in_bed (seconds)
    - bedtime_start, bedtime_end (ISO datetime with timezone)
    - average_heart_rate, lowest_heart_rate, average_hrv
    """
    start = (date.today() - timedelta(days=days)).isoformat()
    end = (date.today() + timedelta(days=1)).isoformat()
    data = _get("sleep", {"start_date": start, "end_date": end})
    return data.get("data", [])


def fetch_daily_sleep(days: int = 7) -> dict[str, dict[str, Any]]:
    """Fetch daily sleep scores for the last N days."""
    start = (date.today() - timedelta(days=days)).isoformat()
    end = date.today().isoformat()
    data = _get("daily_sleep", {"start_date": start, "end_date": end})
    result = {}
    for item in data.get("data", []):
        result[item["day"]] = item
    return result


def fetch_daily_readiness(days: int = 7) -> dict[str, dict[str, Any]]:
    """Fetch daily readiness scores for the last N days."""
    start = (date.today() - timedelta(days=days)).isoformat()
    end = date.today().isoformat()
    data = _get("daily_readiness", {"start_date": start, "end_date": end})
    result = {}
    for item in data.get("data", []):
        result[item["day"]] = item
    return result


def fetch_daily_activity(days: int = 7) -> dict[str, dict[str, Any]]:
    """Fetch daily activity scores for the last N days."""
    start = (date.today() - timedelta(days=days)).isoformat()
    end = date.today().isoformat()
    data = _get("daily_activity", {"start_date": start, "end_date": end})
    result = {}
    for item in data.get("data", []):
        result[item["day"]] = item
    return result


def sleep_periods_to_records(
    periods: list[dict[str, Any]],
    daily_sleep: dict[str, dict[str, Any]],
    daily_readiness: dict[str, dict[str, Any]] | None = None,
    daily_activity: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Convert Oura sleep periods to daily records keyed by wake-date.

    Groups sleep periods by wake-date (the `day` field). For each day:
    - Sums durations of all periods (main sleep + naps)
    - Uses the LONGEST period's bedtime_start/wake_end for timing
    - Attaches daily score from the daily_sleep endpoint
    - Filters out periods with total_sleep_duration < 3600s (artifacts)

    Returns dict of {date_iso: record_dict}.
    """
    by_day: dict[str, list[dict]] = {}
    for p in periods:
        day = p.get("day")
        if not day:
            continue
        by_day.setdefault(day, []).append(p)

    records: dict[str, dict[str, Any]] = {}

    for day, day_periods in sorted(by_day.items()):
        valid = [p for p in day_periods if (p.get("total_sleep_duration") or 0) >= 3600]

        if not valid:
            record = {"date": day, "sources": ["oura"]}
            if day in daily_sleep:
                record["sleep_score"] = daily_sleep[day].get("score")
            if daily_readiness and day in daily_readiness:
                record["readiness_score"] = daily_readiness[day].get("score")
            if daily_activity and day in daily_activity:
                record["activity_score"] = daily_activity[day].get("score")
                record["steps"] = daily_activity[day].get("steps")
            records[day] = record
            continue

        main = max(valid, key=lambda p: p.get("total_sleep_duration") or 0)
        record: dict[str, Any] = {
            "date": day,
            "sources": ["oura"],
            "sleep_duration_hours": sum((p.get("total_sleep_duration") or 0) for p in valid)
            / 3600,
            "deep_sleep_min": sum((p.get("deep_sleep_duration") or 0) for p in valid) / 60,
            "rem_sleep_min": sum((p.get("rem_sleep_duration") or 0) for p in valid) / 60,
            "sleep_efficiency": main.get("efficiency"),
            "bedtime": _extract_time(main.get("bedtime_start")),
            "wake_time": _extract_time(main.get("bedtime_end")),
            "avg_hr": main.get("average_heart_rate"),
            "lowest_hr": main.get("lowest_heart_rate"),
            "hrv_ms": main.get("average_hrv"),
            "sleep_latency": (main.get("latency") or 0) / 60 if main.get("latency") else None,
            "time_in_bed": main.get("time_in_bed"),
        }

        if day in daily_sleep:
            record["sleep_score"] = daily_sleep[day].get("score")
        if daily_readiness and day in daily_readiness:
            record["readiness_score"] = daily_readiness[day].get("score")
        if daily_activity and day in daily_activity:
            record["activity_score"] = daily_activity[day].get("score")
            record["steps"] = daily_activity[day].get("steps")

        records[day] = record

    return records


def _extract_time(iso_str: str | None) -> str | None:
    """Extract HH:MM:SS from ISO datetime string."""
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.time().isoformat()
    except (ValueError, TypeError):
        return None


def pull_all(days: int = 30) -> dict[str, dict[str, Any]]:
    """Pull all Oura data and return records keyed by date.

    This is the main entry point for fetching Oura data.
    """
    sleep_data = fetch_sleep(days)
    daily_sleep = fetch_daily_sleep(days)
    daily_readiness = fetch_daily_readiness(days)
    daily_activity = fetch_daily_activity(days)

    return sleep_periods_to_records(sleep_data, daily_sleep, daily_readiness, daily_activity)


if __name__ == "__main__":
    records = pull_all(14)
    print(f"Fetched {len(records)} days of Oura data\n")
    for day, r in sorted(records.items()):
        dur = r.get("sleep_duration_hours")
        dur_s = f"{dur:.2f}h" if dur else "  --  "
        score = f"{r.get('sleep_score'):3.0f}" if r.get("sleep_score") else " --"
        bed = r.get("bedtime", "N/A")
        wake = r.get("wake_time", "N/A")
        print(f"  {day}: sleep={dur_s}  score={score}  bed={bed}→{wake}")
