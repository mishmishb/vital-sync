import json
from datetime import date

from vital_sync import oura_client


class FrozenDate(date):
    @classmethod
    def today(cls) -> date:
        return cls(2026, 5, 22)


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


def test_get_token_prefers_environment(monkeypatch) -> None:
    monkeypatch.setenv("OURA_API_KEY", "env-token")

    assert oura_client._get_token() == "env-token"


def test_get_token_reads_local_dotenv_before_home(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("OURA_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    key_name = "OURA" + "_API_KEY"
    (tmp_path / ".env").write_text(f"OTHER=value\n{key_name}='dotenv-token'\n")

    assert oura_client._get_token() == "dotenv-token"


def test_get_builds_authenticated_url_and_decodes_json(monkeypatch) -> None:
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.header_items())
        captured["timeout"] = timeout
        return FakeResponse({"data": [{"day": "2026-05-22"}]})

    monkeypatch.setattr(oura_client, "_get_token", lambda: "token-123")
    monkeypatch.setattr(oura_client.urllib.request, "urlopen", fake_urlopen)

    payload = oura_client._get(
        "daily_activity", {"start_date": "2026-05-20", "end_date": "2026-05-22"}
    )

    assert payload == {"data": [{"day": "2026-05-22"}]}
    assert captured == {
        "url": "https://api.ouraring.com/v2/usercollection/daily_activity?"
        "start_date=2026-05-20&end_date=2026-05-22",
        "headers": {"Authorization": "Bearer token-123"},
        "timeout": 30,
    }


def test_fetch_helpers_use_expected_windows_and_day_mappings(monkeypatch) -> None:
    calls = []

    def fake_get(endpoint, params):
        calls.append((endpoint, params))
        if endpoint == "daily_activity":
            return {"data": [{"day": "2026-05-21", "score": 77, "steps": 9000}]}
        if endpoint == "workout":
            return {"data": [{"day": "2026-05-21", "activity": "walking"}]}
        raise AssertionError(endpoint)

    monkeypatch.setattr(oura_client, "date", FrozenDate)
    monkeypatch.setattr(oura_client, "_get", fake_get)

    assert oura_client.fetch_daily_activity(days=2) == {
        "2026-05-21": {"day": "2026-05-21", "score": 77, "steps": 9000}
    }
    assert oura_client.fetch_workouts(days=2) == [{"day": "2026-05-21", "activity": "walking"}]
    assert calls == [
        ("daily_activity", {"start_date": "2026-05-20", "end_date": "2026-05-22"}),
        ("workout", {"start_date": "2026-05-20", "end_date": "2026-05-22"}),
    ]


def test_artifact_only_records_keep_daily_readiness_and_activity() -> None:
    records = oura_client.sleep_periods_to_records(
        [{"day": "2026-05-22", "total_sleep_duration": 900}],
        {"2026-05-22": {"score": 40}},
        {"2026-05-22": {"score": 72}},
        {"2026-05-22": {"score": 81, "steps": 6543}},
    )

    assert records["2026-05-22"] == {
        "date": "2026-05-22",
        "sources": ["oura"],
        "sleep_score": 40,
        "readiness_score": 72,
        "activity_score": 81,
        "steps": 6543,
    }
