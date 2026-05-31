"""Sleep tag management for health data analysis.

Sleep tags are stored as direct per-date facts. Baseline tags are only a seed
for new dates; once a date has an entry, that list is the source of truth.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from typing import Any


def _coerce_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def _dedupe_preserve_order(tags: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for tag in tags:
        if not isinstance(tag, str) or not tag:
            continue
        if tag in seen:
            continue
        seen.add(tag)
        deduped.append(tag)
    return deduped


def active_baseline_tags(tag_data: dict[str, Any], target_day: str | date) -> list[str]:
    """Return baseline tags active for a wake-date.

    New flat files use only ``baseline_tags``. ``baseline_effective_from`` is
    still honoured while migrating legacy files so historical rows materialise
    correctly before that key is removed.
    """
    target = _coerce_date(target_day)
    active = list(tag_data.get("baseline_tags", []) or [])
    dated_baselines = tag_data.get("baseline_effective_from", {}) or {}

    for tag, start_day in dated_baselines.items():
        if isinstance(start_day, str) and target >= _coerce_date(start_day):
            active.append(tag)

    return _dedupe_preserve_order(active)


def normalise_canonical_tags(tag_data: dict[str, Any]) -> bool:
    """Convert legacy canonical tag lists into a descriptive mapping."""
    canonical_tags = tag_data.get("canonical_tags")
    if canonical_tags is None:
        tag_data["canonical_tags"] = {}
        return True
    if isinstance(canonical_tags, list):
        tag_data["canonical_tags"] = {tag: "" for tag in _dedupe_preserve_order(canonical_tags)}
        return True
    return False


def build_sleep_tags_for_day(
    tag_data: dict[str, Any], target_day: str | date, record_tags: list[str] | None = None
) -> tuple[list[str], bool]:
    """Return tags for a date, seeding a missing date from baseline + record tags."""
    target = target_day.isoformat() if isinstance(target_day, date) else target_day
    tags_by_date = tag_data.setdefault("tags_by_date", {})
    if target in tags_by_date:
        tags = _dedupe_preserve_order(tags_by_date[target] or [])
        if tags != tags_by_date[target]:
            tags_by_date[target] = tags
            return tags, True
        return tags, False

    tags = _dedupe_preserve_order(
        [*(tag_data.get("baseline_tags", []) or []), *(record_tags or [])]
    )
    tags_by_date[target] = tags
    return tags, True


def _dedupe_existing_tags_by_date(tags_by_date: dict[str, Any]) -> bool:
    changed = False
    for target_day, tags in list(tags_by_date.items()):
        deduped = _dedupe_preserve_order(tags or [])
        if deduped != tags:
            tags_by_date[target_day] = deduped
            changed = True
    return changed


def _legacy_record_dates(
    record_dates: Iterable[str | date], history: dict[str, Any], tags_by_date: dict[str, Any]
) -> set[str]:
    return {
        day.isoformat() if isinstance(day, date) else day
        for day in [*record_dates, *history.keys(), *tags_by_date.keys()]
    }


def _legacy_tags_for_day(
    tag_data: dict[str, Any], history: dict[str, Any], target_day: str
) -> list[str]:
    day_history = history.get(target_day, {}) or {}
    negated = set(day_history.get("negated", []) or [])
    tags = [tag for tag in active_baseline_tags(tag_data, target_day) if tag not in negated]
    tags.extend(day_history.get("applied", []) or [])
    return _dedupe_preserve_order(tags)


def _materialise_legacy_tags_by_date(
    tag_data: dict[str, Any],
    record_dates: Iterable[str | date],
    history: dict[str, Any],
    tags_by_date: dict[str, Any],
) -> bool:
    changed = False
    for target_day in sorted(_legacy_record_dates(record_dates, history, tags_by_date)):
        tags = tags_by_date.get(target_day)
        if tags is None:
            tags = _legacy_tags_for_day(tag_data, history, target_day)
        else:
            tags = _dedupe_preserve_order(tags or [])
        if tags_by_date.get(target_day) != tags:
            tags_by_date[target_day] = tags
            changed = True
    return changed


def _remove_legacy_keys(tag_data: dict[str, Any]) -> bool:
    changed = False
    for legacy_key in ("history", "baseline_effective_from"):
        if legacy_key in tag_data:
            del tag_data[legacy_key]
            changed = True
    return changed


def migrate_legacy_sleep_tags(
    tag_data: dict[str, Any], record_dates: Iterable[str | date]
) -> bool:
    """Convert baseline/history sleep tags to flat per-date ``tags_by_date`` entries."""
    changed = normalise_canonical_tags(tag_data)
    has_legacy_history = "history" in tag_data or "baseline_effective_from" in tag_data
    tags_by_date = tag_data.setdefault("tags_by_date", {})

    if not has_legacy_history:
        return _dedupe_existing_tags_by_date(tags_by_date) or changed

    history = tag_data.get("history", {}) or {}
    changed = (
        _materialise_legacy_tags_by_date(tag_data, record_dates, history, tags_by_date) or changed
    )
    return _remove_legacy_keys(tag_data) or changed


class SleepTagManager:
    """Stub — tag-based sleep analysis under active development."""

    def __init__(self):
        pass

    def get_all_used_tags(self) -> list[str]:
        return []

    def compute_tag_metric_comparison(
        self, records: list[Any], tag: str, metric: str
    ) -> dict[str, Any] | None:
        """Compare metric values when tag was active vs inactive."""
        return None
