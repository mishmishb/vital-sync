"""Tests for pre_pull_check.py — backup, gap detection, missing date tracking.

Uses the run() function with configurable paths for isolated testing.
"""

import json
from datetime import date, timedelta

from vital_sync.pre_pull_check import run


def _write_cache(path, records):
    path.write_text(json.dumps(records))


class TestBackup:
    def test_backup_created(self, tmp_path):
        cache_file = tmp_path / "cache.json"
        backup_dir = tmp_path / "backups"
        gaps_file = tmp_path / "gaps.json"
        _write_cache(cache_file, [{"date": "2026-04-28", "sleep_score": 85}])

        run(cache_path=cache_file, backup_dir=backup_dir, gaps_file=gaps_file)

        backups = sorted(backup_dir.glob("cache_*.json"))
        assert len(backups) == 1

    def test_no_cache_no_backup(self, tmp_path):
        cache_file = tmp_path / "cache.json"
        backup_dir = tmp_path / "backups"
        gaps_file = tmp_path / "gaps.json"
        # No cache file written

        run(cache_path=cache_file, backup_dir=backup_dir, gaps_file=gaps_file)

        backups = list(backup_dir.glob("cache_*.json"))
        assert len(backups) == 0

    def test_backup_rotation(self, tmp_path):
        """Old backups beyond 30 are cleaned up."""
        cache_file = tmp_path / "cache.json"
        backup_dir = tmp_path / "backups"
        gaps_file = tmp_path / "gaps.json"
        backup_dir.mkdir()

        # Create 35 fake backup files
        for i in range(35):
            (backup_dir / f"cache_20260428_{i:04d}.json").write_text("[]")

        _write_cache(cache_file, [{"date": "2026-04-28"}])

        run(cache_path=cache_file, backup_dir=backup_dir, gaps_file=gaps_file)

        backups = sorted(backup_dir.glob("cache_*.json"))
        assert len(backups) <= 31


class TestGapDetection:
    def test_no_gaps(self, tmp_path):
        cache_file = tmp_path / "cache.json"
        backup_dir = tmp_path / "backups"
        gaps_file = tmp_path / "gaps.json"
        _write_cache(
            cache_file,
            [
                {"date": "2026-04-26", "sleep_score": 78, "sleep_duration_hours": 7.2},
                {"date": "2026-04-27", "sleep_score": 79, "sleep_duration_hours": 7.4},
                {"date": "2026-04-28", "sleep_score": 85, "sleep_duration_hours": 7.1},
            ],
        )

        run(cache_path=cache_file, backup_dir=backup_dir, gaps_file=gaps_file)

        gaps = json.loads(gaps_file.read_text())
        assert gaps["records_score_only"] == 0
        assert gaps["recent_score_only_needs_recheck"] == []

    def test_score_only_detected(self, tmp_path):
        cache_file = tmp_path / "cache.json"
        backup_dir = tmp_path / "backups"
        gaps_file = tmp_path / "gaps.json"
        _write_cache(
            cache_file,
            [
                {"date": "2026-04-27", "sleep_score": 79, "sleep_duration_hours": None},
                {"date": "2026-04-28", "sleep_score": 85, "sleep_duration_hours": 7.1},
            ],
        )

        run(cache_path=cache_file, backup_dir=backup_dir, gaps_file=gaps_file)

        gaps = json.loads(gaps_file.read_text())
        assert gaps["records_score_only"] == 1
        assert gaps["records_with_full_sleep"] == 1

    def test_recent_score_only_needs_recheck(self, tmp_path):
        cache_file = tmp_path / "cache.json"
        backup_dir = tmp_path / "backups"
        gaps_file = tmp_path / "gaps.json"
        today = date.today()
        yesterday = (today - timedelta(days=1)).isoformat()
        _write_cache(
            cache_file,
            [
                {"date": yesterday, "sleep_score": 80, "sleep_duration_hours": None},
            ],
        )

        run(cache_path=cache_file, backup_dir=backup_dir, gaps_file=gaps_file)

        gaps = json.loads(gaps_file.read_text())
        assert yesterday in gaps["recent_score_only_needs_recheck"]

    def test_old_score_only_not_recent(self, tmp_path):
        """Score-only from >3 days ago should not be in recent list."""
        cache_file = tmp_path / "cache.json"
        backup_dir = tmp_path / "backups"
        gaps_file = tmp_path / "gaps.json"
        old_date = (date.today() - timedelta(days=10)).isoformat()
        _write_cache(
            cache_file,
            [
                {"date": old_date, "sleep_score": 50, "sleep_duration_hours": None},
            ],
        )

        run(cache_path=cache_file, backup_dir=backup_dir, gaps_file=gaps_file)

        gaps = json.loads(gaps_file.read_text())
        assert gaps["records_score_only"] == 1
        assert old_date not in gaps["recent_score_only_needs_recheck"]

    def test_total_records_count(self, tmp_path):
        cache_file = tmp_path / "cache.json"
        backup_dir = tmp_path / "backups"
        gaps_file = tmp_path / "gaps.json"
        _write_cache(
            cache_file,
            [
                {"date": "2026-04-26", "sleep_score": 78},
                {"date": "2026-04-27", "sleep_score": 79},
            ],
        )

        run(cache_path=cache_file, backup_dir=backup_dir, gaps_file=gaps_file)

        gaps = json.loads(gaps_file.read_text())
        assert gaps["total_records"] == 2

    def test_no_cache_graceful(self, tmp_path):
        cache_file = tmp_path / "nonexistent.json"
        backup_dir = tmp_path / "backups"
        gaps_file = tmp_path / "gaps.json"

        run(cache_path=cache_file, backup_dir=backup_dir, gaps_file=gaps_file)
        # Should not raise — just prints a message
        assert not gaps_file.exists()  # Gap file not written when no cache
