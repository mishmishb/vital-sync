"""Sleep tag management for health data analysis.

Track nightly factors (supplements, environment, behaviours) and their
impact on sleep metrics. Baseline tags are assumed applied every night;
only deviations (negations, extras) are surfaced in reports.
"""

from __future__ import annotations

from datetime import date
from typing import Any


def _coerce_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def _dedupe_preserve_order(tags: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for tag in tags:
        if tag in seen:
            continue
        seen.add(tag)
        deduped.append(tag)
    return deduped


def active_baseline_tags(tag_data: dict[str, Any], target_day: str | date) -> list[str]:
    """Return baseline tags active for the given wake-date.

    `baseline_tags` remain the always-on defaults.
    `baseline_effective_from` adds tags that become baseline only from a
    specific ISO date onward, without rewriting earlier history.
    """
    target = _coerce_date(target_day)
    baseline = list(tag_data.get("baseline_tags", []) or [])
    dated_baselines = tag_data.get("baseline_effective_from", {}) or {}

    active = list(baseline)
    for tag, start_day in dated_baselines.items():
        if not isinstance(start_day, str):
            continue
        if target >= _coerce_date(start_day):
            active.append(tag)

    return _dedupe_preserve_order(active)


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
