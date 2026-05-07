"""Sleep tag management for health data analysis.

Track nightly factors (supplements, environment, behaviours) and their
impact on sleep metrics. Baseline tags are assumed applied every night;
only deviations (negations, extras) are surfaced in reports.
"""

from typing import Any


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
