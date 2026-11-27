"""Heart rate through the day: five-minute buckets cached, re-bucketed on read.

Google stores heart rate a second apart, so the cache keeps its own rollup of
it instead, one local day a row, and the tool answers in coarser buckets
aligned to the local clock. Every value here is fictional.
"""

import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from google_health_mcp import db
from google_health_mcp.tools import google_sync

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Windows has no time.tzset")

_LOCAL = timezone(timedelta(hours=1))


@pytest.fixture(autouse=True)
def _utc_plus_one(monkeypatch):
    """A host one hour east of UTC with no daylight saving, so local days differ from UTC's."""
    monkeypatch.setenv("TZ", "Etc/GMT-1")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def _bucket(start_utc: str, low=50, mean=55.0, high=60, empty=False) -> dict:
    """One five-minute rollup point as Google sends it; `empty` is a window nothing was worn in."""
    begin = datetime.fromisoformat(start_utc)
    point = {
        "startTime": start_utc,
        "endTime": (begin + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
    }
    if not empty:
        point["heartRate"] = {
            "beatsPerMinuteMin": low,
            "beatsPerMinuteAvg": mean,
            "beatsPerMinuteMax": high,
        }
    return point


def _sync(tmp_db, points, start=date(2026, 3, 15), end=date(2026, 3, 16)):
    with patch.object(google_sync.api, "roll_up", return_value=points) as roll_up:
        count = google_sync.sync_heart_rate_intraday(tmp_db, start, end)
    tmp_db.commit()
    return count, roll_up


def _days(tmp_db):
    return db.query_day_lists(tmp_db, "heart_rate_intraday", "2026-01-01", "2026-12-31")


class TestTheSync:
    def test_it_asks_for_five_minute_buckets_between_local_midnights(self, tmp_db):
        """With the day of margin either side that every day-list type asks for."""
        _, roll_up = _sync(tmp_db, [])
        roll_up.assert_called_once_with(
            "heart-rate",
            datetime(2026, 3, 14, tzinfo=_LOCAL),
            datetime(2026, 3, 18, tzinfo=_LOCAL),
            300,
        )

    def test_only_the_days_asked_for_are_stored(self, tmp_db):
        _sync(
            tmp_db,
            [_bucket("2026-03-14T10:00:00Z"), _bucket("2026-03-15T10:00:00Z")],
            start=date(2026, 3, 15),
            end=date(2026, 3, 15),
        )
        assert [d["date"] for d in _days(tmp_db)] == ["2026-03-15"]

    def test_a_bucket_belongs_to_the_local_day_it_starts_on(self, tmp_db):
        # 23:30 UTC on the 15th is 00:30 on the 16th here.
        count, _ = _sync(tmp_db, [_bucket("2026-03-15T10:00:00Z"), _bucket("2026-03-15T23:30:00Z")])
        assert count == 2
        days = _days(tmp_db)
        assert [d["date"] for d in days] == ["2026-03-15", "2026-03-16"]
        assert [p["startTime"] for p in days[1]["points"]] == ["2026-03-15T23:30:00Z"]

    def test_a_window_nothing_was_worn_in_is_not_kept(self, tmp_db):
        _sync(
            tmp_db,
            [_bucket("2026-03-15T10:00:00Z"), _bucket("2026-03-15T10:05:00Z", empty=True)],
        )
        (day,) = _days(tmp_db)
        assert [p["startTime"] for p in day["points"]] == ["2026-03-15T10:00:00Z"]

    def test_a_day_with_nothing_worn_writes_no_row(self, tmp_db):
        count, _ = _sync(tmp_db, [_bucket("2026-03-15T10:00:00Z", empty=True)])
        assert count == 0
        assert _days(tmp_db) == []

    def test_a_resync_replaces_the_days_buckets(self, tmp_db):
        _sync(tmp_db, [_bucket("2026-03-15T10:00:00Z", high=60)])
        _sync(tmp_db, [_bucket("2026-03-15T10:00:00Z", high=64)])
        (day,) = _days(tmp_db)
        assert [p["heartRate"]["beatsPerMinuteMax"] for p in day["points"]] == [64]

    def test_a_point_with_an_unreadable_start_is_skipped(self, tmp_db):
        _sync(tmp_db, [{"startTime": "soon", "heartRate": {}}, _bucket("2026-03-15T10:00:00Z")])
        (day,) = _days(tmp_db)
        assert len(day["points"]) == 1

    def test_the_days_buckets_are_stored_compressed(self, tmp_db):
        _sync(tmp_db, [_bucket("2026-03-15T10:00:00Z")])
        (raw,) = tmp_db.execute("SELECT points FROM heart_rate_intraday").fetchone()
        assert isinstance(raw, bytes)


async def _call(db_path, **kwargs):
    from google_health_mcp.tools.heart_tools import health_get_heart_rate_intraday

    with (
        patch("google_health_mcp.helpers.GOOGLE_CLIENT_PATH") as client,
        patch("google_health_mcp.helpers.GOOGLE_TOKENS_PATH") as tokens,
        patch("google_health_mcp.tools.heart_tools.refresh_before_query") as refresh,
        patch.object(db, "DB_PATH", db_path),
    ):
        client.exists.return_value = True
        tokens.exists.return_value = True
        body = json.loads(await health_get_heart_rate_intraday(**kwargs))
    return body, refresh.call_args_list


def _path(tmp_db):
    return Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])


class TestTheTool:
    async def test_buckets_are_rebucketed_on_the_local_clock(self, tmp_db):
        # 22:00, 22:05 and 22:10 UTC are 23:00-23:15 here; 22:15 starts the next quarter.
        _sync(
            tmp_db,
            [
                _bucket("2026-03-15T22:00:00Z", low=50, mean=54.0, high=58),
                _bucket("2026-03-15T22:05:00Z", low=48, mean=52.0, high=70),
                _bucket("2026-03-15T22:10:00Z", low=51, mean=56.5, high=59),
                _bucket("2026-03-15T22:15:00Z", low=47, mean=49.0, high=51),
            ],
        )
        body, _ = await _call(_path(tmp_db), start="2026-03-15T23:00", end="2026-03-15T23:30")
        assert body["buckets"] == [
            {"time": "2026-03-15T23:00", "min": 48, "avg": 54.2, "max": 70},
            {"time": "2026-03-15T23:15", "min": 47, "avg": 49.0, "max": 51},
        ]
        assert body["resolution_minutes"] == 15

    async def test_the_summary_says_when_the_peak_was(self, tmp_db):
        _sync(
            tmp_db,
            [
                _bucket("2026-03-15T22:00:00Z", low=50, mean=54.0, high=58),
                _bucket("2026-03-15T23:40:00Z", low=60, mean=66.0, high=72),
            ],
        )
        body, _ = await _call(
            _path(tmp_db), start="2026-03-15T23:00", end="2026-03-16T01:00", resolution_minutes=60
        )
        assert body["summary"] == {"min": 50, "avg": 60.0, "max": 72, "max_at": "2026-03-16T00:00"}

    async def test_the_window_is_cut_to_the_times_asked_for(self, tmp_db):
        _sync(tmp_db, [_bucket("2026-03-15T21:55:00Z"), _bucket("2026-03-15T22:00:00Z")])
        body, _ = await _call(_path(tmp_db), start="2026-03-15T23:00", end="2026-03-15T23:30")
        assert [b["time"] for b in body["buckets"]] == ["2026-03-15T23:00"]

    async def test_a_utc_time_is_read_as_that_instant(self, tmp_db):
        _sync(tmp_db, [_bucket("2026-03-15T22:00:00Z")])
        body, _ = await _call(
            _path(tmp_db), start="2026-03-15T22:00:00Z", end="2026-03-15T22:30:00Z"
        )
        assert [b["time"] for b in body["buckets"]] == ["2026-03-15T23:00"]

    async def test_a_date_alone_is_the_whole_local_day(self, tmp_db):
        # 23:30 UTC on the 14th is 00:30 on the 15th here; 23:00 UTC on the 15th is the 16th.
        _sync(
            tmp_db,
            [
                _bucket("2026-03-14T23:30:00Z"),
                _bucket("2026-03-15T22:55:00Z"),
                _bucket("2026-03-15T23:00:00Z"),
            ],
            start=date(2026, 3, 14),
        )
        body, calls = await _call(_path(tmp_db), start="2026-03-15", resolution_minutes=60)
        assert [b["time"] for b in body["buckets"]] == ["2026-03-15T00:00", "2026-03-15T23:00"]
        assert {(c.args[1], c.args[2]) for c in calls} == {(date(2026, 3, 15), date(2026, 3, 15))}

    async def test_the_default_window_is_the_latest_sleep(self, tmp_db):
        db.save_sleep(
            tmp_db,
            {
                "date": "2026-03-16",
                "start_time": "2026-03-15T22:00:00Z",
                "end_time": "2026-03-16T06:00:00Z",
            },
        )
        db.save_sleep(tmp_db, {"date": "2026-03-10", "start_time": "2026-03-09T22:00:00Z"})
        tmp_db.commit()
        _sync(tmp_db, [_bucket("2026-03-15T21:55:00Z"), _bucket("2026-03-15T22:00:00Z")])
        body, calls = await _call(_path(tmp_db))
        assert body["start"] == "2026-03-15T23:00"
        assert body["end"] == "2026-03-16T07:00"
        assert [b["time"] for b in body["buckets"]] == ["2026-03-15T23:00"]
        assert {c.args[0] for c in calls} == {"heart_rate_intraday"}
        assert {(c.args[1], c.args[2]) for c in calls} == {(date(2026, 3, 15), date(2026, 3, 16))}

    async def test_live_reaches_the_refresh(self, tmp_db):
        _, calls = await _call(_path(tmp_db), start="2026-03-15", live=True)
        assert calls and all(c.args[3] is True for c in calls)

    async def test_more_than_a_week_is_refused(self, tmp_db):
        with pytest.raises(ToolError):
            await _call(_path(tmp_db), start="2026-03-01", end="2026-03-15")

    async def test_an_end_before_the_start_is_refused(self, tmp_db):
        with pytest.raises(ToolError):
            await _call(_path(tmp_db), start="2026-03-15T23:00", end="2026-03-15T22:00")

    async def test_an_unreadable_time_is_refused(self, tmp_db):
        with pytest.raises(ToolError):
            await _call(_path(tmp_db), start="last night")

    async def test_an_empty_window_says_so(self, tmp_path):
        body, _ = await _call(tmp_path / "empty.db", start="2026-03-15")
        assert body["message"] == "No intraday heart rate found for this period."

    async def test_a_resolution_outside_the_schema_is_refused(self):
        from google_health_mcp.mcp_instance import mcp

        # Refused by the argument schema, before the tool or its auth gate runs.
        with pytest.raises(Exception, match="resolution_minutes"):
            await mcp.call_tool(
                "health_get_heart_rate_intraday", {"start": "2026-03-15", "resolution_minutes": 7}
            )
