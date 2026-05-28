"""Low-coverage behavioural tests for Hevy client analytics and API boundaries."""

import json
from datetime import UTC, datetime, timedelta
from urllib.error import HTTPError

import pytest

from vital_sync import hevy_client


def _set(
    weight: float | None = 50, reps: int | None = 10, rpe: float | None = 8
) -> hevy_client.HevySet:
    return hevy_client.HevySet(
        index=0,
        set_type="normal",
        weight_kg=weight,
        reps=reps,
        distance_meters=None,
        duration_seconds=None,
        rpe=rpe,
    )


def _exercise(title: str, template_id: str = "", sets: list[hevy_client.HevySet] | None = None):
    return hevy_client.HevyExercise(
        index=0,
        title=title,
        exercise_template_id=template_id,
        notes="",
        superset_id=None,
        sets=sets if sets is not None else [_set()],
    )


def _workout(
    day_offset: int,
    title: str = "Workout",
    exercise: hevy_client.HevyExercise | None = None,
    workout_id: str | None = None,
) -> hevy_client.HevyWorkout:
    start = datetime.now(UTC) - timedelta(days=day_offset)
    return hevy_client.HevyWorkout(
        id=workout_id or f"w-{day_offset}",
        title=title,
        routine_id=None,
        description="",
        start_time=start,
        end_time=start + timedelta(minutes=45),
        updated_at=start + timedelta(minutes=46),
        created_at=start,
        exercises=[exercise or _exercise("Bench Press")],
    )


def test_get_api_key_reads_environment_before_files(monkeypatch) -> None:
    monkeypatch.setenv("HEVY_API_KEY", "from-env")
    assert hevy_client._get_api_key() == "from-env"


def test_hevy_request_requires_key_without_network(monkeypatch) -> None:
    monkeypatch.delenv("HEVY_API_KEY", raising=False)
    monkeypatch.setattr(hevy_client.Path, "home", lambda: hevy_client.Path("/tmp/no-such-home"))

    with pytest.raises(RuntimeError, match="HEVY_API_KEY not found"):
        hevy_client._hevy_request("/user/info")


def test_hevy_request_builds_url_headers_and_wraps_http_errors(monkeypatch) -> None:
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _tb):
            return False

        def read(self):
            return b'{"ok": true}'

    seen = {}

    def fake_urlopen(req, timeout):
        seen["url"] = req.full_url
        seen["api-key"] = req.headers["Api-key"]
        seen["accept"] = req.headers["Accept"]
        seen["timeout"] = timeout
        return Response()

    monkeypatch.setenv("HEVY_API_KEY", "secret-key")
    monkeypatch.setattr(hevy_client.urllib.request, "urlopen", fake_urlopen)

    assert hevy_client._hevy_request("/workouts", {"page": "1"}) == {"ok": True}
    assert seen == {
        "url": "https://api.hevyapp.com/v1/workouts?page=1",
        "api-key": "secret-key",
        "accept": "application/json",
        "timeout": 30,
    }

    def failing_urlopen(_req, timeout):
        raise HTTPError("url", 429, "rate limited", {}, None)

    monkeypatch.setattr(hevy_client.urllib.request, "urlopen", failing_urlopen)
    with pytest.raises(RuntimeError, match="Hevy API error 429"):
        hevy_client._hevy_request("/workouts")


def test_template_cache_loads_from_disk_and_fetches_when_missing(monkeypatch, tmp_path) -> None:
    cache_file = tmp_path / "exercise_templates.json"
    monkeypatch.setattr(hevy_client, "_TEMPLATE_CACHE_FILE", cache_file)
    monkeypatch.setattr(
        hevy_client,
        "fetch_exercise_templates",
        lambda: [
            {
                "id": "tmpl-bench",
                "title": "Bench Press",
                "primary_muscle_group": "chest",
                "secondary_muscle_groups": ["triceps"],
                "equipment": "barbell",
            }
        ],
    )

    fetched = hevy_client._load_template_cache()
    assert fetched["tmpl-bench"]["primary"] == "chest"
    assert json.loads(cache_file.read_text())["tmpl-bench"]["secondary"] == ["triceps"]

    cache_file.write_text(json.dumps({"tmpl-squat": {"primary": "quads", "secondary": []}}))
    assert hevy_client._load_template_cache() == {
        "tmpl-squat": {"primary": "quads", "secondary": []}
    }


def test_guess_muscle_groups_prefers_template_data_and_falls_back_to_keywords(monkeypatch) -> None:
    monkeypatch.setattr(
        hevy_client,
        "_get_template_info",
        lambda template_id: {"primary": "lats", "secondary": ["biceps", "biceps"]},
    )
    assert hevy_client._guess_muscle_groups("Unknown", "tmpl") == ["lats", "biceps"]

    monkeypatch.setattr(hevy_client, "_get_template_info", lambda template_id: {})
    assert hevy_client._guess_muscle_groups("Barbell Bench Press") == ["chest", "triceps"]
    assert hevy_client._guess_muscle_groups("Mystery Movement") == ["unknown"]


@pytest.mark.parametrize(
    ("exercise", "expected"),
    [
        ("Bench Press", "push"),
        ("Lat Pulldown", "pull"),
        ("Back Squat", "legs"),
        ("Cable Crunch", "core"),
        ("Treadmill Run", "cardio"),
        ("Mobility Drill", "other"),
    ],
)
def test_movement_pattern_classifies_common_exercises(exercise: str, expected: str) -> None:
    assert hevy_client._movement_pattern(exercise) == expected


def test_muscle_frequency_volume_and_indirect_counts_recent_workouts(monkeypatch) -> None:
    monkeypatch.setattr(hevy_client, "_template_lookup", {})
    workouts = [
        _workout(1, exercise=_exercise("Bench Press", sets=[_set(100, 5)])),
        _workout(20, exercise=_exercise("Back Squat", sets=[_set(120, 5)])),
    ]

    assert hevy_client.muscle_group_frequency(workouts, days=14) == {"chest": 1, "triceps": 1}
    assert hevy_client.muscle_group_volume(workouts, days=14) == {"chest": 500.0, "triceps": 500.0}
    assert hevy_client.muscle_group_frequency_with_indirect(workouts, days=14) == {
        "chest": {"direct": 1, "indirect": 0.0, "total": 1.0},
        "shoulders": {"direct": 0, "indirect": 0.1, "total": 0.1},
        "triceps": {"direct": 1, "indirect": 0.2, "total": 1.2},
    }


def test_movement_pattern_balance_reports_imbalances_and_empty_period(monkeypatch) -> None:
    monkeypatch.setattr(hevy_client, "_template_lookup", {})
    push_heavy = _workout(1, exercise=_exercise("Bench Press", sets=[_set(100, 10)]))
    pull_light = _workout(1, exercise=_exercise("Barbell Row", sets=[_set(10, 10)]))

    balance = hevy_client.movement_pattern_balance([push_heavy, pull_light], days=14)

    assert balance["push"]["volume_kg"] == 1000
    assert balance["pull"]["volume_kg"] == 100
    assert "Push volume is 10.0x pull volume (imbalanced)" in balance["_flags"]
    assert "No leg work detected" in balance["_flags"]
    assert hevy_client.movement_pattern_balance([], days=14) == {
        "message": "No workout data in the last 14 days"
    }


def test_exercise_progression_tracks_matching_sets_in_chronological_order(monkeypatch) -> None:
    monkeypatch.setattr(hevy_client, "_template_lookup", {})
    workouts = [
        _workout(1, exercise=_exercise("Bench Press", sets=[_set(90, 3)])),
        _workout(5, exercise=_exercise("Bench Press", sets=[_set(80, 5)])),
        _workout(2, exercise=_exercise("Squat", sets=[_set(120, 5)])),
    ]

    progression = hevy_client.exercise_progression(workouts, "bench")

    assert [point["max_weight_kg"] for point in progression] == [80, 90]
    assert [point["sets"] for point in progression] == [1, 1]


def test_workout_summary_reports_recent_training_and_no_workout_message(monkeypatch) -> None:
    monkeypatch.setattr(hevy_client, "_template_lookup", {})
    recent = _workout(1, exercise=_exercise("Bench Press", sets=[_set(80, 5), _set(70, 8)]))
    old = _workout(40, exercise=_exercise("Squat", sets=[_set(120, 5)]))

    summary = hevy_client.workout_summary([recent, old], days=14)

    assert summary["workouts"] == 1
    assert summary["total_volume_kg"] == 960
    assert summary["total_time_min"] == 45
    assert summary["unique_exercises"] == 1
    assert summary["muscle_frequency"] == {"chest": 1, "triceps": 1}
    assert summary["muscle_volume"] == {"chest": 960.0, "triceps": 960.0}
    assert "quads" in summary["potentially_ignored_muscles"]
    assert hevy_client.workout_summary([], days=7) == {
        "workouts": 0,
        "message": "No workouts in the last 7 days.",
    }


def test_detect_recovery_issues_requires_history_and_reports_drops(monkeypatch) -> None:
    monkeypatch.setattr(hevy_client, "_template_lookup", {})
    strong_sessions = [
        _workout(5, exercise=_exercise("Bench Press", "bench", [_set(100, 5)])),
        _workout(4, exercise=_exercise("Bench Press", "bench", [_set(100, 5)])),
        _workout(3, exercise=_exercise("Bench Press", "bench", [_set(100, 5)])),
    ]
    weak_latest = _workout(1, exercise=_exercise("Bench Press", "bench", [_set(70, 3)]))

    issue = hevy_client.detect_recovery_issues(strong_sessions + [weak_latest], "bench", 3)

    assert issue is not None
    assert issue["exercise"] == "Bench Press"
    assert any("estimated 1RM dropped" in text for text in issue["issues"])
    assert any("volume dropped" in text for text in issue["issues"])
    assert hevy_client.detect_recovery_issues([weak_latest], "bench", 3) is None
    assert hevy_client.detect_recovery_issues(strong_sessions + [weak_latest], "squat", 3) is None


def test_fetch_templates_and_exercise_history_paginate(monkeypatch) -> None:
    def fake_request(path, params):
        page = int(params["page"])
        if path == "/exercise_templates":
            batch = [{"id": f"t{page}-{i}"} for i in range(10 if page == 1 else 2)]
            return {"exercise_templates": batch}
        if path == "/exercise_history/template-1":
            return {
                "history": [{"page": page, "index": i} for i in range(10 if page == 1 else 2)]
                if page < 3
                else []
            }
        raise AssertionError(path)

    monkeypatch.setattr(hevy_client, "_hevy_request", fake_request)

    assert len(hevy_client.fetch_exercise_templates()) == 12
    history = hevy_client.fetch_exercise_history("template-1")
    assert len(history) == 12
    assert {item["page"] for item in history} == {1, 2}


def test_correlate_with_sleep_requires_pairs_and_calculates_correlations(monkeypatch) -> None:
    monkeypatch.setattr(hevy_client, "_template_lookup", {})
    workouts = [
        _workout(6 - idx, exercise=_exercise("Bench Press", sets=[_set(50 + idx * 10, 10)]))
        for idx in range(5)
    ]
    sleep_records = [
        {
            "date": workout.start_time.date().isoformat(),
            "sleep_score": 70 + idx * 5,
            "readiness_score": 60 + idx * 4,
            "hrv_ms": 35 + idx * 3,
            "avg_hr": 60 - idx,
            "deep_sleep_min": 70 + idx * 5,
        }
        for idx, workout in enumerate(workouts)
    ]

    result = hevy_client.correlate_with_sleep(workouts, sleep_records)

    assert result["pairs"] == 5
    assert result["correlations"]["volume_vs_sleep_score"] == pytest.approx(1.0)
    assert hevy_client.correlate_with_sleep(workouts[:2], sleep_records[:2]) == {
        "pairs": 2,
        "message": "Insufficient data for correlation (need >=5 workout+sleep pairs)",
    }
