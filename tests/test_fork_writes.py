"""Fork: the opt-in write tools (docs/fork.md). Fictional data, no network."""

import asyncio
import io
import json
import os
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from google_health_mcp import api, config, db, helpers
from google_health_mcp.fork import scopes, write_tools, writes
from google_health_mcp.tools import google_sync

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
    def types(self):
        return [t for t, _, _ in self.synced]

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
    for cached in {t for _, types in writes.WRITERS.values() for t in types}:
        monkeypatch.setitem(
            google_sync.GOOGLE_SYNC_HANDLERS,
            cached,
            lambda conn, start, end, t=cached: synced.append((t, str(start), str(end))),
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
        assert google.synced == [
            ("food_log", "2026-03-10", "2026-03-10"),
            ("food_entries", "2026-03-10", "2026-03-10"),
        ]

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
        assert google.types == ["food_log", "water_entries"]

    def test_weight(self, google):
        _call(write_tools.health_log_weight, kilograms=70.5, time=_EVENING)
        assert google.writes[0][1:] == (
            "users/me/dataTypes/weight/dataPoints",
            {"weight": {"sampleTime": _SAMPLE, "weightGrams": 70500.0}},
        )
        assert google.types == ["weight", "weight_readings"]

    def test_body_fat(self, google):
        _call(write_tools.health_log_body_fat, percentage=20, time=_EVENING)
        assert google.writes[0][1:] == (
            "users/me/dataTypes/body-fat/dataPoints",
            {"bodyFat": {"sampleTime": _SAMPLE, "percentage": 20}},
        )
        assert google.types == ["weight", "body_fat_readings"]

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
        assert set(google.types) == {"exercises"}

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
        assert google.synced == [
            ("sleep", "2026-03-10", "2026-03-11"),
            ("sleep_sessions", "2026-03-10", "2026-03-11"),
        ]

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
    def test_deletes_by_batch_delete_and_refreshes_the_logged_day(self, google):
        _call(write_tools.health_log_weight, kilograms=70, time=_EVENING)
        google.calls.clear(), google.synced.clear()
        out = _call(
            write_tools.health_delete_logged, data_type="weight", data_point_id="fictional-point-7"
        )
        assert google.writes == [
            (
                "POST",
                "users/me/dataTypes/weight/dataPoints:batchDelete",
                {"names": ["users/me/dataTypes/weight/dataPoints/fictional-point-7"]},
            )
        ]
        assert out["deleted"] == "fictional-point-7" and out["cache_refreshed"] is True
        assert google.synced == [
            ("weight", "2026-03-10", "2026-03-10"),
            ("weight_readings", "2026-03-10", "2026-03-10"),
        ]

    @pytest.mark.parametrize(
        "data_type,point",
        [
            ("steps", "fictional-point-7"),
            ("weight", "../settings"),
            # Well formed, but not a point this server logged: one a tracker
            # recorded is never this tool's to delete.
            ("weight", "fictional-point-8"),
            ("body-fat", "fictional-point-7"),
        ],
    )
    def test_refuses_what_this_server_did_not_create(self, google, data_type, point):
        _call(write_tools.health_log_weight, kilograms=70, time=_EVENING)
        google.calls.clear()
        with pytest.raises(Exception, match="Cannot delete"):
            asyncio.run(write_tools.health_delete_logged(data_type=data_type, data_point_id=point))
        assert google.writes == []

    def test_deleting_sleep_clears_the_cached_session_and_night(self, google):
        _call(write_tools.health_log_sleep, start="2026-03-10T23:00", end="2026-03-11T07:00")
        name = _POINT.format("sleep")
        conn = db.get_db()
        db.save_sleep_session(
            conn, {"session_id": name, "date": "2026-03-11", "record": "{}", "provider": "google"}
        )
        db.save_sleep_session(
            conn,
            {"session_id": name + "0", "date": "2026-03-11", "record": "{}", "provider": "google"},
        )
        db.save_sleep(conn, {"date": "2026-03-11", "total_minutes": 480, "provider": "google"})
        conn.commit(), conn.close()
        # The re-sync finds no session left, so it writes nothing: the sleep
        # sync only upserts, and would leave both rows behind on its own.
        _call(
            write_tools.health_delete_logged, data_type="sleep", data_point_id="fictional-point-7"
        )
        conn = db.get_db()
        sessions = [r[0] for r in conn.execute("SELECT session_id FROM sleep_sessions")]
        nights = conn.execute("SELECT COUNT(*) FROM sleep").fetchone()[0]
        conn.close()
        assert sessions == [name + "0"] and nights == 0
        assert google.types[-2:] == ["sleep", "sleep_sessions"]

    def test_deleting_sleep_keeps_what_an_import_recorded_for_the_night(self, google):
        """A Google upsert leaves the columns it never writes, so a night can
        hold an imported efficiency beside Google's sums: only the sums go."""
        _call(write_tools.health_log_sleep, start="2026-03-10T23:00", end="2026-03-11T07:00")
        conn = db.get_db()
        db.save_sleep(
            conn,
            {
                "date": "2026-03-11",
                "total_minutes": 480,
                "deep_minutes": 90,
                "efficiency": 91,
                "sessions": 1,
                "provider": "google",
            },
        )
        conn.commit(), conn.close()
        _call(
            write_tools.health_delete_logged, data_type="sleep", data_point_id="fictional-point-7"
        )
        conn = db.get_db()
        night = dict(conn.execute("SELECT * FROM sleep").fetchone())
        conn.close()
        assert night["efficiency"] == 91
        assert night["total_minutes"] is night["deep_minutes"] is night["sessions"] is None

    def test_deleting_one_sleep_session_rebuilds_the_night_from_the_rest(self, google, monkeypatch):
        _call(write_tools.health_log_sleep, start="2026-03-11T13:00", end="2026-03-11T14:00")
        conn = db.get_db()
        db.save_sleep(
            conn,
            {
                "date": "2026-03-11",
                "total_minutes": 540,
                "deep_minutes": 90,
                "efficiency": 91,
                "sessions": 2,
                "provider": "google",
            },
        )
        conn.commit(), conn.close()
        left = {
            "name": _POINT.format("sleep") + "0",
            "sleep": {
                "interval": {"endTime": "2026-03-11T11:00:00Z", "endUtcOffset": "-14400s"},
                "summary": {"minutesAsleep": 480},
            },
        }
        monkeypatch.setattr(api, "list_google_data_points", lambda *a, **k: [left])
        monkeypatch.setitem(google_sync.GOOGLE_SYNC_HANDLERS, "sleep", google_sync.sync_sleep)
        _call(
            write_tools.health_delete_logged, data_type="sleep", data_point_id="fictional-point-7"
        )
        conn = db.get_db()
        night = dict(conn.execute("SELECT * FROM sleep").fetchone())
        conn.close()
        assert (night["total_minutes"], night["sessions"]) == (480, 1)
        assert night["deep_minutes"] is None and night["efficiency"] == 91

    def test_every_column_the_sleep_sync_writes_is_withdrawn_on_delete(self, monkeypatch):
        stages = [{"type": t, "minutes": 1} for t in google_sync._SLEEP_STAGE_COLUMNS]
        point = {
            "sleep": {
                "interval": {
                    "startTime": "2026-03-11T03:00:00Z",
                    "endTime": "2026-03-11T11:00:00Z",
                    "endUtcOffset": "-14400s",
                },
                "summary": {"minutesAsleep": 1, "minutesInSleepPeriod": 1, "stagesSummary": stages},
            }
        }
        monkeypatch.setattr(api, "list_google_data_points", lambda *a, **k: [point])
        conn = db.get_db()
        google_sync.sync_sleep(conn, *[datetime(2026, 3, 11).date()] * 2)
        row = conn.execute("SELECT * FROM sleep").fetchone()
        conn.close()
        written = {k for k in row.keys() if row[k] is not None} - {"date", "provider"}
        assert written == set(writes._GOOGLE_SLEEP_COLUMNS)


class TestTheCacheRefresh:
    def test_refuses_to_log_what_it_could_not_record_as_its_own(self, google, monkeypatch):
        """A point created while the ledger is out of reach could never be
        deleted here, so nothing is sent until it is known to be writable."""

        def broken(*_a, **_k):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(db, "get_db", broken)
        with pytest.raises(Exception, match="nothing was logged"):
            asyncio.run(write_tools.health_log_weight(kilograms=70, time=_EVENING))
        assert google.writes == []

    def test_a_logged_point_stays_deletable_when_its_refresh_fails(self, google, monkeypatch):
        def failing(conn, start, end):
            raise sqlite3.OperationalError("disk I/O error")

        working = google_sync.GOOGLE_SYNC_HANDLERS["weight"]
        monkeypatch.setitem(google_sync.GOOGLE_SYNC_HANDLERS, "weight", failing)
        out = _call(write_tools.health_log_weight, kilograms=70, time=_EVENING)
        assert out["data_point_id"] == "fictional-point-7" and out["cache_refreshed"] is False
        monkeypatch.setitem(google_sync.GOOGLE_SYNC_HANDLERS, "weight", working)
        google.calls.clear()
        _call(
            write_tools.health_delete_logged, data_type="weight", data_point_id="fictional-point-7"
        )
        assert len(google.writes) == 1

    def test_a_failing_sync_still_returns_the_created_id(self, google, monkeypatch):
        def failing(conn, start, end):
            raise api.HealthNetworkError("Network error.")

        monkeypatch.setitem(google_sync.GOOGLE_SYNC_HANDLERS, "weight_readings", failing)
        out = _call(write_tools.health_log_weight, kilograms=70, time=_EVENING)
        assert out["data_point_id"] == "fictional-point-7" and out["cache_refreshed"] is False

    def test_refreshes_the_account_day_even_when_the_host_is_a_day_behind(self, google):
        """Tokyo is past midnight while this host is still on the day before."""
        google.zone = "Asia/Tokyo"
        tomorrow = (datetime.now(ZoneInfo("Asia/Tokyo")) + timedelta(days=1)).date()
        _call(write_tools.health_log_weight, kilograms=70, time=f"{tomorrow}T00:30")
        assert google.synced[-1] == ("weight_readings", str(tomorrow), str(tomorrow))

    def test_a_workout_on_a_day_an_import_recorded_is_cached_beside_it(self, google, monkeypatch):
        """Upstream leaves a day an import recorded to the import, to keep a
        workout both sources hold from being stored twice. A workout this
        server just logged is in no import, so it is stored all the same."""
        conn = db.get_db()
        conn.execute(
            "INSERT INTO exercises (log_id, date) VALUES ('fictional-import-1', '2026-03-10')"
        )
        conn.commit(), conn.close()
        workout = {
            "name": _POINT.format("exercise"),
            "exercise": {
                "interval": {"startTime": "2026-03-10T11:00:00Z", "startUtcOffset": "-14400s"},
                "exerciseType": "RUNNING",
            },
        }
        other = {**workout, "name": _POINT.format("exercise") + "0"}
        monkeypatch.setattr(api, "list_google_data_points", lambda *a, **k: [workout, other])
        monkeypatch.setitem(
            google_sync.GOOGLE_SYNC_HANDLERS, "exercises", google_sync.sync_exercises
        )
        args = {"exercise_type": "running", "start": "2026-03-10T07:00", "end": "2026-03-10T07:30"}
        assert _call(write_tools.health_log_exercise, **args)["cache_refreshed"] is True

        conn = db.get_db()
        # A later sync of the day leaves it, as Google still holds it.
        google_sync.sync_exercises(conn, *[datetime(2026, 3, 10).date()] * 2)
        rows = conn.execute("SELECT log_id, provider FROM exercises ORDER BY log_id").fetchall()
        conn.close()
        # Only the logged workout: the day's other Google workout stays the import's.
        assert [tuple(r) for r in rows] == [
            ("fictional-import-1", None),
            (_POINT.format("exercise"), "google"),
        ]

    def test_a_workout_the_sync_stored_is_called_refreshed(self, google, monkeypatch):
        def store(conn, start, end):
            conn.execute(
                "INSERT INTO exercises (log_id, date, provider) VALUES (?, ?, 'google')",
                (_POINT.format("exercise"), str(start)),
            )

        monkeypatch.setitem(google_sync.GOOGLE_SYNC_HANDLERS, "exercises", store)
        args = {"exercise_type": "running", "start": "2026-03-10T07:00", "end": "2026-03-10T07:30"}
        assert _call(write_tools.health_log_exercise, **args)["cache_refreshed"] is True


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
