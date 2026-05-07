#!/usr/bin/env python3
"""Pre-pull backup and gap detection for health data cache.

Run BEFORE the daily Oura data pull. Creates a timestamped backup
and reports which dates have incomplete data that should be rechecked.

Usage:
    python3 pre_pull_check.py          # run against real cache
    from pre_pull_check import run     # call run() with custom paths for testing
"""

import json
import os
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path

CACHE = Path(
    os.environ.get(
        "VITAL_SYNC_CACHE",
        str(Path.home() / ".vital_sync" / "cache.json"),
    )
)
BACKUP_DIR = CACHE.parent / "backups"
GAPS_FILE = CACHE.parent / "missing_data_gaps.json"


def run(
    cache_path: Path | None = None, backup_dir: Path | None = None, gaps_file: Path | None = None
) -> None:
    """Run backup and gap check with configurable paths (for testing)."""
    c = cache_path or CACHE
    bd = backup_dir or BACKUP_DIR
    gf = gaps_file or GAPS_FILE

    bd.mkdir(parents=True, exist_ok=True)

    # 1. Backup
    if c.exists():
        stamp = datetime.now().strftime("%Y%m%d_%H%M")
        backup = bd / f"cache_{stamp}.json"
        shutil.copy2(c, backup)
        existing = sorted(bd.glob("cache_*.json"))
        for old in existing[:-30]:
            old.unlink()
        print(f"Backed up to {backup}")
    else:
        print("No cache to backup")

    # 2. Gap detection
    if not c.exists():
        print("No cache — skipping gap check")
        return

    with open(c) as f:
        cache = json.load(f)

    records = cache if isinstance(cache, list) else cache.get("records", [])
    today = date.today()

    score_only = []
    for r in records:
        d = r.get("date", "")
        if r.get("sleep_score") is not None and r.get("sleep_duration_hours") is None:
            score_only.append(d)

    existing_dates = set()
    for r in records:
        d = r.get("date", "")
        if d:
            try:
                existing_dates.add(date.fromisoformat(d))
            except Exception:
                pass

    missing_dates = []
    for i in range(30):
        d = today - timedelta(days=i)
        if d not in existing_dates:
            missing_dates.append(d.isoformat())

    recent_score_only = [d for d in score_only if d >= (today - timedelta(days=3)).isoformat()]

    gaps = {
        "checked_at": datetime.now().isoformat(),
        "score_only_no_duration": sorted(score_only),
        "missing_entirely_from_30d": sorted(missing_dates),
        "recent_score_only_needs_recheck": sorted(recent_score_only),
        "total_records": len(records),
        "records_with_full_sleep": sum(1 for r in records if r.get("sleep_duration_hours")),
        "records_score_only": len(score_only),
    }

    with open(gf, "w") as f:
        json.dump(gaps, f, indent=2)

    print(
        f"Gap report: {len(score_only)} score-only, {len(missing_dates)} missing, {len(recent_score_only)} need recheck"
    )
    for d in recent_score_only:
        print(f"  ⚠ {d} has score but no duration — needs backfill")


if __name__ == "__main__":
    run()
