from datetime import date

from vital_sync.sleep_tags import (
    SleepTagManager,
    active_baseline_tags,
    migrate_legacy_sleep_tags,
)


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


def test_migrate_flat_sleep_tags_only_dedupes_existing_dates() -> None:
    tag_data = {
        "baseline_tags": ["sleep mask"],
        "canonical_tags": ["sleep mask"],
        "tags_by_date": {"2026-05-22": ["melatonin", "melatonin"]},
    }

    changed = migrate_legacy_sleep_tags(tag_data, ["2026-05-21", "2026-05-22"])

    assert changed is True
    assert tag_data == {
        "baseline_tags": ["sleep mask"],
        "canonical_tags": {"sleep mask": ""},
        "tags_by_date": {"2026-05-22": ["melatonin"]},
    }


def test_sleep_tag_manager_stub_is_safe_and_explicit() -> None:
    manager = SleepTagManager()

    assert manager.get_all_used_tags() == []
    assert manager.compute_tag_metric_comparison([], "melatonin", "sleep_score") is None
