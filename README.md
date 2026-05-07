# vital-sync

**Personal health data aggregation and analytics toolkit.**

Integrates Oura Ring, Hevy workout app, Renpho scale, and MyNetDiary into a local cache with statistical analysis. Works standalone via CLI or as a context provider for LLM-based health assistants (Hermes, ChatGPT, Claude).

[![PyPI version](https://img.shields.io/pypi/v/vital-sync)](https://pypi.org/project/vital-sync/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

## Features

- **Oura Ring v2 API client** — sleep periods, daily scores (sleep/readiness/activity), HRV, heart rate, SpO₂
- **Hevy workout API client** — workout sync, exercise tracking, 1RM estimation, muscle group analysis
- **Statistics engine** — rolling baselines, hybrid deviation detection (adaptive z-scores + absolute floors), Pearson correlations, circadian consistency
- **LLM context builders** — pre-processed JSON output for morning check-ins and weekly trend reports
- **CSV import** — Renpho body composition and MyNetDiary nutrition data *(manual: export CSV from each app, then `vital-sync-import`)*
- **Zero external dependencies** — pure Python stdlib

## Quick Start

```bash
pip install vital-sync
```

Set your API keys as environment variables:

```bash
export OURA_API_KEY="your-oura-personal-access-token"
export HEVY_API_KEY="your-hevy-api-key"  # requires Hevy Pro subscription
```

Or create a `.env` file in your working directory with the same variables.

### Fetch your sleep data

```python
from vital_sync.oura_client import pull_all

records = pull_all(days=7)
for date, data in sorted(records.items()):
    print(f"{date}: {data.get('sleep_duration_hours', '?'):.1f}h, score={data.get('sleep_score')}")
```

### Analyse workout trends

```python
from vital_sync.hevy_client import sync_workouts, workout_summary

workouts = sync_workouts()
summary = workout_summary(workouts, days=14)
print(f"Sessions: {summary['workouts']}, Volume: {summary['total_volume_kg']}kg")
```

### Generate a morning check-in JSON

```bash
vital-sync-morning
```

Outputs a structured JSON object with today's sleep data, 7-day trends, recent workouts, and gap detection — ready for an LLM to interpret.

### Generate a weekly health report JSON

```bash
vital-sync-weekly
```

Outputs comprehensive analysis: baselines vs previous week, correlations (e.g. training volume → sleep quality), circadian consistency, and detected deviations.

## Architecture

```
vital_sync/
├── oura_client.py      # Oura Ring API v2
├── hevy_client.py      # Hevy workout API
├── analytics.py        # Statistics engine
├── morning_checkin.py  # Daily context builder
├── weekly_report.py    # Weekly trend report
├── pre_pull_check.py   # Backup + gap detection
├── import_csv.py       # Renpho/MyNetDiary CSV import
└── sleep_tags.py       # Nightly factor tracking
```

Data is stored as a local JSON cache at `~/.vital_sync/cache.json` (configurable via `VITAL_SYNC_CACHE` env var).

## Hermes Agent Integration

vital-sync is the health data backend for [Hermes Agent](https://hermes-agent.nousresearch.com/docs) but works with any LLM pipeline. The `morning_checkin` and `weekly_report` modules output structured JSON — Hermes cron jobs run them and inject the output as LLM context.

### Setup

```bash
pip install vital-sync
export OURA_API_KEY="..." HEVY_API_KEY="..."
# Initialise the cache with a full pull
python -m vital_sync.pre_pull_check
```

### Morning check-in (daily at 11:00)

Creates a Hermes cron job that runs `vital-sync-morning`, producing JSON with last night's sleep, 7-day trend, workout recaps, and gap detection. Hermes injects this into your morning health prompt:

```bash
# The script output (JSON) becomes LLM context for the morning check-in prompt
hermes cronjob create \
  --schedule "0 11 * * *" \
  --name "morning-health-checkin" \
  --script "$(which vital-sync-morning)" \
  --no-agent  # raw script output delivered as context
```

### Weekly report (Sunday at 20:00)

```bash
hermes cronjob create \
  --schedule "0 20 * * 0" \
  --name "weekly-health-report" \
  --script "$(which vital-sync-weekly)" \
  --no-agent
```

### Daily data pull (10:00)

Runs the backup + gap check, pulls fresh Oura data, syncs Hevy workouts, and merges into cache:

```bash
# Create a wrapper script that runs daily pull + merge
cat > ~/.vital_sync/pull.sh << 'EOF'
#!/bin/bash
source ~/.hermes/.env
set -e
python -m vital_sync.pre_pull_check
python -c "
from vital_sync.oura_client import pull_all
from vital_sync.hevy_client import sync_workouts
from vital_sync.analytics import load_cache, save_cache, merge_hevy_into_records
import os
cache = os.environ.get('VITAL_SYNC_CACHE', os.path.expanduser('~/.vital_sync/cache.json'))
records = load_cache(cache)
new_data = pull_all(days=2)
# Merge Oura into records
from datetime import date
from vital_sync.analytics import DailyRecord
for day_str, data in sorted(new_data.items()):
    r = DailyRecord(date=date.fromisoformat(day_str))
    for k, v in data.items():
        if k != 'date' and v is not None:
            setattr(r, k, v)
    replaced = False
    for i, existing in enumerate(records):
        if existing.date.isoformat() == day_str:
            r.sources = list(set(getattr(existing, 'sources', []) + ['oura']))
            for field in ['mood','notes','sleep_tags','calories_in','protein_g','body_fat_pct','weight_kg','hevy_workouts','hevy_total_volume_kg']:
                if getattr(existing, field, None) is not None:
                    setattr(r, field, getattr(existing, field))
            records[i] = r
            replaced = True
            break
    if not replaced:
        records.append(r)
workouts = sync_workouts()
records = merge_hevy_into_records(workouts, records)
records.sort(key=lambda r: r.date)
save_cache(records, cache)
print(f'Cache updated: {len(records)} records')
"
EOF
chmod +x ~/.vital_sync/pull.sh

hermes cronjob create \
  --schedule "0 10 * * *" \
  --name "health-data-pull" \
  --script ~/.vital_sync/pull.sh \
  --no-agent
```

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `OURA_API_KEY` | — | Oura Personal Access Token |
| `HEVY_API_KEY` | — | Hevy API key |
| `VITAL_SYNC_CACHE` | `~/.vital_sync/cache.json` | Cache file path |
| `VITAL_SYNC_DATA_DIR` | `~/.vital_sync/` | Data directory |
| `VITAL_SYNC_SLEEP_TAGS` | `~/.vital_sync/sleep_tags.json` | Sleep tags file |

## Cache Schema

```json
{
  "date": "2026-04-27",
  "sleep_score": 79.0,
  "readiness_score": 80.0,
  "sleep_duration_hours": 7.41,
  "sleep_efficiency": 88.0,
  "deep_sleep_min": 123.0,
  "rem_sleep_min": 82.5,
  "avg_hr": 60.12,
  "lowest_hr": 54.0,
  "hrv_ms": 32.0,
  "steps": 8500,
  "bedtime": "23:42:29",
  "wake_time": "08:06:17",
  "hevy_workouts": 1,
  "hevy_total_volume_kg": 4520.0,
  "hevy_muscle_groups": ["chest", "triceps", "shoulders"],
  "sources": ["oura", "hevy"]
}
```

## Development

```bash
git clone https://github.com/mishmishb/vital-sync.git
cd vital-sync
pip install -e ".[dev]"
pytest
```

## License

MIT — see [LICENSE](LICENSE).

## Author

Built by [mishmishb](https://github.com/mishmishb) as part of a personal health analytics pipeline. Contributions welcome.
