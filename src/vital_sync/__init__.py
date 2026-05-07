"""vital_sync — Personal health data aggregation and analytics toolkit.

Integrates Oura Ring, Hevy workout app, Renpho scale, and MyNetDiary.
Works standalone (CLI) or as an LLM context provider for AI assistants.

Key modules:
    vital_sync.oura_client     — Oura Ring API v2 client
    vital_sync.hevy_client     — Hevy workout API client
    vital_sync.analytics       — Statistics engine (baselines, deviations, correlations)
    vital_sync.morning_checkin — JSON context builder for daily health briefings
    vital_sync.weekly_report   — JSON context builder for weekly trend reports
    vital_sync.pre_pull_check  — Backup and gap detection before data pulls
    vital_sync.import_csv      — Renpho/MyNetDiary CSV importer
"""

from vital_sync._version import version

__version__ = version
