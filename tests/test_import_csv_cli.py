"""Behavioural CLI tests for CSV import."""

import json
import sys
from pathlib import Path

import pytest

from vital_sync import import_csv


def test_main_prints_usage_and_exits_when_arguments_are_missing(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["import_csv.py"])

    with pytest.raises(SystemExit) as exc:
        import_csv.main()

    assert exc.value.code == 1
    assert "Usage: import_csv.py <renpho|mynetdiary> <path_to_csv>" in capsys.readouterr().out


def test_main_rejects_missing_csv_before_touching_cache(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    monkeypatch.setattr(sys, "argv", ["import_csv.py", "renpho", str(tmp_path / "missing.csv")])
    monkeypatch.setattr(import_csv, "CACHE_PATH", tmp_path / "cache.json")

    with pytest.raises(SystemExit) as exc:
        import_csv.main()

    assert exc.value.code == 1
    assert "Error: File not found" in capsys.readouterr().out
    assert not (tmp_path / "cache.json").exists()


def test_main_rejects_unknown_source_without_saving(monkeypatch, tmp_path: Path, capsys) -> None:
    csv_path = tmp_path / "input.csv"
    csv_path.write_text("Date,Weight(kg)\n2026-05-20,78.4\n")
    monkeypatch.setattr(sys, "argv", ["import_csv.py", "garmin", str(csv_path)])
    monkeypatch.setattr(import_csv, "CACHE_PATH", tmp_path / "cache.json")

    with pytest.raises(SystemExit) as exc:
        import_csv.main()

    assert exc.value.code == 1
    assert "Error: Unknown source 'garmin'" in capsys.readouterr().out
    assert not (tmp_path / "cache.json").exists()


def test_main_imports_renpho_csv_merges_existing_cache_and_reports_new_dates(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(
        json.dumps(
            [
                {
                    "date": "2026-05-20",
                    "sleep_score": 82,
                    "sources": ["oura"],
                }
            ]
        )
    )
    csv_path = tmp_path / "renpho.csv"
    csv_path.write_text(
        "Date,Weight(kg),Body Fat(%),Muscle Mass(kg)\n"
        "2026-05-20,78.1,18.4,62.0\n"
        "2026-05-21,77.9,18.2,62.1\n"
    )
    monkeypatch.setattr(sys, "argv", ["import_csv.py", "renpho", str(csv_path)])
    monkeypatch.setattr(import_csv, "CACHE_PATH", cache_path)

    import_csv.main()

    output = capsys.readouterr().out
    saved = json.loads(cache_path.read_text())
    by_date = {record["date"]: record for record in saved}
    assert "Parsed 2 records from renpho CSV" in output
    assert "Cache updated: 1 → 2 total records" in output
    assert "New dates added: 2026-05-21" in output
    assert by_date["2026-05-20"]["sleep_score"] == 82
    assert by_date["2026-05-20"]["weight_kg"] == 78.1
    assert by_date["2026-05-20"]["sources"] == ["oura", "renpho"]
    assert by_date["2026-05-21"]["body_fat_pct"] == 18.2


def test_main_imports_mynetdiary_csv_and_reports_no_new_dates(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(json.dumps([{"date": "2026-05-20", "sources": ["oura"]}]))
    csv_path = tmp_path / "mynetdiary.csv"
    csv_path.write_text("Date,Calories,Protein,Weight\n2026-05-20,2400,175,78.2\n")
    monkeypatch.setattr(sys, "argv", ["import_csv.py", "mynetdiary", str(csv_path)])
    monkeypatch.setattr(import_csv, "CACHE_PATH", cache_path)

    import_csv.main()

    output = capsys.readouterr().out
    saved = json.loads(cache_path.read_text())
    assert "Parsed 1 records from mynetdiary CSV" in output
    assert "No new dates (all imported dates already existed)" in output
    assert saved[0]["calories_in"] == 2400
    assert saved[0]["protein_g"] == 175
    assert saved[0]["weight_kg"] == 78.2
    assert saved[0]["sources"] == ["oura", "mynetdiary"]
