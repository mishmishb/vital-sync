from datetime import date

from vital_sync.sleep_tags import (
    SleepTagManager,
    active_baseline_tags,
    merge_legacy_history,
    migrate_legacy_sleep_tags,
)

# A synthetic 8-tag baseline, used only to exercise the merge/override logic.
# It deliberately mirrors the *shape* of a real tag store without containing one:
# this repository is public, and a real baseline plus real per-night overrides
# would describe an identifiable person's nights.
SYNTH_BASELINE = [
    "alpha",
    "bravo",
    "charlie",
    "delta",
    "echo",
    "foxtrot",
    "golf",
    "hotel",
]


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


# --- merge_legacy_history: porting a legacy applied/negated event log -------


def test_merge_history_applies_override_to_existing_baseline_date() -> None:
    """A date already present with baseline tags must take the override.

    The merge has to recompute the effective list; leaving the baseline in
    place is the bug this function exists to fix.
    """
    tag_data = {
        "baseline_tags": list(SYNTH_BASELINE),
        "tags_by_date": {"2000-01-01": list(SYNTH_BASELINE)},
    }
    history = {
        "2000-01-01": {"applied": ["romeo"], "negated": ["alpha"]},
    }

    changed = merge_legacy_history(tag_data, history)

    result = tag_data["tags_by_date"]["2000-01-01"]
    assert changed == 1
    assert "romeo" in result
    assert "alpha" not in result
    # tags that exist only on this side survive the override
    assert "golf" in result and "hotel" in result


def test_merge_history_materialises_date_absent_from_tags_by_date() -> None:
    """A date absent from the tag store entirely must be built from history."""
    tag_data = {
        "baseline_tags": list(SYNTH_BASELINE),
        "tags_by_date": {},
    }
    history = {
        "2000-01-02": {
            "applied": ["romeo", "sierra", "alpha", "bravo"],
            "negated": ["charlie"],
        },
    }

    changed = merge_legacy_history(tag_data, history)

    assert changed == 1
    result = tag_data["tags_by_date"]["2000-01-02"]
    assert "romeo" in result
    assert "sierra" in result
    # the negated baseline tag must not be carried in
    assert "charlie" not in result


def test_merge_history_leaves_date_absent_from_history_untouched() -> None:
    """The reverse case: a date the history never mentions stays as it is."""
    tag_data = {
        "baseline_tags": list(SYNTH_BASELINE),
        "tags_by_date": {"2000-01-03": list(SYNTH_BASELINE)},
    }
    before = list(tag_data["tags_by_date"]["2000-01-03"])
    history = {
        "2000-01-01": {"applied": ["romeo"], "negated": ["alpha"]},
    }

    merge_legacy_history(tag_data, history)

    assert tag_data["tags_by_date"]["2000-01-03"] == before


def test_merge_history_is_idempotent_second_run_reports_zero() -> None:
    tag_data = {
        "baseline_tags": list(SYNTH_BASELINE),
        "tags_by_date": {"2000-01-01": list(SYNTH_BASELINE)},
    }
    history = {
        "2000-01-01": {"applied": ["romeo"], "negated": ["alpha"]},
        "2000-01-02": {"applied": ["sierra"], "negated": ["bravo"]},
    }

    first = merge_legacy_history(tag_data, history)
    snapshot = {k: list(v) for k, v in tag_data["tags_by_date"].items()}
    second = merge_legacy_history(tag_data, history)

    assert first == 2
    assert second == 0  # negative control: re-running changes nothing
    assert tag_data["tags_by_date"] == snapshot


def test_merge_history_does_not_invent_empty_entries_or_touch_other_dates() -> None:
    """A date present on neither side stays absent."""
    tag_data = {
        "baseline_tags": list(SYNTH_BASELINE),
        "tags_by_date": {"2000-01-01": ["romeo"]},
    }
    history = {
        "2000-01-01": {"applied": ["romeo"], "negated": ["alpha"]},
    }

    changed = merge_legacy_history(tag_data, history)

    assert changed == 0
    assert set(tag_data["tags_by_date"]) == {"2000-01-01"}
    assert all(tag_data["tags_by_date"].values())  # no empty entries invented
