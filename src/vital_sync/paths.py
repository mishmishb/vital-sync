"""Path resolution and first-run data bootstrap for vital-sync.

Defaults are project-local to the current working directory so CLI users can run
vital-sync from any integration repo without writing into the installed package.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DATA_DIR_NAME = ".vital-sync-data"
DEFAULT_SLEEP_TAGS = {
    "baseline_tags": [],
    "canonical_tags": {},
    "tags_by_date": {},
}


@dataclass(frozen=True)
class DataFiles:
    data_dir: Path
    cache_path: Path
    sleep_tags_path: Path
    config_path: Path
    hevy_cache_dir: Path


def data_dir() -> Path:
    """Return the vital-sync data directory.

    `VITAL_SYNC_DATA_DIR` wins. Otherwise use `.vital-sync-data` under the
    caller's current working directory, not the package install location.
    """
    return (
        Path(os.environ.get("VITAL_SYNC_DATA_DIR", DEFAULT_DATA_DIR_NAME)).expanduser().resolve()
    )


def cache_path() -> Path:
    return Path(os.environ.get("VITAL_SYNC_CACHE", str(data_dir() / "cache.json"))).expanduser()


def sleep_tags_path() -> Path:
    return Path(
        os.environ.get("VITAL_SYNC_SLEEP_TAGS", str(data_dir() / "sleep_tags.json"))
    ).expanduser()


def config_path() -> Path:
    return Path(os.environ.get("VITAL_SYNC_CONFIG", str(data_dir() / "config.json"))).expanduser()


def hevy_cache_dir() -> Path:
    return Path(
        os.environ.get("VITAL_SYNC_HEVY_CACHE_DIR", str(data_dir() / "hevy_cache"))
    ).expanduser()


def _write_json_if_missing(path: Path, payload: object) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def ensure_data_files() -> DataFiles:
    """Create safe first-run data files without creating personal config."""
    resolved_data_dir = data_dir()
    resolved_cache_path = cache_path()
    resolved_sleep_tags_path = sleep_tags_path()
    resolved_config_path = config_path()
    resolved_hevy_cache_dir = hevy_cache_dir()

    resolved_data_dir.mkdir(parents=True, exist_ok=True)
    resolved_hevy_cache_dir.mkdir(parents=True, exist_ok=True)
    _write_json_if_missing(resolved_cache_path, [])
    _write_json_if_missing(resolved_sleep_tags_path, DEFAULT_SLEEP_TAGS)

    return DataFiles(
        data_dir=resolved_data_dir,
        cache_path=resolved_cache_path,
        sleep_tags_path=resolved_sleep_tags_path,
        config_path=resolved_config_path,
        hevy_cache_dir=resolved_hevy_cache_dir,
    )
