#!/usr/bin/env bash
# Hermes Agent integration: example cron setup for vital-sync
#
# Copy the relevant sections into your Hermes cron-job definitions.
# Each block shows the complete command for a single cron job.

set -euo pipefail

DATA_DIR="${VITAL_SYNC_DATA_DIR:-$HOME/.vital_sync}"

# ─────────────────────────────────────────────────────────────
# 1. Daily data pull + Hevy sync
#    Schedule: 0 10 * * *  (10:00 daily)
# ─────────────────────────────────────────────────────────────
# In Hermes: cronjob create --schedule "0 10 * * *" --name "health-data-pull" --script path/to/pull_health_data.sh
PULL_SCRIPT="${DATA_DIR}/pull_health_data.sh"
cat > "$PULL_SCRIPT" << 'PYEOF'
#!/usr/bin/env python3
"""Daily health data pull: Oura sleep + Hevy workouts → cache."""
from vital_sync.oura_client import pull_all
from vital_sync.hevy_client import sync_workouts
from vital_sync.analytics import load_cache, save_cache, merge_hevy_into_records
from vital_sync.pre_pull_check import run
import os

cache_path = os.environ.get("VITAL_SYNC_CACHE", os.path.expanduser("~/.vital_sync/cache.json"))

# Backup + gap check
run()

# Oura pull
new_data = pull_all(days=2)
records = load_cache(cache_path)

# Merge Oura into cache
from vital_sync.analytics import DailyRecord
from datetime import date
for day_str, data in new_data.items():
    r = DailyRecord(date=date.fromisoformat(day_str))
    for k, v in data.items():
        if k != "date" and v is not None:
            setattr(r, k, v)
    # Update or append
    replaced = False
    for i, existing in enumerate(records):
        if existing.date.isoformat() == day_str:
            # Preserve non-Oura fields
            for field in ["mood", "anxiety", "irritability", "notes", "sleep_tags",
                          "calories_in", "protein_g", "body_fat_pct", "weight_kg",
                          "hevy_workouts", "hevy_total_volume_kg"]:
                old_val = getattr(existing, field, None)
                if old_val is not None:
                    setattr(r, field, old_val)
            r.sources = list(set(getattr(existing, "sources", []) + ["oura"]))
            records[i] = r
            replaced = True
            break
    if not replaced:
        records.append(r)

# Hevy sync + merge
workouts = sync_workouts()
records = merge_hevy_into_records(workouts, records)
records.sort(key=lambda r: r.date)

save_cache(records, cache_path)
print(f"Cache updated: {len(records)} records")
PYEOF
chmod +x "$PULL_SCRIPT"

# ─────────────────────────────────────────────────────────────
# 2. Morning check-in context builder
#    Schedule: 0 11 * * *  (11:00 daily)
# ─────────────────────────────────────────────────────────────
# In Hermes: cronjob create --schedule "0 11 * * *" --name "morning-health-checkin" --prompt "..."
# The prompt should instruct Hermes: "Read the JSON output of vital_sync.morning_checkin
# and present a concise morning health update. See the vital-sync skill for the format."
# Then load the vital-sync skill and the health-data-query skill.

# ─────────────────────────────────────────────────────────────
# 3. Weekly trend report
#    Schedule: 0 20 * * 0  (Sunday 20:00)
# ─────────────────────────────────────────────────────────────
# In Hermes: cronjob create --schedule "0 20 * * 0" --name "weekly-health-report" --prompt "..."
# The prompt should instruct Hermes: "Read the JSON output of vital_sync.weekly_report
# and draft a narrative health trend report. See the vital-sync skill for the format."

echo "vital-sync Hermes integration scripts written to: $DATA_DIR"
echo ""
echo "Next steps:"
echo "  1. Set VITAL_SYNC_CACHE env var if not using default (~/.vital_sync/cache.json)"
echo "  2. Ensure OURA_API_KEY and HEVY_API_KEY are in ~/.hermes/.env"
echo "  3. Run '$PULL_SCRIPT' once to initialise the cache"
echo "  4. Create Hermes cron jobs using the examples above"
