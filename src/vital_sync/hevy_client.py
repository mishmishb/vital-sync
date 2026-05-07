"""
Hevy API client for workout data integration.

Handles authentication, pagination, incremental sync via /events,
and structured parsing of workouts, exercises, sets, and routines.

API docs: https://api.hevyapp.com/docs/
Base URL: https://api.hevyapp.com/v1
Auth: api-key header (UUID, from https://hevy.com/settings?developer)
"""

from __future__ import annotations

import json
import math
import os
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

# ── Configuration ────────────────────────────────────────────────────────────

HEVY_BASE_URL = "https://api.hevyapp.com/v1"
HEVY_CACHE_DIR = Path(
    os.environ.get(
        "VITAL_SYNC_DATA_DIR",
        str(Path.home() / ".vital_sync" / "hevy_cache"),
    )
)
HEVY_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _get_api_key() -> str | None:
    """Read Hevy API key from env or a sidecar file."""
    key = os.environ.get("HEVY_API_KEY")
    if key:
        return key
    for env_path in (Path(".env"), Path.home() / ".hermes" / ".env"):
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("HEVY_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def _hevy_request(path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
    """Make an authenticated GET request to the Hevy API."""
    api_key = _get_api_key()
    if not api_key:
        raise RuntimeError("HEVY_API_KEY not found in env or .env file")

    url = f"{HEVY_BASE_URL}{path}"
    if params:
        query = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{url}?{query}"

    req = urllib.request.Request(
        url,
        headers={
            "api-key": api_key,
            "Accept": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"Hevy API error {e.code}: {body}") from e


# ── Data structures ──────────────────────────────────────────────────────────


@dataclass
class HevySet:
    index: int
    set_type: str  # normal, warmup, drop, failure
    weight_kg: float | None
    reps: int | None
    distance_meters: float | None
    duration_seconds: float | None
    rpe: float | None

    @property
    def volume(self) -> float:
        """Approximate volume: weight * reps. Returns 0 if missing data."""
        if self.weight_kg is not None and self.reps is not None:
            return self.weight_kg * self.reps
        return 0.0

    @property
    def estimated_1rm(self) -> float | None:
        """Epley formula: weight * (1 + reps/30)."""
        if self.weight_kg is None or self.reps is None or self.reps <= 0:
            return None
        return self.weight_kg * (1 + self.reps / 30)


@dataclass
class HevyExercise:
    index: int
    title: str
    exercise_template_id: str
    notes: str
    superset_id: int | None
    sets: list[HevySet]

    @property
    def total_volume(self) -> float:
        return sum(s.volume for s in self.sets)

    @property
    def max_weight(self) -> float | None:
        weights = [s.weight_kg for s in self.sets if s.weight_kg is not None]
        return max(weights) if weights else None

    @property
    def best_estimated_1rm(self) -> float | None:
        e1rms = [s.estimated_1rm for s in self.sets if s.estimated_1rm is not None]
        return max(e1rms) if e1rms else None

    @property
    def muscle_groups(self) -> list[str]:
        """Muscle groups from Hevy's official template data or heuristic fallback."""
        return _guess_muscle_groups(self.title, self.exercise_template_id)


@dataclass
class HevyWorkout:
    id: str
    title: str
    routine_id: str | None
    description: str
    start_time: datetime
    end_time: datetime
    updated_at: datetime
    created_at: datetime
    exercises: list[HevyExercise]

    @property
    def duration_minutes(self) -> float:
        return (self.end_time - self.start_time).total_seconds() / 60

    @property
    def total_volume(self) -> float:
        return sum(e.total_volume for e in self.exercises)

    @property
    def muscle_groups(self) -> set[str]:
        groups: set[str] = set()
        for ex in self.exercises:
            groups.update(ex.muscle_groups)
        return groups


def _parse_workout(raw: dict[str, Any]) -> HevyWorkout:
    """Parse a raw workout dict from the API into a HevyWorkout."""

    def _dt(s: str) -> datetime:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))

    exercises = []
    for ex_raw in raw.get("exercises", []):
        sets = []
        for s_raw in ex_raw.get("sets", []):
            sets.append(
                HevySet(
                    index=s_raw.get("index", 0),
                    set_type=s_raw.get("type", "normal"),
                    weight_kg=s_raw.get("weight_kg"),
                    reps=s_raw.get("reps"),
                    distance_meters=s_raw.get("distance_meters"),
                    duration_seconds=s_raw.get("duration_seconds"),
                    rpe=s_raw.get("rpe"),
                )
            )
        exercises.append(
            HevyExercise(
                index=ex_raw.get("index", 0),
                title=ex_raw.get("title", ""),
                exercise_template_id=ex_raw.get("exercise_template_id", ""),
                notes=ex_raw.get("notes", ""),
                superset_id=ex_raw.get("supersets_id"),
                sets=sets,
            )
        )

    return HevyWorkout(
        id=raw["id"],
        title=raw.get("title", ""),
        routine_id=raw.get("routine_id"),
        description=raw.get("description", ""),
        start_time=_dt(raw["start_time"]),
        end_time=_dt(raw["end_time"]),
        updated_at=_dt(raw["updated_at"]),
        created_at=_dt(raw["created_at"]),
        exercises=exercises,
    )


# ── Muscle group heuristic mapping ───────────────────────────────────────────

_MUSCLE_KEYWORDS: dict[str, list[str]] = {
    "chest": [
        "bench press",
        "chest fly",
        "chest dip",
        "push-up",
        "push up",
        "pec deck",
        "cable cross",
        "incline press",
        "decline press",
        "chest press",
    ],
    "triceps": [
        "tricep",
        "skullcrusher",
        "overhead extension",
        "cable pushdown",
        "diamond push",
        "close grip",
    ],
    "shoulders": [
        "shoulder press",
        "military press",
        "lateral raise",
        "front raise",
        "rear delt",
        "face pull",
        "arnold press",
        "upright row",
    ],
    "back": [
        "lat pulldown",
        "pull-up",
        "pull up",
        "chin-up",
        "chin up",
        "seated row",
        "bent over row",
        "t-bar row",
        "deadlift",
        "rack pull",
        "shrug",
        "back extension",
        "hyperextension",
    ],
    "biceps": ["bicep", "curl", "hammer curl", "preacher curl", "incline curl"],
    "quads": [
        "squat",
        "leg press",
        "leg extension",
        "lunge",
        "split squat",
        "hack squat",
        "goblet squat",
        "front squat",
    ],
    "hamstrings": [
        "romanian deadlift",
        "rdl",
        "leg curl",
        "seated leg curl",
        "lying leg curl",
        "good morning",
        "nordic curl",
        "rear kick",
    ],
    "glutes": [
        "hip thrust",
        "glute bridge",
        "glute kickback",
        "bulgarian split squat",
        "step-up",
        "step up",
        "hip abduction",
    ],
    "calves": [
        "calf raise",
        "calf press",
        "standing calf",
        "seated calf",
        "jump rope",
        "treadmill",
    ],
    "abs": [
        "crunch",
        "plank",
        "leg raise",
        "russian twist",
        "hanging knee",
        "ab wheel",
        "cable crunch",
        "heel tap",
    ],
    "traps": ["shrug", "farmer walk"],
    "forearms": ["wrist curl", "reverse curl", "grip", "farmer walk"],
}


_TEMPLATE_CACHE_FILE = HEVY_CACHE_DIR / "exercise_templates.json"


def _load_template_cache() -> dict[str, dict[str, Any]]:
    """Load cached exercise templates for muscle group lookups."""
    if not _TEMPLATE_CACHE_FILE.exists():
        # Auto-fetch on first use
        templates = fetch_exercise_templates()
        cache = {}
        for t in templates:
            cache[t["id"]] = {
                "title": t["title"],
                "primary": t.get("primary_muscle_group", "unknown"),
                "secondary": t.get("secondary_muscle_groups", []),
                "equipment": t.get("equipment", "unknown"),
            }
        _TEMPLATE_CACHE_FILE.write_text(json.dumps(cache, indent=2))
        return cache
    return json.loads(_TEMPLATE_CACHE_FILE.read_text())


_template_lookup: dict[str, dict[str, Any]] | None = None


def _get_template_info(template_id: str) -> dict[str, Any]:
    """Get exercise template info by ID, with caching."""
    global _template_lookup
    if _template_lookup is None:
        _template_lookup = _load_template_cache()
    return _template_lookup.get(template_id, {})


def _guess_muscle_groups(exercise_title: str, template_id: str | None = None) -> list[str]:
    """
    Get muscle groups for an exercise.
    Uses Hevy's official primary/secondary muscle data when available,
    falls back to keyword heuristics only for unknown templates.
    """
    groups: list[str] = []

    # Try official Hevy template data first
    if template_id:
        info = _get_template_info(template_id)
        primary = info.get("primary")
        if primary and primary != "unknown":
            groups.append(primary)
        for sec in info.get("secondary", []):
            if sec and sec not in groups:
                groups.append(sec)
        if groups:
            return groups

    # Fallback to keyword heuristics
    lower = exercise_title.lower()
    for muscle, keywords in _MUSCLE_KEYWORDS.items():
        if any(kw in lower for kw in keywords):
            groups.append(muscle)

    if not groups:
        return ["unknown"]

    # Compound movement secondary muscles
    if "bench press" in lower and "triceps" not in groups:
        groups.append("triceps")
    if "squat" in lower and "glutes" not in groups:
        groups.append("glutes")
    if "deadlift" in lower and "glutes" not in groups:
        groups.append("glutes")
    if "deadlift" in lower and "traps" not in groups:
        groups.append("traps")
    if "row" in lower and "biceps" not in groups:
        groups.append("biceps")
    if "pull" in lower and "biceps" not in groups:
        groups.append("biceps")
    if "lat pulldown" in lower and "biceps" not in groups:
        groups.append("biceps")
    return list(dict.fromkeys(groups))  # preserve order, dedup


def _movement_pattern(exercise_title: str) -> str:
    """Classify exercise as push, pull, legs, or core/cardio."""
    lower = exercise_title.lower()
    push_signatures = ["bench", "press", "fly", "dip", "push-up", "push up", "chest"]
    pull_signatures = ["row", "pull", "curl", "deadlift", "shrug", "lat pulldown", "chin"]
    leg_signatures = [
        "squat",
        "lunge",
        "leg press",
        "leg extension",
        "leg curl",
        "calf",
        "hip thrust",
        "glute",
        "hack squat",
    ]
    core_signatures = ["crunch", "plank", "leg raise", "twist", "ab wheel", "cable crunch"]
    cardio_signatures = ["treadmill", "jump rope", "running", "cycling", "elliptical"]

    if any(s in lower for s in push_signatures):
        return "push"
    if any(s in lower for s in pull_signatures):
        return "pull"
    if any(s in lower for s in leg_signatures):
        return "legs"
    if any(s in lower for s in core_signatures):
        return "core"
    if any(s in lower for s in cardio_signatures):
        return "cardio"
    return "other"


def _indirect_muscle_stimulus(workouts: list[HevyWorkout], days: int = 14) -> dict[str, float]:
    """
    Estimate indirect muscle stimulus from movement patterns.
    A chest day also stimulates triceps and front delts.
    A back day also stimulates biceps and rear delts.
    Returns a dict of muscle -> indirect_volume (as fraction of direct work).
    """
    cutoff = datetime.now(UTC) - timedelta(days=days)
    indirect: dict[str, float] = defaultdict(float)

    # Multipliers for indirect stimulus from primary muscle work
    INDIRECT_MAP: dict[str, dict[str, float]] = {
        "chest": {"triceps": 0.50, "shoulders": 0.30},
        "back": {"biceps": 0.50, "traps": 0.40, "shoulders": 0.20},
        "shoulders": {"triceps": 0.30, "traps": 0.20},
        "quads": {"glutes": 0.40, "hamstrings": 0.20},
        "hamstrings": {"glutes": 0.30, "back": 0.10},
        "glutes": {"hamstrings": 0.20, "quads": 0.10},
    }

    for w in workouts:
        if w.start_time < cutoff:
            continue
        for ex in w.exercises:
            direct_groups = ex.muscle_groups
            vol = ex.total_volume
            for dg in direct_groups:
                if dg in INDIRECT_MAP:
                    for muscle, fraction in INDIRECT_MAP[dg].items():
                        indirect[muscle] += vol * fraction

    return dict(indirect)


def muscle_group_frequency_with_indirect(
    workouts: list[HevyWorkout], days: int = 14
) -> dict[str, dict[str, Any]]:
    """
    Return muscle group frequency with both direct and indirect stimulus counts.
    Format: {muscle: {"direct": count, "indirect": estimated_count, "total": combined}}
    """
    cutoff = datetime.now(UTC) - timedelta(days=days)
    direct_counts: dict[str, int] = defaultdict(int)
    indirect_counts: dict[str, float] = defaultdict(float)

    # Count direct muscle hits
    for w in workouts:
        if w.start_time >= cutoff:
            for mg in w.muscle_groups:
                if mg != "unknown":
                    direct_counts[mg] += 1

    # Estimate indirect stimulus
    indirect_vol = _indirect_muscle_stimulus(workouts, days=days)
    # Convert volume to approximate session equivalents (very rough: 1000 kg = 1 session)
    for muscle, vol in indirect_vol.items():
        indirect_counts[muscle] = vol / 1000.0

    all_muscles = set(direct_counts.keys()) | set(indirect_counts.keys())
    result = {}
    for muscle in sorted(all_muscles):
        d = direct_counts.get(muscle, 0)
        ind = indirect_counts.get(muscle, 0.0)
        result[muscle] = {
            "direct": d,
            "indirect": round(ind, 1),
            "total": round(d + ind, 1),
        }

    return result


def movement_pattern_balance(workouts: list[HevyWorkout], days: int = 30) -> dict[str, Any]:
    """
    Analyze push/pull/legs/core balance in recent training.
    """
    cutoff = datetime.now(UTC) - timedelta(days=days)
    patterns: dict[str, list[float]] = defaultdict(list)  # pattern -> list of volumes

    for w in workouts:
        if w.start_time < cutoff:
            continue
        for ex in w.exercises:
            pat = _movement_pattern(ex.title)
            patterns[pat].append(ex.total_volume)

    total_vol = sum(sum(vols) for vols in patterns.values())
    if total_vol == 0:
        return {"message": f"No workout data in the last {days} days"}

    result = {}
    for pat, vols in sorted(patterns.items(), key=lambda x: sum(x[1]), reverse=True):
        vol = sum(vols)
        result[pat] = {
            "volume_kg": round(vol, 1),
            "sets": len(vols),
            "pct_of_total": round(vol / total_vol * 100, 1),
        }

    # Flag imbalances
    flags = []
    push_vol = result.get("push", {}).get("volume_kg", 0)
    pull_vol = result.get("pull", {}).get("volume_kg", 0)
    leg_vol = result.get("legs", {}).get("volume_kg", 0)

    if push_vol > 0 and pull_vol > 0:
        ratio = push_vol / pull_vol
        if ratio > 1.5:
            flags.append(f"Push volume is {ratio:.1f}x pull volume (imbalanced)")
        elif ratio < 0.67:
            flags.append(f"Pull volume is {1 / ratio:.1f}x push volume (imbalanced)")
    elif push_vol > 0 and pull_vol == 0:
        flags.append("No pull work detected")
    elif pull_vol > 0 and push_vol == 0:
        flags.append("No push work detected")

    if leg_vol == 0:
        flags.append("No leg work detected")
    elif push_vol > 0 and leg_vol / push_vol < 0.5:
        flags.append("Leg volume is low relative to upper body")

    result["_flags"] = flags
    return result


# ── API operations ───────────────────────────────────────────────────────────


def fetch_all_workouts(since: datetime | None = None) -> list[HevyWorkout]:
    """
    Fetch all workouts, paginating through the API.
    Optionally filter to workouts created/updated since a given datetime.
    """
    workouts: list[HevyWorkout] = []
    page = 1
    while True:
        raw = _hevy_request("/workouts", params={"page": str(page), "pageSize": "10"})
        batch = [_parse_workout(w) for w in raw.get("workouts", [])]
        if not batch:
            break
        if since:
            batch = [w for w in batch if w.updated_at >= since]
            if not batch:
                # If the whole page is before 'since', we could still have older
                # pages with newer items, but Hevy orders newest first, so we
                # can safely stop if the whole page is too old.
                break
        workouts.extend(batch)
        if page >= raw.get("page_count", 1):
            break
        page += 1
    return workouts


def fetch_exercise_templates() -> list[dict[str, Any]]:
    """Fetch all exercise templates (custom + default)."""
    templates: list[dict[str, Any]] = []
    page = 1
    while True:
        raw = _hevy_request("/exercise_templates", params={"page": str(page), "pageSize": "10"})
        batch = raw.get("exercise_templates", [])
        if not batch:
            break
        templates.extend(batch)
        if len(batch) < 10:
            break
        page += 1
    return templates


def fetch_exercise_history(exercise_template_id: str) -> list[dict[str, Any]]:
    """Fetch historical performance for a specific exercise."""
    history: list[dict[str, Any]] = []
    page = 1
    while True:
        raw = _hevy_request(
            f"/exercise_history/{exercise_template_id}",
            params={"page": str(page), "pageSize": "10"},
        )
        batch = raw.get("history", [])
        if not batch:
            break
        history.extend(batch)
        if len(batch) < 10:
            break
        page += 1
    return history


# ── Caching layer ────────────────────────────────────────────────────────────

_CACHE_FILE = HEVY_CACHE_DIR / "workouts.json"
_META_FILE = HEVY_CACHE_DIR / "meta.json"


def _load_cache() -> tuple[list[HevyWorkout], datetime | None]:
    """Load cached workouts and last sync timestamp."""
    if not _CACHE_FILE.exists():
        return [], None
    try:
        raw_list = json.loads(_CACHE_FILE.read_text())
        workouts = [_parse_workout(w) for w in raw_list]
    except Exception:
        return [], None

    last_sync = None
    if _META_FILE.exists():
        try:
            meta = json.loads(_META_FILE.read_text())
            last_sync = datetime.fromisoformat(meta["last_sync"])
        except Exception:
            pass
    return workouts, last_sync


def _save_cache(workouts: list[HevyWorkout]) -> None:
    """Save workouts to cache and update sync metadata."""
    serializable = []
    for w in workouts:
        d = {
            "id": w.id,
            "title": w.title,
            "routine_id": w.routine_id,
            "description": w.description,
            "start_time": w.start_time.isoformat(),
            "end_time": w.end_time.isoformat(),
            "updated_at": w.updated_at.isoformat(),
            "created_at": w.created_at.isoformat(),
            "exercises": [
                {
                    "index": e.index,
                    "title": e.title,
                    "exercise_template_id": e.exercise_template_id,
                    "notes": e.notes,
                    "supersets_id": e.superset_id,
                    "sets": [
                        {
                            "index": s.index,
                            "type": s.set_type,
                            "weight_kg": s.weight_kg,
                            "reps": s.reps,
                            "distance_meters": s.distance_meters,
                            "duration_seconds": s.duration_seconds,
                            "rpe": s.rpe,
                        }
                        for s in e.sets
                    ],
                }
                for e in w.exercises
            ],
        }
        serializable.append(d)
    _CACHE_FILE.write_text(json.dumps(serializable, indent=2))
    _META_FILE.write_text(json.dumps({"last_sync": datetime.now(UTC).isoformat()}, indent=2))


def sync_workouts(force_full: bool = False) -> list[HevyWorkout]:
    """
    Sync workouts from Hevy.

    Uses the /workouts endpoint with client-side since filtering for incremental
    sync. On first run or force_full=True, fetches the full workout history.
    """
    cached, last_sync = _load_cache()

    if force_full or last_sync is None:
        workouts = fetch_all_workouts()
        _save_cache(workouts)
        return workouts

    # Incremental: fetch only workouts updated since last sync.
    # /workouts returns newest-first, so fetch_all_workouts(since=last_sync)
    # stops paginating once a page is entirely older than 'since'.
    fresh = fetch_all_workouts(since=last_sync)

    # Merge: newer workouts replace cached ones by ID.
    # Keep cached workouts that weren't in the fresh batch (either unchanged
    # or older than the 'since' window — we already have those).
    by_id = {w.id: w for w in cached}
    for w in fresh:
        by_id[w.id] = w

    workouts = sorted(by_id.values(), key=lambda w: w.start_time, reverse=True)
    _save_cache(workouts)
    return workouts


# ── Analytics helpers ────────────────────────────────────────────────────────


def muscle_group_frequency(workouts: list[HevyWorkout], days: int = 14) -> dict[str, int]:
    """Count how many times each muscle group was trained in the last N days."""
    cutoff = datetime.now(UTC) - timedelta(days=days)
    counts: dict[str, int] = defaultdict(int)
    for w in workouts:
        if w.start_time >= cutoff:
            for mg in w.muscle_groups:
                counts[mg] += 1
    return dict(counts)


def muscle_group_volume(workouts: list[HevyWorkout], days: int = 14) -> dict[str, float]:
    """Total volume per muscle group in the last N days."""
    cutoff = datetime.now(UTC) - timedelta(days=days)
    volume: dict[str, float] = defaultdict(float)
    for w in workouts:
        if w.start_time < cutoff:
            continue
        for ex in w.exercises:
            for mg in ex.muscle_groups:
                volume[mg] += ex.total_volume
    return dict(volume)


def exercise_progression(
    workouts: list[HevyWorkout], exercise_title_substring: str
) -> list[dict[str, Any]]:
    """Track estimated 1RM and max weight for an exercise over time."""
    points = []
    for w in sorted(workouts, key=lambda x: x.start_time):
        for ex in w.exercises:
            if exercise_title_substring.lower() in ex.title.lower():
                best_1rm = ex.best_estimated_1rm
                max_w = ex.max_weight
                total_vol = ex.total_volume
                if best_1rm or max_w:
                    points.append(
                        {
                            "date": w.start_time.date().isoformat(),
                            "best_1rm_kg": best_1rm,
                            "max_weight_kg": max_w,
                            "total_volume": total_vol,
                            "sets": len(ex.sets),
                        }
                    )
    return points


def workout_summary(workouts: list[HevyWorkout], days: int = 7) -> dict[str, Any]:
    """High-level summary of recent training."""
    cutoff = datetime.now(UTC) - timedelta(days=days)
    recent = [w for w in workouts if w.start_time >= cutoff]

    if not recent:
        return {"workouts": 0, "message": f"No workouts in the last {days} days."}

    total_volume = sum(w.total_volume for w in recent)
    total_time = sum(w.duration_minutes for w in recent)
    unique_exercises = set()
    for w in recent:
        for ex in w.exercises:
            unique_exercises.add(ex.title)

    mg_freq = muscle_group_frequency(recent, days=days)
    mg_vol = muscle_group_volume(recent, days=days)

    # Find potentially undertrained muscles (trained 0 times in period)
    all_muscles = set(_MUSCLE_KEYWORDS.keys())
    trained = set(mg_freq.keys()) - {"unknown"}
    ignored = sorted(all_muscles - trained)

    # Find most frequent
    most_frequent = sorted(mg_freq.items(), key=lambda x: x[1], reverse=True)[:3]

    return {
        "workouts": len(recent),
        "total_volume_kg": round(total_volume, 1),
        "total_time_min": round(total_time, 1),
        "unique_exercises": len(unique_exercises),
        "muscle_frequency": mg_freq,
        "muscle_volume": {k: round(v, 1) for k, v in mg_vol.items()},
        "most_frequent_muscles": most_frequent,
        "potentially_ignored_muscles": ignored,
        "avg_duration_min": round(total_time / len(recent), 1),
    }


def detect_recovery_issues(
    workouts: list[HevyWorkout], exercise_title_substring: str, lookback_workouts: int = 3
) -> dict[str, Any] | None:
    """
    Compare the most recent performance of an exercise against the previous N performances.
    Returns a dict if a significant drop is detected.

    NOTE: Uses exact exercise template IDs for precise tracking, falling back to name
    substring matching only for exercises without enough template-specific history.
    """
    # First try exact template ID matching for precision
    by_template: dict[str, list[dict[str, Any]]] = {}
    for w in sorted(workouts, key=lambda x: x.start_time):
        for ex in w.exercises:
            if exercise_title_substring.lower() in ex.title.lower():
                tid = ex.exercise_template_id
                if tid not in by_template:
                    by_template[tid] = []
                by_template[tid].append(
                    {
                        "date": w.start_time.date(),
                        "title": ex.title,
                        "best_1rm": ex.best_estimated_1rm,
                        "max_weight": ex.max_weight,
                        "volume": ex.total_volume,
                        "sets": len(ex.sets),
                    }
                )

    # Find the template with the most history
    best_template = None
    best_count = 0
    for tid, history in by_template.items():
        if len(history) > best_count:
            best_count = len(history)
            best_template = tid

    if best_template is None:
        return None

    relevant = by_template[best_template]

    if len(relevant) < lookback_workouts + 1:
        return None

    latest = relevant[-1]
    prior = relevant[-(lookback_workouts + 1) : -1]

    avg_prior_1rm = sum(p["best_1rm"] for p in prior if p["best_1rm"]) / max(
        1, sum(1 for p in prior if p["best_1rm"])
    )
    avg_prior_vol = sum(p["volume"] for p in prior) / len(prior)

    issues = []
    if latest["best_1rm"] and avg_prior_1rm and latest["best_1rm"] < avg_prior_1rm * 0.90:
        drop = (avg_prior_1rm - latest["best_1rm"]) / avg_prior_1rm * 100
        issues.append(f"estimated 1RM dropped {drop:.0f}% vs prior {lookback_workouts} sessions")
    if latest["volume"] < avg_prior_vol * 0.80:
        drop = (avg_prior_vol - latest["volume"]) / avg_prior_vol * 100
        issues.append(f"volume dropped {drop:.0f}% vs prior {lookback_workouts} sessions")

    if not issues:
        return None

    return {
        "exercise": latest["title"],
        "latest_date": latest["date"].isoformat(),
        "issues": issues,
        "latest_1rm": latest["best_1rm"],
        "avg_prior_1rm": round(avg_prior_1rm, 1) if avg_prior_1rm else None,
    }


def export_for_cache(workouts: list[HevyWorkout]) -> list[dict[str, Any]]:
    """Export workouts to a flat list suitable for the health data cache."""
    out = []
    for w in workouts:
        for ex in w.exercises:
            for s in ex.sets:
                out.append(
                    {
                        "date": w.start_time.date().isoformat(),
                        "workout_id": w.id,
                        "workout_title": w.title,
                        "exercise": ex.title,
                        "muscle_groups": ex.muscle_groups,
                        "set_type": s.set_type,
                        "weight_kg": s.weight_kg,
                        "reps": s.reps,
                        "rpe": s.rpe,
                        "volume": s.volume,
                        "est_1rm": s.estimated_1rm,
                    }
                )
    return out


# ── Oura correlation bridge ──────────────────────────────────────────────────


def correlate_with_sleep(
    workouts: list[HevyWorkout],
    sleep_records: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Correlate workout metrics with subsequent sleep metrics.
    sleep_records should be a list of dicts with keys like:
      date (YYYY-MM-DD), sleep_score, readiness_score, hrv_ms, avg_hr, etc.
    Returns Pearson-like trends (simple covariance approximations).
    """
    # Build sleep lookup by date
    sleep_by_date = {}
    for sr in sleep_records:
        d = sr.get("date")
        if d:
            sleep_by_date[d] = sr

    pairs = []
    for w in workouts:
        # Sleep after workout = night of workout date (assuming evening workouts)
        # For morning workouts, sleep would be the *next* night
        workout_date = w.start_time.date().isoformat()
        sleep = sleep_by_date.get(workout_date)
        if not sleep:
            # Try next day
            next_day = (w.start_time + timedelta(days=1)).date().isoformat()
            sleep = sleep_by_date.get(next_day)
        if sleep:
            pairs.append(
                {
                    "workout_date": workout_date,
                    "workout_volume": w.total_volume,
                    "workout_duration_min": w.duration_minutes,
                    "workout_muscle_groups": list(w.muscle_groups),
                    "sleep_score": sleep.get("sleep_score"),
                    "readiness_score": sleep.get("readiness_score"),
                    "hrv_ms": sleep.get("hrv_ms"),
                    "avg_hr": sleep.get("avg_hr"),
                    "deep_sleep_min": sleep.get("deep_sleep_min"),
                    "rem_sleep_min": sleep.get("rem_sleep_min"),
                }
            )

    if len(pairs) < 5:
        return {
            "pairs": len(pairs),
            "message": "Insufficient data for correlation (need >=5 workout+sleep pairs)",
        }

    # Simple correlation on volume vs sleep metrics
    def _corr(x_key: str, y_key: str) -> float | None:
        xs = [p[x_key] for p in pairs if p[x_key] is not None and p[y_key] is not None]
        ys = [p[y_key] for p in pairs if p[x_key] is not None and p[y_key] is not None]
        if len(xs) < 5:
            return None
        n = len(xs)
        mean_x = sum(xs) / n
        mean_y = sum(ys) / n
        num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=False))
        den_x = math.sqrt(sum((x - mean_x) ** 2 for x in xs))
        den_y = math.sqrt(sum((y - mean_y) ** 2 for y in ys))
        if den_x == 0 or den_y == 0:
            return None
        return num / (den_x * den_y)

    return {
        "pairs": len(pairs),
        "correlations": {
            "volume_vs_sleep_score": _corr("workout_volume", "sleep_score"),
            "volume_vs_readiness": _corr("workout_volume", "readiness_score"),
            "volume_vs_hrv": _corr("workout_volume", "hrv_ms"),
            "volume_vs_deep_sleep": _corr("workout_volume", "deep_sleep_min"),
            "duration_vs_sleep_score": _corr("workout_duration_min", "sleep_score"),
        },
    }


if __name__ == "__main__":
    # Simple smoke test when run directly
    key = _get_api_key()
    print(f"API key found: {'yes' if key else 'NO - set HEVY_API_KEY'}")
    if key:
        try:
            info = _hevy_request("/user/info")
            print(f"User: {info}")
        except Exception as e:
            print(f"Connection test failed: {e}")
