"""Fork: the opt-in write tools (docs/fork.md). Fictional data, no network."""

import asyncio
import io
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

import pytest

from google_health_mcp import api, config, helpers
from google_health_mcp.fork import scopes, write_tools, writes
from google_health_mcp.tools import sync_tools

_POINT = "users/fictional-user-1/dataTypes/{}/dataPoints/fictional-point-7"


class FakeGoogle:
    """Serves the settings read and the write calls; records every request."""

    def __init__(self, zone="America/New_York", error=None):
        self.zone, self.error, self.calls = zone, error, []

    def __call__(self, req, timeout=None):
        body = json.loads(req.data) if req.data else None
        url = req.full_url.removeprefix(config.GOOGLE_API_BASE + "/")
        self.calls.append((req.get_method(), url, body))
        if url == "users/me/settings":
            return io.BytesIO(json.dumps({"timeZone": self.zone}).encode())
        if self.error:
            raise self.error
        data_type = url.split("/")[3].split(":")[0]
        name = _POINT.format(data_type)
        return io.BytesIO(json.dumps({"done": True, "response": {"name": name}}).encode())

    @property
    def writes(self):
        return [c for c in self.calls if c[1] != "users/me/settings"]


@pytest.fixture
def google(tmp_path, monkeypatch):
    for name in ("GOOGLE_CLIENT_PATH", "GOOGLE_TOKENS_PATH"):
        path = tmp_path / f"{name}.json"
        path.write_text("{}")
        monkeypatch.setattr(helpers, name, path)
    monkeypatch.setattr(config, "WRITE_MODE", True)
    monkeypatch.setattr(api, "refresh_google_token", lambda: "fictional-token")
    monkeypatch.setattr(writes, "refresh_google_token", lambda: "fictional-token")
    synced = []
    monkeypatch.setattr(
        sync_tools,
        "run_sync",
        lambda types, since=None, until=None, **_: (
            synced.append((types, since, until)) or {t: {"status": "ok"} for t in types}
        ),
    )
    fake = FakeGoogle()
    fake.synced = synced
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return fake


def _call(tool, **kwargs) -> dict:
    return json.loads(asyncio.run(tool(**kwargs)))


# A fixed past evening, after New York's 2026 switch to daylight time on 8 March.
_EVENING = "2026-03-10T22:00"
_SAMPLE = {"physicalTime": "2026-03-11T02:00:00Z", "utcOffset": "-14400s"}
_INSTANT = {
    "startTime": "2026-03-11T02:00:00Z",
    "startUtcOffset": "-14400s",
    "endTime": "2026-03-11T02:00:01Z",
    "endUtcOffset": "-14400s",
}


class TestEachWriteSendsTheDocumentedBody:
    def test_food(self, google):
        out = _call(
            write_tools.health_log_food,
            food="Fictional banana pie",
            time=_EVENING,
            meal_type="dinner",
            calories=420,
            protein_g=6,
            carbs_g=60,
            fat_g=17,
        )
        assert google.writes == [
            (
                "POST",
                "users/me/dataTypes/nutrition-log/dataPoints",
                {
                    "nutritionLog": {
                        "interval": _INSTANT,
                        "foodDisplayName": "Fictional banana pie",
                        "mealType": "DINNER",
                        "energy": {"kcal": 420},
                        "totalCarbohydrate": {"grams": 60},
                        "totalFat": {"grams": 17},
                        "nutrients": [{"nutrient": "PROTEIN", "quantity": {"grams": 6}}],
                    }
                },
            )
        ]
        assert out["data_point_id"] == "fictional-point-7"
        assert out["cache_refreshed"] is True
        assert google.synced == [(["food_log", "food_entries"], "2026-03-10", "2026-03-10")]

    def test_food_sends_only_what_was_given(self, google):
        _call(write_tools.health_log_food, food="Fictional soup", time=_EVENING)
        body = google.writes[0][2]["nutritionLog"]
        assert set(body) == {"interval", "foodDisplayName"}

    def test_water(self, google):
        _call(write_tools.health_log_water, milliliters=500, time=_EVENING)
        assert google.writes[0][1:] == (
            "users/me/dataTypes/hydration-log/dataPoints",
            {"hydrationLog": {"interval": _INSTANT, "amountConsumed": {"milliliters": 500}}},
        )
        assert google.synced[0][0] == ["food_log", "water_entries"]

    def test_weight(self, google):
        _call(write_tools.health_log_weight, kilograms=70.5, time=_EVENING)
        assert google.writes[0][1:] == (
            "users/me/dataTypes/weight/dataPoints",
            {"weight": {"sampleTime": _SAMPLE, "weightGrams": 70500.0}},
        )
        assert google.synced[0][0] == ["weight", "weight_readings"]

    def test_body_fat(self, google):
        _call(write_tools.health_log_body_fat, percentage=20, time=_EVENING)
        assert google.writes[0][1:] == (
            "users/me/dataTypes/body-fat/dataPoints",
            {"bodyFat": {"sampleTime": _SAMPLE, "percentage": 20}},
        )
        assert google.synced[0][0] == ["weight", "body_fat_readings"]

    def test_exercise(self, google):
        _call(
            write_tools.health_log_exercise,
            exercise_type="running",
            start="2026-03-10T07:00",
            end="2026-03-10T07:30",
            calories=300,
            distance_km=5,
        )
        assert google.writes[0][1:] == (
            "users/me/dataTypes/exercise/dataPoints",
            {
                "exercise": {
                    "interval": {
                        "startTime": "2026-03-10T11:00:00Z",
                        "startUtcOffset": "-14400s",
                        "endTime": "2026-03-10T11:30:00Z",
                        "endUtcOffset": "-14400s",
                    },
                    "exerciseType": "RUNNING",
                    "displayName": "Running",
                    "metricsSummary": {"caloriesKcal": 300, "distanceMillimeters": 5000000.0},
                }
            },
        )
        assert google.synced[0][0] == ["exercises"]

    def test_sleep_refreshes_both_nights(self, google):
        _call(write_tools.health_log_sleep, start="2026-03-10T23:00", end="2026-03-11T07:00")
        assert google.writes[0][1:] == (
            "users/me/dataTypes/sleep/dataPoints",
            {
                "sleep": {
                    "interval": {
                        "startTime": "2026-03-11T03:00:00Z",
                        "startUtcOffset": "-14400s",
                        "endTime": "2026-03-11T11:00:00Z",
                        "endUtcOffset": "-14400s",
                    }
                }
            },
        )
        assert google.synced == [(["sleep", "sleep_sessions"], "2026-03-10", "2026-03-11")]

    def test_mood(self, google):
        out = _call(
            write_tools.health_log_mood, moods=["fatigued"], valence="unpleasant", time=_EVENING
        )
        assert google.writes[0][1:] == (
            "users/me/dataTypes/moods/dataPoints",
            {
                "moods": {
                    "sampleTime": _SAMPLE,
                    "moods": ["FATIGUED"],
                    "valences": ["UNPLEASANT"],
                }
            },
        )
        # Write-only: nothing in the cache can hold it, so nothing is synced.
        assert out["cache_refreshed"] is False and google.synced == []

    def test_symptoms(self, google):
        _call(write_tools.health_log_symptoms, symptoms=["fatigue", "headache"], time=_EVENING)
        assert google.writes[0][1:] == (
            "users/me/dataTypes/symptoms/dataPoints",
            {"symptoms": {"sampleTime": _SAMPLE, "symptoms": ["FATIGUE", "HEADACHE"]}},
        )

    def test_an_explicit_offset_wins_over_the_account_zone(self, google):
        _call(write_tools.health_log_weight, kilograms=70, time="2026-03-10T22:00+01:00")
        assert google.writes[0][2]["weight"]["sampleTime"] == {
            "physicalTime": "2026-03-10T21:00:00Z",
            "utcOffset": "3600s",
        }


class TestDelete:
    def test_deletes_by_batch_delete_and_refreshes_the_day(self, google):
        out = _call(
            write_tools.health_delete_logged,
            data_type="weight",
            data_point_id="fictional-point-7",
            date="2026-03-10",
        )
        assert google.writes == [
            (
                "POST",
                "users/me/dataTypes/weight/dataPoints:batchDelete",
                {"names": ["users/me/dataTypes/weight/dataPoints/fictional-point-7"]},
            )
        ]
        assert out["deleted"] == "fictional-point-7" and out["cache_refreshed"] is True
        assert google.synced == [(["weight", "weight_readings"], "2026-03-10", "2026-03-10")]

    @pytest.mark.parametrize(
        "data_type,point", [("steps", "fictional-point-7"), ("weight", "../settings")]
    )
    def test_refuses_what_this_server_does_not_write(self, google, data_type, point):
        with pytest.raises(Exception, match="Cannot delete"):
            asyncio.run(write_tools.health_delete_logged(data_type=data_type, data_point_id=point))
        assert google.writes == []


class TestRefusals:
    @pytest.mark.parametrize(
        "tool,kwargs",
        [
            (write_tools.health_log_weight, {"kilograms": -1}),
            (write_tools.health_log_body_fat, {"percentage": 120}),
            (write_tools.health_log_food, {"food": "x", "meal_type": "brunch"}),
            (write_tools.health_log_mood, {"moods": ["elated-ish"]}),
            (write_tools.health_log_symptoms, {"symptoms": []}),
            (write_tools.health_log_weight, {"kilograms": 70, "time": "yesterday"}),
            (
                write_tools.health_log_sleep,
                {"start": "2026-03-11T07:00", "end": "2026-03-10T23:00"},
            ),
        ],
    )
    def test_a_bad_argument_sends_nothing(self, google, tool, kwargs):
        with pytest.raises(Exception):
            asyncio.run(tool(**kwargs))
        assert google.writes == []

    def test_an_error_names_the_operation_and_never_the_body(self, google):
        google.error = urllib.error.HTTPError(
            "u",
            400,
            "Bad Request",
            {},
            io.BytesIO(b'{"error": {"message": "70.5 invalid for users/fictional-user-1"}}'),
        )
        with pytest.raises(Exception) as caught:
            asyncio.run(write_tools.health_log_weight(kilograms=70.5, time=_EVENING))
        message = str(caught.value)
        assert message.startswith("API error 400 for weight create")
        assert "70.5" not in message and "users/" not in message

    def test_a_server_error_is_not_resent(self, google):
        google.error = urllib.error.HTTPError("u", 503, "Unavailable", {}, io.BytesIO(b""))
        with pytest.raises(Exception, match="check before logging it again"):
            asyncio.run(write_tools.health_log_water(milliliters=250, time=_EVENING))
        assert len(google.writes) == 1

    def test_offline_mode_sends_nothing(self, google, monkeypatch):
        monkeypatch.setattr(config, "OFFLINE_MODE", True)
        out = _call(write_tools.health_log_water, milliliters=250, time="2026-03-10T22:00+00:00")
        assert out["offline_mode"] is True and google.calls == []


def test_every_write_scope_has_a_writer():
    """A writeonly scope nothing writes under is a permission granted for nothing,
    and a writer whose scope is not requested answers 403 at the first log."""
    claimed = [t for types in scopes.WRITE_SCOPE_WRITERS.values() for t in types]
    assert len(claimed) == len(set(claimed)), "a type is claimed by two scopes"
    assert set(claimed) == set(writes.WRITERS)


def _registered(env_value: str | None) -> set[str]:
    env = {k: v for k, v in os.environ.items() if "GOOGLE_HEALTH" not in k}
    if env_value is not None:
        env["GOOGLE_HEALTH_MCP_WRITE"] = env_value
    probe = (
        "import asyncio, json, google_health_mcp.cli;"
        "from google_health_mcp.mcp_instance import mcp;"
        "print(json.dumps([t.name for t in asyncio.run(mcp.list_tools())]))"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, env=env, timeout=60
    )
    assert out.returncode == 0, out.stderr[-500:]
    return set(json.loads(out.stdout))


def test_the_write_tools_are_registered_only_when_on():
    names = {t.__name__ for t in write_tools.WRITE_TOOLS}
    assert len(names) == 9
    assert not names & _registered(None)
    assert names <= _registered("1")
