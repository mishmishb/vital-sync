from datetime import date

from vital_sync.sleep_tags import SleepTagManager, active_baseline_tags


def test_active_baseline_tags_accepts_date_objects_and_dedupes_in_order() -> None:
    tag_data = {
        "baseline_tags": ["sleep mask", "sleep mask", "cool room"],
        "baseline_effective_from": {
            "melatonin": "2026-05-22",
            "invalid": 123,
            "future": "2026-06-01",
        },
    }

    assert active_baseline_tags(tag_data, date(2026, 5, 22)) == [
        "sleep mask",
        "cool room",
        "melatonin",
    ]


def test_sleep_tag_manager_stub_is_safe_and_explicit() -> None:
    manager = SleepTagManager()

    assert manager.get_all_used_tags() == []
    assert manager.compute_tag_metric_comparison([], "melatonin", "sleep_score") is None
