"""
Tests for dict record handling.
Validates that key functions accept plain dicts (as from cache.json)
alongside DailyRecord objects, and do not crash.

BUG DISCOVERED: compute_baseline line 260 computes end_date with
`max((r.date for r in records), ...)` BEFORE reaching the dict
handling code at lines 267-273. This causes AttributeError on dicts.
"""

from datetime import date

import pytest

from vital_sync.analytics import (
    DailyRecord,
    compute_all_baselines,
    compute_baseline,
    detect_deviations,
    detect_deviations_adaptive,
    detect_deviations_hybrid,
    merge_records,
)

# ── compute_baseline with dicts ──────────────────────────────────────────────


class TestComputeBaselineDicts:
    """compute_baseline has dict handling at line 267 but crashes earlier."""

    def test_dict_records_work_without_explicit_end_date(self):
        """compute_baseline works with dict records without explicit end_date.

        Previously crashed at line 260 because end_date was computed with
        `max((r.date for r in records), ...)` before dict handling.
        Now uses _record_date helper that handles both dicts and objects.
        """
        records = [
            {"date": "2026-04-20", "sleep_score": 85},
            {"date": "2026-04-21", "sleep_score": 82},
            {"date": "2026-04-22", "sleep_score": 88},
            {"date": "2026-04-23", "sleep_score": 80},
        ]
        stats = compute_baseline(records, "sleep_score", window_days=4)
        assert stats is not None
        assert stats.n == 4
        assert stats.mean == pytest.approx(83.75, abs=0.01)

    def test_works_with_explicit_end_date(self):
        """Providing end_date explicitly avoids line 260 crash.

        When end_date is provided, the genexpr at line 260 is skipped
        and the dict handling at lines 267-273 works correctly.
        This is a PARTIAL workaround.
        """
        records = [
            {"date": "2026-04-20", "sleep_score": 85},
            {"date": "2026-04-21", "sleep_score": 82},
            {"date": "2026-04-22", "sleep_score": 88},
            {"date": "2026-04-23", "sleep_score": 80},
        ]
        # With explicit end_date, line 260 is skipped
        stats = compute_baseline(records, "sleep_score", window_days=4, end_date=date(2026, 4, 23))
        assert stats is not None
        assert stats.n == 4
        assert stats.mean == pytest.approx(83.75, abs=0.01)

    def test_accepts_dict_with_date_objects_and_end_date(self):
        """dict records with date objects work when end_date is explicit."""
        records = [
            {"date": date(2026, 4, 20), "sleep_score": 85},
            {"date": date(2026, 4, 21), "sleep_score": 82},
        ]
        stats = compute_baseline(records, "sleep_score", window_days=2, end_date=date(2026, 4, 21))
        assert stats is not None
        assert stats.n == 2

    def test_mixed_dict_and_record_with_end_date(self):
        """Mixed list works with explicit end_date."""
        records = [
            {"date": "2026-04-20", "sleep_score": 85},
            DailyRecord(date=date(2026, 4, 21), sleep_score=82),
            {"date": "2026-04-22", "sleep_score": 88},
        ]
        stats = compute_baseline(records, "sleep_score", window_days=3, end_date=date(2026, 4, 22))
        assert stats is not None
        assert stats.n == 3

    def test_non_isoformat_date_dict_parses_with_fallback(self):
        """dict with non-ISO date string is parsed via fallback format.

        Previously crashed because date.fromisoformat(r['date']) had no
        try/except. Now _record_date tries %m/%d/%Y as a fallback.
        """
        records = [
            {"date": "04/20/2026", "sleep_score": 85},
        ]
        stats = compute_baseline(records, "sleep_score", window_days=1, end_date=date(2026, 4, 20))
        assert stats is not None
        assert stats.n == 1
        assert stats.mean == 85


# ── merge_records with dicts ─────────────────────────────────────────────────


class TestMergeRecordsDicts:
    """merge_records has explicit dict handling (line 870-877)."""

    def test_merge_dict_records(self):
        """merge_records merges dict records correctly."""
        dict_records = [
            {"date": "2026-04-20", "weight_kg": 80.5, "sources": ["renpho"]},
            {"date": "2026-04-21", "weight_kg": 80.2, "sources": ["renpho"]},
        ]
        oura_records = [
            DailyRecord(date=date(2026, 4, 20), sleep_score=85),
            DailyRecord(date=date(2026, 4, 21), sleep_score=82),
        ]
        merged = merge_records(dict_records, oura_records)
        assert len(merged) == 2
        r0 = merged[0]
        assert r0.date == date(2026, 4, 20)
        assert r0.sleep_score == 85
        assert r0.weight_kg == 80.5

    def test_merge_pure_dicts(self):
        """merge_records with only dict records produces DailyRecord objects."""
        records = [
            {"date": "2026-04-20", "sleep_score": 85, "sources": ["oura"]},
            {"date": "2026-04-21", "sleep_score": 82, "sources": ["oura"]},
        ]
        merged = merge_records(records)
        assert len(merged) == 2
        assert all(isinstance(r, DailyRecord) for r in merged)
        assert merged[0].sleep_score == 85


# ── detect_deviations with dicts ─────────────────────────────────────────────


class TestDetectDeviationsDicts:
    """detect_deviations does NOT have dict handling — crashes via compute_all_baselines."""

    def test_compute_all_baselines_works_with_dicts(self, sample_records_dicts):
        """compute_all_baselines works correctly with dict records.

        Previously crashed because compute_baseline didn't handle dicts
        in end_date computation. Now fixed via _record_date helper.
        """
        baselines = compute_all_baselines(sample_records_dicts, window_days=7)
        assert isinstance(baselines, dict)
        assert "sleep_score" in baselines
        assert "hrv_ms" in baselines

    def test_detect_deviations_works_with_records(self, sample_records):
        """detect_deviations works correctly with DailyRecord objects."""
        baselines = compute_all_baselines(sample_records, window_days=7)
        alerts = detect_deviations(sample_records, baselines)
        assert isinstance(alerts, list)


# ── detect_deviations_hybrid with dicts ──────────────────────────────────────


class TestDetectDeviationsHybridDicts:
    """detect_deviations_hybrid crashes via compute_all_baselines on dicts."""

    def test_hybrid_works_with_dict_records(self, sample_records_dicts):
        """detect_deviations_hybrid works with dict records.

        Previously crashed because compute_all_baselines → compute_baseline
        didn't handle dicts in end_date computation.
        """
        baselines = compute_all_baselines(sample_records_dicts, window_days=14)
        alerts = detect_deviations_hybrid(sample_records_dicts, baselines)
        assert isinstance(alerts, list)

    def test_hybrid_works_with_records(self, sample_records):
        """detect_deviations_hybrid works correctly with DailyRecord objects."""
        baselines = compute_all_baselines(sample_records, window_days=14)
        alerts = detect_deviations_hybrid(sample_records, baselines)
        assert isinstance(alerts, list)


# ── detect_deviations_adaptive with dicts ────────────────────────────────────


class TestDetectDeviationsAdaptiveDicts:
    """detect_deviations_adaptive crashes via compute_all_baselines on dicts."""

    def test_adaptive_works_with_dict_records(self, sample_records_dicts):
        """detect_deviations_adaptive works with dict records.

        Previously crashed because compute_all_baselines → compute_baseline
        didn't handle dicts in end_date computation.
        """
        baselines = compute_all_baselines(sample_records_dicts, window_days=14)
        alerts = detect_deviations_adaptive(sample_records_dicts, baselines)
        assert isinstance(alerts, list)
