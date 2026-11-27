"""Heart rate query tools."""

from datetime import date, datetime, timedelta
from typing import Literal

import anyio

from .. import db
from ..errors import InvalidDateError
from ..helpers import empty_answer, format_response, parse_date, require_auth
from ..mcp_instance import mcp
from .sync_tools import refresh_before_query


@mcp.tool(structured_output=False)
@require_auth
async def health_get_heart_rate(
    start_date: str | None = None,
    end_date: str | None = None,
    live: bool = False,
) -> str:
    """Get daily resting heart rate and heart rate zones.

    Returns resting HR and zone breakdown (Out of Range, Fat Burn, Cardio, Peak)
    from the local cache by default, auto-syncing if stale.

    Args:
        start_date: Start date as "YYYY-MM-DD", "YYYY-MM", or "30d". Default: 30
            days back from the end date or today, whichever is earlier.
        end_date: End date as "YYYY-MM-DD", "YYYY-MM" (its last day), or "Nd".
            Default: today.
        live: If true, re-fetch this window from the API before reading the cache.

    Returns one entry per day with resting_hr, zones array, calculation_method
    (Google's WITH_SLEEP or ONLY_WITH_AWAKE_DATA) and data_source (the device and
    platform Google says recorded it).
    Zone data: name, minutes, caloriesOut, max/min HR for each zone. Zones are
    present only on days that arrived by import; a sync does not write them.
    Google's own zone boundaries are in health_get_heart_rate_zones.
    """
    start, end = parse_date(start_date, end_date, default_days=30)

    await anyio.to_thread.run_sync(lambda: refresh_before_query("heart_rate", start, end, live))

    def _query():
        conn = db.get_db()
        try:
            return db.query_heart_rate(conn, start.isoformat(), end.isoformat())
        finally:
            conn.close()

    entries = await anyio.to_thread.run_sync(_query)

    if not entries:
        return empty_answer("No heart rate data found for this period.", live)

    return format_response({"heart_rate": entries, "count": len(entries)})


@mcp.tool(structured_output=False)
@require_auth
async def health_get_heart_rate_zones(
    start_date: str | None = None,
    end_date: str | None = None,
    live: bool = False,
) -> str:
    """Get Google's daily heart-rate zone boundaries.

    Each day's four zones (LIGHT, MODERATE, VIGOROUS, PEAK) with the minimum
    and maximum beats per minute of each, as Google reports them. Boundaries
    only: time spent in each zone is not part of this. health_get_heart_rate
    carries an import's zones, with minutes, on days that arrived that way.

    Args:
        start_date: Start date as "YYYY-MM-DD", "YYYY-MM", or "30d". Default: 30
            days back from the end date or today, whichever is earlier.
        end_date: End date as "YYYY-MM-DD", "YYYY-MM" (its last day), or "Nd".
            Default: today.
        live: If true, re-fetch this window from the API before reading the cache.

    Returns one entry per day with zone_boundaries (the list as Google sent
    it, the bounds as strings).
    """
    start, end = parse_date(start_date, end_date, default_days=30)

    await anyio.to_thread.run_sync(
        lambda: refresh_before_query("heart_rate_zones", start, end, live)
    )

    def _query():
        conn = db.get_db()
        try:
            return db.query_heart_rate_zones(conn, start.isoformat(), end.isoformat())
        finally:
            conn.close()

    entries = await anyio.to_thread.run_sync(_query)

    if not entries:
        return empty_answer("No heart rate zones found for this period.", live)

    return format_response({"heart_rate_zones": entries, "count": len(entries)})


#: The longest window the intraday tool answers, so a reply stays small.
MAX_INTRADAY_DAYS = 7


def _midnight(day: date) -> datetime:
    """The host's local midnight starting `day`, at that date's own offset."""
    return datetime(day.year, day.month, day.day).astimezone()


def _moment(text: str, name: str) -> tuple[datetime, date | None]:
    """A time argument as an aware datetime, and its date when it was a date alone.

    A time with no offset is the host's local time, as is a date's midnight.
    """
    try:
        day = date.fromisoformat(text)
    except ValueError:
        pass
    else:
        return _midnight(day), day
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise InvalidDateError(
            f"Cannot parse {name}. Use YYYY-MM-DD, YYYY-MM-DDTHH:MM, or a time with an offset."
        ) from None
    return (moment if moment.tzinfo else moment.astimezone()), None


def _floor(moment: datetime, minutes: int) -> datetime:
    """The start of the local-clock bucket `moment` falls in, at its own offset.

    The offset is kept so the hour a clock repeats when it goes back stays two
    hours rather than one.
    """
    local = moment.astimezone()
    return local - timedelta(
        minutes=(local.hour * 60 + local.minute) % minutes,
        seconds=local.second,
        microseconds=local.microsecond,
    )


def _ceil(moment: datetime, minutes: int) -> datetime:
    floor = _floor(moment, minutes)
    return floor if floor == moment else floor + timedelta(minutes=minutes)


def _clock(moment: datetime) -> str:
    """A local time to the minute with its offset, unambiguous across a clock change."""
    return moment.astimezone().isoformat(timespec="minutes")


def _latest_sleep() -> tuple[str, str] | None:
    conn = db.get_db()
    try:
        return db.latest_sleep_window(conn)
    finally:
        conn.close()


def _intraday_window(start: str | None, end: str | None, minutes: int) -> tuple[datetime, datetime]:
    """The window asked for, its end exclusive, defaulting to the latest sleep.

    Widened to whole buckets: the cache holds five-minute buckets, so a window
    cutting one could only drop it or read all of it. The reply reports the
    widened window as its start and end.
    """
    first = last = None
    if start is None:
        if end is not None:
            raise InvalidDateError("end needs a start.")
        night = _latest_sleep()
        if night is not None:
            try:
                first, last = _moment(night[0], "start")[0], _moment(night[1], "end")[0]
            except InvalidDateError:
                pass
        if first is None:
            start = date.today().isoformat()
    if first is None:
        first, first_day = _moment(start, "start")
        if end is None:
            last = (
                _midnight(first_day + timedelta(days=1)) if first_day else first + timedelta(days=1)
            )
        else:
            last, last_day = _moment(end, "end")
            if last_day:
                # The next date's own midnight: a local day is not always 24 hours.
                last = _midnight(last_day + timedelta(days=1))
    if last <= first:
        raise InvalidDateError("end must be after start.")
    if last - first > timedelta(days=MAX_INTRADAY_DAYS):
        raise InvalidDateError(f"The window can be at most {MAX_INTRADAY_DAYS} days.")
    return _floor(first, minutes), _ceil(last, minutes)


def _bpm(heart: dict, key: str) -> float | None:
    value = heart.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _number(value: float) -> int | float:
    return int(value) if value == int(value) else round(value, 1)


def _rebucket(days: list[dict], first: datetime, last: datetime, minutes: int) -> list[dict]:
    """The cached five-minute buckets inside [first, last), merged on the local clock."""
    merged: dict[datetime, dict] = {}
    for day in days:
        for point in day.get("points") or ():
            heart = point.get("heartRate") if isinstance(point, dict) else None
            if not isinstance(heart, dict):
                continue
            try:
                moment = datetime.fromisoformat(point.get("startTime"))
            except (TypeError, ValueError):
                continue
            if moment.tzinfo is None or not first <= moment < last:
                continue
            low, mean, high = (
                _bpm(heart, k)
                for k in ("beatsPerMinuteMin", "beatsPerMinuteAvg", "beatsPerMinuteMax")
            )
            if low is None or mean is None or high is None:
                continue
            bucket = merged.setdefault(
                _floor(moment, minutes), {"min": low, "means": [], "max": high}
            )
            bucket["min"] = min(bucket["min"], low)
            bucket["max"] = max(bucket["max"], high)
            bucket["means"].append(mean)
    return [
        {
            "time": _clock(moment),
            "min": _number(b["min"]),
            "avg": round(sum(b["means"]) / len(b["means"]), 1),
            "max": _number(b["max"]),
            "_means": b["means"],
        }
        for moment, b in sorted(merged.items())
    ]


@mcp.tool(structured_output=False)
@require_auth
async def health_get_heart_rate_intraday(
    start: str | None = None,
    end: str | None = None,
    resolution_minutes: Literal[5, 10, 15, 30, 60] = 15,
    live: bool = False,
) -> str:
    """Get heart rate through a night or a day, to see when it was high.

    Min, average and max beats per minute per bucket, from Google's
    five-minute rollup of the heart rate, cached a local day at a time and
    auto-synced if stale. Use it to see when during a night or a day the
    heart rate rose, such as after a late meal or an afternoon workout.

    Args:
        start: "YYYY-MM-DD" (local midnight), "YYYY-MM-DDTHH:MM" (local time),
            or a time with an offset or Z. Default: the longest sleep session
            of the latest night held in the cache, or today if none is.
        end: Same forms, exclusive; a date alone means the end of that day.
            Default: a day after start. At most 7 days after start. Both ends
            are widened to whole buckets, and the reply says where they fell.
        resolution_minutes: Bucket size, aligned to the local clock: 5, 10, 15,
            30 or 60. Default: 15.
        live: If true, re-fetch these days from the API before reading the cache.

    Times are local, to the minute, with their UTC offset, so the hour a clock
    repeats when it goes back reads as two hours. Returns start and end (the
    window as widened), resolution_minutes, buckets (each with time, the start
    of the bucket, min and max bpm, and avg, the mean of its five-minute
    averages), and a summary with the window's min, avg and max and max_at,
    the bucket the highest reading fell in. Buckets with nothing worn are left
    out.
    """
    first, last = await anyio.to_thread.run_sync(
        lambda: _intraday_window(start, end, resolution_minutes)
    )
    first_day = first.astimezone().date()
    last_day = (last - timedelta(microseconds=1)).astimezone().date()

    await anyio.to_thread.run_sync(
        lambda: refresh_before_query("heart_rate_intraday", first_day, last_day, live)
    )

    def _query():
        conn = db.get_db()
        try:
            return db.query_day_lists(
                conn, "heart_rate_intraday", first_day.isoformat(), last_day.isoformat()
            )
        finally:
            conn.close()

    buckets = _rebucket(await anyio.to_thread.run_sync(_query), first, last, resolution_minutes)

    if not buckets:
        return empty_answer("No intraday heart rate found for this period.", live)

    means = [m for b in buckets for m in b.pop("_means")]
    peak = max(buckets, key=lambda b: b["max"])
    return format_response(
        {
            "start": _clock(first),
            "end": _clock(last),
            "resolution_minutes": resolution_minutes,
            "buckets": buckets,
            "summary": {
                "min": min(b["min"] for b in buckets),
                "avg": round(sum(means) / len(means), 1),
                "max": peak["max"],
                "max_at": peak["time"],
            },
        }
    )
