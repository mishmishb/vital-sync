"""Tests for Hevy API client — API interaction, data parsing, cache, and sync logic."""

import json
from datetime import UTC, datetime

import pytest

from vital_sync import hevy_client

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def fresh_cache_dir(monkeypatch, tmp_path):
    """Redirect cache to a temp directory for isolated tests."""
    monkeypatch.setattr(hevy_client, "HEVY_CACHE_DIR", tmp_path)
    monkeypatch.setattr(hevy_client, "_CACHE_FILE", tmp_path / "workouts.json")
    monkeypatch.setattr(hevy_client, "_META_FILE", tmp_path / "meta.json")
    return tmp_path


@pytest.fixture
def sample_workout_raw():
    """Minimal valid workout JSON from Hevy API."""
    return {
        "id": "abc-123",
        "title": "Test Workout",
        "routine_id": None,
        "description": "",
        "start_time": "2026-04-27T19:48:13+00:00",
        "end_time": "2026-04-27T20:27:52+00:00",
        "updated_at": "2026-04-27T20:27:53.660Z",
        "created_at": "2026-04-27T20:27:53.660Z",
        "exercises": [
            {
                "index": 0,
                "title": "Bench Press (Barbell)",
                "exercise_template_id": "tmpl-1",
                "notes": "",
                "superset_id": None,
                "sets": [
                    {"index": 0, "type": "normal", "weight_kg": 80.0, "reps": 8},
                    {"index": 1, "type": "warmup", "weight_kg": 60.0, "reps": 10},
                ],
            }
        ],
    }


@pytest.fixture
def sample_workout_raw2():
    """A second workout for multi-workout tests."""
    return {
        "id": "def-456",
        "title": "Second Workout",
        "routine_id": None,
        "description": "",
        "start_time": "2026-04-28T10:00:00+00:00",
        "end_time": "2026-04-28T10:45:00+00:00",
        "updated_at": "2026-04-28T10:45:00.000Z",
        "created_at": "2026-04-28T10:45:00.000Z",
        "exercises": [],
    }


# ── Helpers ───────────────────────────────────────────────────────────────────


def _mk_set(index=0, set_type="normal", weight_kg=None, reps=None, rpe=None):
    """Shortcut for creating HevySet with all required fields."""
    return hevy_client.HevySet(
        index=index,
        set_type=set_type,
        weight_kg=weight_kg,
        reps=reps,
        distance_meters=None,
        duration_seconds=None,
        rpe=rpe,
    )


def _mk_exercise(title="Bench", sets=None, template_id="t1"):
    """Shortcut for creating HevyExercise with defaults."""
    return hevy_client.HevyExercise(
        index=0,
        title=title,
        exercise_template_id=template_id,
        notes="",
        superset_id=None,
        sets=sets or [],
    )


# ── HevySet ───────────────────────────────────────────────────────────────────


class TestHevySet:
    def test_volume_computation(self):
        s = _mk_set(weight_kg=80.0, reps=8)
        assert s.volume == 640.0

    def test_volume_zero_when_missing_weight(self):
        s = _mk_set(weight_kg=None, reps=10)
        assert s.volume == 0.0

    def test_volume_zero_when_missing_reps(self):
        s = _mk_set(weight_kg=50.0, reps=None)
        assert s.volume == 0.0

    def test_estimated_1rm_epley(self):
        s = _mk_set(weight_kg=80.0, reps=8)
        assert s.estimated_1rm == pytest.approx(80 * (1 + 8 / 30))

    def test_estimated_1rm_none_when_zero_reps(self):
        s = _mk_set(weight_kg=80.0, reps=0)
        assert s.estimated_1rm is None

    def test_estimated_1rm_none_when_no_weight(self):
        s = _mk_set(weight_kg=None, reps=8)
        assert s.estimated_1rm is None


# ── HevyExercise ──────────────────────────────────────────────────────────────


class TestHevyExercise:
    def test_total_volume(self):
        sets = [_mk_set(weight_kg=60, reps=10), _mk_set(weight_kg=80, reps=8)]
        ex = _mk_exercise(sets=sets)
        assert ex.total_volume == 600 + 640

    def test_max_weight(self):
        sets = [_mk_set(weight_kg=60, reps=10), _mk_set(weight_kg=80, reps=8)]
        ex = _mk_exercise(sets=sets)
        assert ex.max_weight == 80.0

    def test_max_weight_none_with_no_sets(self):
        ex = _mk_exercise(sets=[])
        assert ex.max_weight is None

    def test_max_weight_ignores_none_weights(self):
        sets = [_mk_set(weight_kg=None, reps=10), _mk_set(weight_kg=50, reps=8)]
        ex = _mk_exercise(sets=sets)
        assert ex.max_weight == 50.0

    def test_best_estimated_1rm(self):
        sets = [
            _mk_set(weight_kg=60, reps=10),  # 60*(1+10/30) = 80
            _mk_set(weight_kg=80, reps=5),  # 80*(1+5/30) = 93.3
        ]
        ex = _mk_exercise(sets=sets)
        assert ex.best_estimated_1rm == pytest.approx(80 * (1 + 5 / 30))


# ── _parse_workout ────────────────────────────────────────────────────────────


class TestParseWorkout:
    def test_parses_full_workout(self, sample_workout_raw):
        w = hevy_client._parse_workout(sample_workout_raw)
        assert w.id == "abc-123"
        assert w.title == "Test Workout"
        assert w.start_time == datetime(2026, 4, 27, 19, 48, 13, tzinfo=UTC)
        assert w.end_time == datetime(2026, 4, 27, 20, 27, 52, tzinfo=UTC)
        assert w.duration_minutes == pytest.approx(39.65, abs=0.01)

    def test_parses_exercises(self, sample_workout_raw):
        w = hevy_client._parse_workout(sample_workout_raw)
        assert len(w.exercises) == 1
        ex = w.exercises[0]
        assert ex.title == "Bench Press (Barbell)"
        assert len(ex.sets) == 2

    def test_total_volume(self, sample_workout_raw):
        w = hevy_client._parse_workout(sample_workout_raw)
        assert w.total_volume == 1240.0

    def test_duration_zero_when_same_start_end(self, sample_workout_raw):
        r = dict(sample_workout_raw)
        r["end_time"] = r["start_time"]
        w = hevy_client._parse_workout(r)
        assert w.duration_minutes == 0.0

    def test_missing_exercises_field(self):
        raw = {
            "id": "min-1",
            "title": "Minimal",
            "start_time": "2026-04-27T19:00:00+00:00",
            "end_time": "2026-04-27T19:30:00+00:00",
            "updated_at": "2026-04-27T19:30:00.000Z",
            "created_at": "2026-04-27T19:30:00.000Z",
        }
        w = hevy_client._parse_workout(raw)
        assert w.exercises == []
        assert w.total_volume == 0.0

    def test_null_routine_id(self, sample_workout_raw):
        w = hevy_client._parse_workout(sample_workout_raw)
        assert w.routine_id is None

    def test_missing_id_raises_keyerror(self):
        raw = {
            "title": "No ID",
            "start_time": "2026-04-27T19:00:00+00:00",
            "end_time": "2026-04-27T19:30:00+00:00",
            "updated_at": "2026-04-27T19:30:00.000Z",
            "created_at": "2026-04-27T19:30:00.000Z",
        }
        with pytest.raises(KeyError):
            hevy_client._parse_workout(raw)

    def test_sets_use_type_field(self):
        raw = {
            "id": "s1",
            "title": "Test",
            "start_time": "2026-04-27T19:00:00+00:00",
            "end_time": "2026-04-27T19:30:00+00:00",
            "updated_at": "2026-04-27T19:30:00.000Z",
            "created_at": "2026-04-27T19:30:00.000Z",
            "exercises": [
                {
                    "index": 0,
                    "title": "Squat",
                    "exercise_template_id": "t1",
                    "notes": "",
                    "sets": [
                        {"index": 0, "type": "warmup", "weight_kg": 60, "reps": 10},
                        {"index": 1, "type": "normal", "weight_kg": 100, "reps": 5},
                    ],
                }
            ],
        }
        w = hevy_client._parse_workout(raw)
        assert w.exercises[0].sets[0].set_type == "warmup"
        assert w.exercises[0].sets[1].set_type == "normal"


# ── Cache save/load round-trip ────────────────────────────────────────────────


class TestCacheRoundTrip:
    def test_save_and_load_preserves_data(self, fresh_cache_dir, sample_workout_raw):
        w = hevy_client._parse_workout(sample_workout_raw)
        hevy_client._save_cache([w])

        loaded, last_sync = hevy_client._load_cache()
        assert len(loaded) == 1
        assert loaded[0].id == w.id
        assert loaded[0].title == w.title
        assert loaded[0].start_time == w.start_time
        assert last_sync is not None

    def test_load_cache_empty_when_no_file(self, fresh_cache_dir):
        loaded, last_sync = hevy_client._load_cache()
        assert loaded == []
        assert last_sync is None

    def test_multiple_workouts_round_trip(
        self, fresh_cache_dir, sample_workout_raw, sample_workout_raw2
    ):
        w1 = hevy_client._parse_workout(sample_workout_raw)
        w2 = hevy_client._parse_workout(sample_workout_raw2)
        hevy_client._save_cache([w1, w2])

        loaded, _ = hevy_client._load_cache()
        assert len(loaded) == 2
        ids = {w.id for w in loaded}
        assert ids == {"abc-123", "def-456"}

    def test_cache_preserves_exercises_and_sets(self, fresh_cache_dir, sample_workout_raw):
        w = hevy_client._parse_workout(sample_workout_raw)
        hevy_client._save_cache([w])

        loaded, _ = hevy_client._load_cache()
        assert len(loaded[0].exercises) == 1
        assert len(loaded[0].exercises[0].sets) == 2
        assert loaded[0].exercises[0].sets[0].weight_kg == 80.0


# ── fetch_all_workouts pagination ─────────────────────────────────────────────


class TestFetchAllWorkouts:
    def test_single_page(self, monkeypatch):
        def fake_request(path, params):
            return {
                "page": 1,
                "page_count": 1,
                "workouts": [
                    {
                        "id": "w1",
                        "title": "Test",
                        "start_time": "2026-04-27T19:00:00+00:00",
                        "end_time": "2026-04-27T19:30:00+00:00",
                        "updated_at": "2026-04-27T19:30:00.000Z",
                        "created_at": "2026-04-27T19:30:00.000Z",
                        "exercises": [],
                    }
                ],
            }

        monkeypatch.setattr(hevy_client, "_hevy_request", fake_request)
        workouts = hevy_client.fetch_all_workouts()
        assert len(workouts) == 1
        assert workouts[0].id == "w1"

    def test_multi_page(self, monkeypatch):
        pages = {
            1: {
                "page": 1,
                "page_count": 2,
                "workouts": [
                    {
                        "id": "w1",
                        "title": "First",
                        "start_time": "2026-04-28T10:00:00+00:00",
                        "end_time": "2026-04-28T10:30:00+00:00",
                        "updated_at": "2026-04-28T10:30:00.000Z",
                        "created_at": "2026-04-28T10:30:00.000Z",
                        "exercises": [],
                    }
                ],
            },
            2: {
                "page": 2,
                "page_count": 2,
                "workouts": [
                    {
                        "id": "w2",
                        "title": "Second",
                        "start_time": "2026-04-27T19:00:00+00:00",
                        "end_time": "2026-04-27T19:30:00+00:00",
                        "updated_at": "2026-04-27T19:30:00.000Z",
                        "created_at": "2026-04-27T19:30:00.000Z",
                        "exercises": [],
                    }
                ],
            },
        }

        def fake_request(path, params):
            page = int(params["page"])
            return pages[page]

        monkeypatch.setattr(hevy_client, "_hevy_request", fake_request)
        workouts = hevy_client.fetch_all_workouts()
        assert len(workouts) == 2

    def test_since_filter_filters_older(self, monkeypatch):
        def fake_request(path, params):
            return {
                "page": 1,
                "page_count": 1,
                "workouts": [
                    {
                        "id": "w-new",
                        "title": "New",
                        "start_time": "2026-04-28T10:00:00+00:00",
                        "end_time": "2026-04-28T10:30:00+00:00",
                        "updated_at": "2026-04-28T10:30:00.000Z",
                        "created_at": "2026-04-28T10:30:00.000Z",
                        "exercises": [],
                    },
                    {
                        "id": "w-old",
                        "title": "Old",
                        "start_time": "2026-04-20T10:00:00+00:00",
                        "end_time": "2026-04-20T10:30:00+00:00",
                        "updated_at": "2026-04-20T10:30:00.000Z",
                        "created_at": "2026-04-20T10:30:00.000Z",
                        "exercises": [],
                    },
                ],
            }

        monkeypatch.setattr(hevy_client, "_hevy_request", fake_request)
        since = datetime(2026, 4, 25, tzinfo=UTC)
        workouts = hevy_client.fetch_all_workouts(since=since)
        assert len(workouts) == 1
        assert workouts[0].id == "w-new"

    def test_empty_response(self, monkeypatch):
        monkeypatch.setattr(
            hevy_client,
            "_hevy_request",
            lambda path, params: {"page": 1, "page_count": 0, "workouts": []},
        )
        workouts = hevy_client.fetch_all_workouts()
        assert workouts == []

    def test_all_old_page_stops_pagination(self, monkeypatch):
        pages_called = []

        def fake_request(path, params):
            pages_called.append(params.get("page", "?"))
            return {
                "page": int(params["page"]),
                "page_count": 3,
                "workouts": [
                    {
                        "id": "w-ancient",
                        "title": "Ancient",
                        "start_time": "2026-04-01T10:00:00+00:00",
                        "end_time": "2026-04-01T10:30:00+00:00",
                        "updated_at": "2026-04-01T10:30:00.000Z",
                        "created_at": "2026-04-01T10:30:00.000Z",
                        "exercises": [],
                    }
                ],
            }

        monkeypatch.setattr(hevy_client, "_hevy_request", fake_request)
        since = datetime(2026, 4, 25, tzinfo=UTC)
        workouts = hevy_client.fetch_all_workouts(since=since)
        assert workouts == []
        assert pages_called == ["1"]


# ── sync_workouts ─────────────────────────────────────────────────────────────


class TestSyncWorkouts:
    def test_full_fetch_when_no_cache(self, fresh_cache_dir, monkeypatch):
        def fake_request(path, params):
            return {
                "page": 1,
                "page_count": 1,
                "workouts": [
                    {
                        "id": "w1",
                        "title": "First Ever",
                        "start_time": "2026-04-27T19:00:00+00:00",
                        "end_time": "2026-04-27T19:30:00+00:00",
                        "updated_at": "2026-04-27T19:30:00.000Z",
                        "created_at": "2026-04-27T19:30:00.000Z",
                        "exercises": [],
                    }
                ],
            }

        monkeypatch.setattr(hevy_client, "_hevy_request", fake_request)
        workouts = hevy_client.sync_workouts()
        assert len(workouts) == 1
        assert workouts[0].id == "w1"
        assert (fresh_cache_dir / "workouts.json").exists()

    def test_force_full_ignores_cache(self, fresh_cache_dir, monkeypatch, sample_workout_raw):
        w_old = hevy_client._parse_workout(sample_workout_raw)
        hevy_client._save_cache([w_old])

        def fake_request(path, params):
            return {
                "page": 1,
                "page_count": 1,
                "workouts": [
                    {
                        "id": "w-new",
                        "title": "New",
                        "start_time": "2026-04-28T10:00:00+00:00",
                        "end_time": "2026-04-28T10:30:00+00:00",
                        "updated_at": "2026-04-28T10:30:00.000Z",
                        "created_at": "2026-04-28T10:30:00.000Z",
                        "exercises": [],
                    }
                ],
            }

        monkeypatch.setattr(hevy_client, "_hevy_request", fake_request)
        workouts = hevy_client.sync_workouts(force_full=True)
        assert len(workouts) == 1
        assert workouts[0].id == "w-new"

    def test_incremental_sync_detects_new_workout(
        self, fresh_cache_dir, monkeypatch, sample_workout_raw
    ):
        w1 = hevy_client._parse_workout(sample_workout_raw)
        hevy_client._save_cache([w1])

        meta_file = fresh_cache_dir / "meta.json"
        old_sync = datetime(2026, 4, 27, tzinfo=UTC)
        meta_file.write_text(json.dumps({"last_sync": old_sync.isoformat()}))

        def fake_request(path, params):
            return {
                "page": 1,
                "page_count": 1,
                "workouts": [
                    {
                        "id": "w-new",
                        "title": "New",
                        "start_time": "2026-04-28T10:00:00+00:00",
                        "end_time": "2026-04-28T10:30:00+00:00",
                        "updated_at": "2026-04-28T10:30:00.000Z",
                        "created_at": "2026-04-28T10:30:00.000Z",
                        "exercises": [],
                    },
                ],
            }

        monkeypatch.setattr(hevy_client, "_hevy_request", fake_request)
        workouts = hevy_client.sync_workouts()
        assert len(workouts) == 2
        ids = {w.id for w in workouts}
        assert "w-new" in ids
        assert "abc-123" in ids

        updated_meta = json.loads(meta_file.read_text())
        assert updated_meta["last_sync"] != old_sync.isoformat()

    def test_incremental_sync_no_changes_keeps_cache(
        self, fresh_cache_dir, monkeypatch, sample_workout_raw
    ):
        w1 = hevy_client._parse_workout(sample_workout_raw)
        hevy_client._save_cache([w1])

        meta_file = fresh_cache_dir / "meta.json"
        recent_sync = datetime(2026, 4, 28, tzinfo=UTC)
        meta_file.write_text(json.dumps({"last_sync": recent_sync.isoformat()}))

        def fake_request(path, params):
            return {
                "page": 1,
                "page_count": 1,
                "workouts": [
                    {
                        "id": "abc-123",
                        "title": "Test Workout",
                        "start_time": "2026-04-27T19:48:13+00:00",
                        "end_time": "2026-04-27T20:27:52+00:00",
                        "updated_at": "2026-04-27T20:27:53.660Z",
                        "created_at": "2026-04-27T20:27:53.660Z",
                        "exercises": [],
                    }
                ],
            }

        monkeypatch.setattr(hevy_client, "_hevy_request", fake_request)
        workouts = hevy_client.sync_workouts()
        assert len(workouts) >= 1
        assert any(w.id == "abc-123" for w in workouts)
