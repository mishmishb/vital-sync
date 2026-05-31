import json
from pathlib import Path

from vital_sync import paths


def test_default_data_dir_is_current_working_directory_dot_data(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("VITAL_SYNC_DATA_DIR", raising=False)
    monkeypatch.chdir(tmp_path)

    assert paths.data_dir() == tmp_path / ".vital-sync-data"
    assert paths.cache_path() == tmp_path / ".vital-sync-data" / "cache.json"
    assert paths.sleep_tags_path() == tmp_path / ".vital-sync-data" / "sleep_tags.json"
    assert paths.config_path() == tmp_path / ".vital-sync-data" / "config.json"


def test_env_data_dir_overrides_current_working_directory(monkeypatch, tmp_path: Path) -> None:
    data_dir = tmp_path / "custom-data"
    monkeypatch.setenv("VITAL_SYNC_DATA_DIR", str(data_dir))
    monkeypatch.delenv("VITAL_SYNC_CACHE", raising=False)
    monkeypatch.delenv("VITAL_SYNC_SLEEP_TAGS", raising=False)
    monkeypatch.delenv("VITAL_SYNC_CONFIG", raising=False)

    assert paths.data_dir() == data_dir
    assert paths.cache_path() == data_dir / "cache.json"
    assert paths.sleep_tags_path() == data_dir / "sleep_tags.json"
    assert paths.config_path() == data_dir / "config.json"


def test_explicit_file_env_vars_override_data_dir(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VITAL_SYNC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("VITAL_SYNC_CACHE", str(tmp_path / "cache.json"))
    monkeypatch.setenv("VITAL_SYNC_SLEEP_TAGS", str(tmp_path / "tags.json"))
    monkeypatch.setenv("VITAL_SYNC_CONFIG", str(tmp_path / "settings.json"))

    assert paths.cache_path() == tmp_path / "cache.json"
    assert paths.sleep_tags_path() == tmp_path / "tags.json"
    assert paths.config_path() == tmp_path / "settings.json"


def test_ensure_data_files_creates_safe_public_defaults(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("VITAL_SYNC_DATA_DIR", raising=False)
    monkeypatch.delenv("VITAL_SYNC_CACHE", raising=False)
    monkeypatch.delenv("VITAL_SYNC_SLEEP_TAGS", raising=False)
    monkeypatch.delenv("VITAL_SYNC_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)

    created = paths.ensure_data_files()

    assert created.data_dir == tmp_path / ".vital-sync-data"
    assert created.cache_path.read_text() == "[]\n"
    assert json.loads(created.sleep_tags_path.read_text()) == {
        "baseline_tags": [],
        "canonical_tags": {},
        "tags_by_date": {},
    }
    assert not created.config_path.exists()
    assert (tmp_path / ".vital-sync-data" / "hevy_cache").is_dir()
