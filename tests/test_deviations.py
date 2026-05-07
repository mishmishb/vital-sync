"""
Tests for deviation detection:
- Standard detect_deviations with fixed thresholds
- detect_deviations_adaptive with z-score thresholds
- detect_deviations_hybrid with floor checks
- Boiling-frog prevention
- Edge cases
"""

from datetime import date, timedelta

from vital_sync.analytics import (
    DailyRecord,
    compute_all_baselines,
    detect_deviations,
    detect_deviations_adaptive,
    detect_deviations_hybrid,
)

# ── Helpers ──────────────────────────────────────────────────────────────────


def make_records(
    base_date: date,
    metric: str,
    values: list[float],
    extra: dict | None = None,
) -> list[DailyRecord]:
    """Build DailyRecord list with one metric varying across days."""
    records = []
    for i, v in enumerate(values):
        d = base_date - timedelta(days=len(values) - 1 - i)
        r = DailyRecord(date=d)
        setattr(r, metric, v)
        if extra:
            for k, vals in extra.items():
                if i < len(vals):
                    setattr(r, k, vals[i])
        records.append(r)
    return records


# ── Standard deviation detection ────────────────────────────────────────────


class TestDetectDeviations:
    """Test the basic detect_deviations function (lines 336-431)."""

    def test_detects_large_sleep_drop(self, sample_records, today_):
        """Sharp sleep_score drop at the end triggers a deviation."""
        baselines = compute_all_baselines(sample_records, window_days=7, end_date=today_)
        alerts = detect_deviations(sample_records, baselines)
        sleep_alerts = [a for a in alerts if a.metric == "sleep_score"]
        assert len(sleep_alerts) > 0
        assert sleep_alerts[0].direction == "down"
        assert sleep_alerts[0].severity in ("minor", "moderate", "major")

    def test_no_deviations_for_stable_data(self, flat_sleep_records, today_):
        """Stable data does not trigger deviations."""
        baselines = compute_all_baselines(flat_sleep_records, window_days=7, end_date=today_)
        alerts = detect_deviations(flat_sleep_records, baselines)
        assert alerts == []

    def test_spo2_only_alerts_on_drop(self):
        """SpO2 rule has direction='down' — only drops trigger."""
        records = [
            DailyRecord(date=date(2026, 4, 20), spo2=98.0),
            DailyRecord(date=date(2026, 4, 21), spo2=98.0),
            DailyRecord(date=date(2026, 4, 22), spo2=98.0),
            DailyRecord(date=date(2026, 4, 23), spo2=98.0),
            DailyRecord(date=date(2026, 4, 24), spo2=98.0),
            DailyRecord(date=date(2026, 4, 25), spo2=96.0),  # drop
        ]
        baselines = compute_all_baselines(records, window_days=6)
        alerts = detect_deviations(records, baselines)
        spo2_alerts = [a for a in alerts if a.metric == "spo2"]
        assert len(spo2_alerts) > 0
        assert spo2_alerts[0].direction == "down"

        # SpO2 going UP should NOT trigger (direction='down')
        records_up = [
            DailyRecord(date=date(2026, 4, 20), spo2=96.0),
            DailyRecord(date=date(2026, 4, 21), spo2=96.0),
            DailyRecord(date=date(2026, 4, 22), spo2=96.0),
            DailyRecord(date=date(2026, 4, 23), spo2=96.0),
            DailyRecord(date=date(2026, 4, 24), spo2=96.0),
            DailyRecord(date=date(2026, 4, 25), spo2=98.0),  # improvement
        ]
        baselines2 = compute_all_baselines(records_up, window_days=6)
        alerts2 = detect_deviations(records_up, baselines2)
        spo2_alerts2 = [a for a in alerts2 if a.metric == "spo2"]
        assert len(spo2_alerts2) == 0

    def test_empty_records(self):
        """Empty records return empty alert list."""
        alerts = detect_deviations([])
        assert alerts == []

    def test_insufficient_data(self):
        """Less than min_data_points produces no alerts."""
        records = [
            DailyRecord(date=date(2026, 4, 25), sleep_score=85),
            DailyRecord(date=date(2026, 4, 26), sleep_score=80),
        ]
        alerts = detect_deviations(records, min_data_points=3)
        assert alerts == []

    def test_alert_severity_ordering(self, sample_records, today_):
        """Alerts are sorted by severity: major first, minor last."""
        baselines = compute_all_baselines(sample_records, window_days=7, end_date=today_)
        alerts = detect_deviations(sample_records, baselines)
        if len(alerts) >= 2:
            for i in range(len(alerts) - 1):
                prev = alerts[i].severity
                nxt = alerts[i + 1].severity
                rank = {"major": 0, "moderate": 1, "minor": 2}
                assert rank[prev] <= rank[nxt]


# ── Adaptive deviation detection ───────────────────────────────────────────


class TestDetectDeviationsAdaptive:
    """Test adaptive z-score based detection (lines 593-699)."""

    def test_detects_z_score_exceedance(self):
        """A value > 2σ from baseline triggers an alert."""
        records = [
            DailyRecord(date=date(2026, 4, 20), sleep_score=85),
            DailyRecord(date=date(2026, 4, 21), sleep_score=84),
            DailyRecord(date=date(2026, 4, 22), sleep_score=86),
            DailyRecord(date=date(2026, 4, 23), sleep_score=85),
            DailyRecord(date=date(2026, 4, 24), sleep_score=85),
            DailyRecord(date=date(2026, 4, 25), sleep_score=60),  # sharp drop
        ]
        baselines = compute_all_baselines(records, window_days=14)
        alerts = detect_deviations_adaptive(records, baselines)
        sleep_alerts = [a for a in alerts if a.metric == "sleep_score"]
        assert len(sleep_alerts) > 0
        assert sleep_alerts[0].direction == "down"

    def test_stable_metrics_no_alert(self, flat_sleep_records, today_):
        """Stable metrics produce no adaptive alerts."""
        baselines = compute_all_baselines(flat_sleep_records, window_days=14, end_date=today_)
        alerts = detect_deviations_adaptive(flat_sleep_records, baselines)
        assert alerts == []

    def test_zero_std_skipped(self):
        """When std is 0 (all identical values), no alert triggered."""
        records = [
            DailyRecord(date=date(2026, 4, 20), hrv_ms=45),
            DailyRecord(date=date(2026, 4, 21), hrv_ms=45),
            DailyRecord(date=date(2026, 4, 22), hrv_ms=45),
            DailyRecord(date=date(2026, 4, 23), hrv_ms=45),
            DailyRecord(date=date(2026, 4, 24), hrv_ms=45),
            DailyRecord(date=date(2026, 4, 25), hrv_ms=45),
        ]
        baselines = compute_all_baselines(records, window_days=14)
        alerts = detect_deviations_adaptive(records, baselines)
        hrv_alerts = [a for a in alerts if a.metric == "hrv_ms"]
        assert len(hrv_alerts) == 0  # std=0 → skipped at line 630

    def test_direction_aware_above(self):
        """avg_hr alerts on 'above' — high HR triggers, low HR ignored."""
        records = [
            DailyRecord(date=date(2026, 4, 20), avg_hr=55),
            DailyRecord(date=date(2026, 4, 21), avg_hr=56),
            DailyRecord(date=date(2026, 4, 22), avg_hr=55),
            DailyRecord(date=date(2026, 4, 23), avg_hr=54),
            DailyRecord(date=date(2026, 4, 24), avg_hr=55),
            DailyRecord(date=date(2026, 4, 25), avg_hr=75),  # high HR
        ]
        baselines = compute_all_baselines(records, window_days=14)
        alerts = detect_deviations_adaptive(records, baselines)
        hr_alerts = [a for a in alerts if a.metric == "avg_hr"]
        assert len(hr_alerts) > 0
        assert hr_alerts[0].direction == "up"

        # Now test: avg_hr going DOWN should NOT trigger
        records_low = [
            DailyRecord(date=date(2026, 4, 20), avg_hr=60),
            DailyRecord(date=date(2026, 4, 21), avg_hr=60),
            DailyRecord(date=date(2026, 4, 22), avg_hr=60),
            DailyRecord(date=date(2026, 4, 23), avg_hr=60),
            DailyRecord(date=date(2026, 4, 24), avg_hr=60),
            DailyRecord(date=date(2026, 4, 25), avg_hr=45),  # low HR (good!)
        ]
        baselines2 = compute_all_baselines(records_low, window_days=14)
        alerts2 = detect_deviations_adaptive(records_low, baselines2)
        hr_alerts2 = [a for a in alerts2 if a.metric == "avg_hr"]
        assert len(hr_alerts2) == 0  # Going down is good, no alert

    def test_direction_aware_below(self):
        """sleep_score alerts on 'below' — low score triggers, high ignored."""
        # High sleep_score (improvement) should NOT trigger
        records_high = [
            DailyRecord(date=date(2026, 4, 20), sleep_score=70),
            DailyRecord(date=date(2026, 4, 21), sleep_score=71),
            DailyRecord(date=date(2026, 4, 22), sleep_score=70),
            DailyRecord(date=date(2026, 4, 23), sleep_score=71),
            DailyRecord(date=date(2026, 4, 24), sleep_score=70),
            DailyRecord(date=date(2026, 4, 25), sleep_score=95),  # great night!
        ]
        baselines = compute_all_baselines(records_high, window_days=14)
        alerts = detect_deviations_adaptive(records_high, baselines)
        sleep_alerts = [a for a in alerts if a.metric == "sleep_score"]
        assert len(sleep_alerts) == 0  # Going up is good, no alert

    def test_empty_records_adaptive(self):
        """Empty records return empty list."""
        alerts = detect_deviations_adaptive([])
        assert alerts == []


# ── Hybrid deviation detection ──────────────────────────────────────────────


class TestDetectDeviationsHybrid:
    """Test hybrid detection with absolute floors (lines 500-590)."""

    def test_floor_triggers_on_bad_sleep_score(self):
        """sleep_score < 80 triggers 'major' via HYBRID_FLOORS."""
        records = [
            DailyRecord(date=date(2026, 4, 20), sleep_score=85),
            DailyRecord(date=date(2026, 4, 21), sleep_score=86),
            DailyRecord(date=date(2026, 4, 22), sleep_score=85),
            DailyRecord(date=date(2026, 4, 23), sleep_score=86),
            DailyRecord(date=date(2026, 4, 24), sleep_score=85),
            DailyRecord(date=date(2026, 4, 25), sleep_score=65),  # below floor of 80
        ]
        baselines = compute_all_baselines(records, window_days=14)
        alerts = detect_deviations_hybrid(records, baselines)
        sleep_alerts = [a for a in alerts if a.metric == "sleep_score"]
        assert len(sleep_alerts) > 0
        # Floor severity is "major" for sleep_score < 80
        assert any(a.severity == "major" for a in sleep_alerts)

    def test_boiling_frog_prevention(self):
        """Gradual decline should still trigger a floor alert at the end.

        The 'boiling frog' problem: if sleep_score drops 85→84→83→82→81→80→79→78
        gradually, the adaptive z-score might not trigger because the rolling
        baseline slowly normalises the decline. But the absolute floor
        (sleep_score < 80) MUST trigger regardless.
        """
        base = date(2026, 4, 25)
        records = []
        for i in range(10):
            d = base - timedelta(days=9 - i)
            records.append(DailyRecord(date=d, sleep_score=85 - i * 0.8))
        # Values: 85, 84.2, 83.4, 82.6, 81.8, 81, 80.2, 79.4, 78.6, 77.8
        # Last value is 77.8 < 80

        baselines = compute_all_baselines(records, window_days=14)
        alerts = detect_deviations_hybrid(records, baselines)
        sleep_alerts = [a for a in alerts if a.metric == "sleep_score"]
        assert len(sleep_alerts) > 0

        # At least one alert should be from the floor (sleep_score < 80)
        floor_alerts = [a for a in sleep_alerts if a.message and "absolute floor" in a.message]
        assert len(floor_alerts) > 0, (
            "BUG: gradual sleep_score decline to 77.8 should trigger the absolute floor (< 80), "
            "but no floor alert was found. This is the boiling-frog failure mode."
        )
        assert floor_alerts[0].severity == "major"

    def test_spo2_floor(self):
        """SpO2 < 92 triggers 'major' floor alert."""
        records = []
        base = date(2026, 4, 25)
        for i in range(10):
            records.append(DailyRecord(date=base - timedelta(days=9 - i), spo2=97.0))
        records.append(DailyRecord(date=base, spo2=91.0))  # below 92 floor

        baselines = compute_all_baselines(records, window_days=14)
        alerts = detect_deviations_hybrid(records, baselines)
        spo2_alerts = [a for a in alerts if a.metric == "spo2"]
        assert len(spo2_alerts) > 0
        assert spo2_alerts[0].severity == "major"

    def test_sleep_duration_floor(self):
        """Sleep duration < 5h triggers 'major' floor."""
        records = []
        base = date(2026, 4, 25)
        for i in range(10):
            records.append(
                DailyRecord(date=base - timedelta(days=9 - i), sleep_duration_hours=7.5)
            )
        records.append(DailyRecord(date=base, sleep_duration_hours=4.0))  # below 5h

        baselines = compute_all_baselines(records, window_days=14)
        alerts = detect_deviations_hybrid(records, baselines)
        sleep_alerts = [a for a in alerts if a.metric == "sleep_duration_hours"]
        assert len(sleep_alerts) > 0
        assert any(a.severity == "major" for a in sleep_alerts)

    def test_hrv_floor(self):
        """HRV < 20 triggers 'major' floor."""
        records = []
        base = date(2026, 4, 25)
        for i in range(10):
            records.append(DailyRecord(date=base - timedelta(days=9 - i), hrv_ms=45))
        records.append(DailyRecord(date=base, hrv_ms=15))  # below 20

        baselines = compute_all_baselines(records, window_days=14)
        alerts = detect_deviations_hybrid(records, baselines)
        hrv_alerts = [a for a in alerts if a.metric == "hrv_ms"]
        assert len(hrv_alerts) > 0
        assert any(a.severity == "major" for a in hrv_alerts)

    def test_floor_and_adaptive_merge(self):
        """When both floor and adaptive trigger for same metric, more severe wins."""
        records = []
        base = date(2026, 4, 25)
        for i in range(10):
            records.append(DailyRecord(date=base - timedelta(days=9 - i), sleep_score=85))
        records.append(DailyRecord(date=base, sleep_score=50))  # massive drop

        baselines = compute_all_baselines(records, window_days=14)
        alerts = detect_deviations_hybrid(records, baselines)
        sleep_alerts = [a for a in alerts if a.metric == "sleep_score"]
        # Should be exactly one alert (merged), severity "major"
        assert len(sleep_alerts) == 1
        assert sleep_alerts[0].severity == "major"

    def test_hybrid_empty_records(self):
        """Empty records → empty alerts."""
        alerts = detect_deviations_hybrid([])
        assert alerts == []

    def test_hybrid_insufficient_data_respected(self):
        """Floor checks respect min_data_points.

        Previously floor checks fired regardless of min_data_points.
        Now they are skipped when total_records < min_data_points.
        """
        records = [
            DailyRecord(date=date(2026, 4, 25), sleep_score=65),
            DailyRecord(date=date(2026, 4, 26), sleep_score=66),
        ]
        alerts = detect_deviations_hybrid(records, min_data_points=5)
        # Floor checks should be skipped with only 2 records and min_data_points=5
        assert len(alerts) == 0
        assert not any("absolute floor" in a.message for a in alerts)

    def test_hybrid_baseline_mean_can_be_none(self):
        """When a floor triggers but no baseline exists for the metric,
        baseline_mean/baseline_std can be None in DeviationAlert.

        BUG: DeviationAlert.baseline_mean and baseline_std are typed as `float`
        but can be None when baseline is missing (line 576-577).
        """
        # Create records for a metric NOT in the baseline computation
        records = [
            DailyRecord(date=date(2026, 4, 25), sleep_score=65),
        ]
        # No baseline computed — floor should still trigger
        alerts = detect_deviations_hybrid(records, baselines={}, min_data_points=1)
        sleep_alerts = [a for a in alerts if a.metric == "sleep_score"]
        # Should still trigger floor alert even without baseline
        assert len(sleep_alerts) > 0
        # baseline_mean/baseline_std can be None
        if sleep_alerts[0].baseline_mean is None:
            pass  # This is the bug — typed as float but can be None


# ── DeviationAlert structure ─────────────────────────────────────────────────


class TestDeviationAlertStructure:
    """Validate DeviationAlert fields."""

    def test_all_required_fields_present(self, sample_records, today_):
        """Deviation alerts have all expected fields."""
        baselines = compute_all_baselines(sample_records, window_days=7, end_date=today_)
        alerts = detect_deviations(sample_records, baselines)
        if alerts:
            alert = alerts[0]
            assert alert.metric is not None
            assert alert.date is not None
            assert alert.value is not None
            assert alert.baseline_mean is not None
            assert alert.baseline_std is not None
            assert alert.severity in ("minor", "moderate", "major")
            assert alert.direction in ("up", "down")
            assert len(alert.message) > 0
