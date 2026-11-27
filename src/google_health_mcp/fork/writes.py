"""Fork: create and delete data points through the Google Health API (docs/fork.md).

Its own request function rather than `api.google_get`, for two reasons: that
one re-sends a request answered 500-503, and a re-sent create can log the same
meal twice; and it labels a request by data type and method, which reads
"list" for a create. Errors follow the same rule: fixed text, a status code and
the operation, never a response body or a resource name.
"""

import http.client
import json
import logging
import re
import sqlite3
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .. import api, config, db
from ..auth import RefreshNetworkError, TokenRefused, refresh_google_token
from ..errors import GoogleHealthError
from ..tools import google_sync

logger = logging.getLogger(__name__)


class WriteInputError(GoogleHealthError):
    """An argument a write cannot send; nothing was sent. Written for the model."""


#: Each type this fork writes, the data point field holding its payload, and
#: the cache types filled from it, refreshed for the day after a write.
WRITERS: dict[str, tuple[str, tuple[str, ...]]] = {
    "nutrition-log": ("nutritionLog", ("food_log", "food_entries")),
    "hydration-log": ("hydrationLog", ("food_log", "water_entries")),
    "weight": ("weight", ("weight", "weight_readings")),
    "body-fat": ("bodyFat", ("weight", "body_fat_readings")),
    "exercise": ("exercise", ("exercises",)),
    "sleep": ("sleep", ("sleep", "sleep_sessions")),
    # Write-only types: nothing reads them back, so nothing caches them.
    "moods": ("moods", ()),
    "symptoms": ("symptoms", ()),
}

MEAL_TYPES = frozenset(
    "BEFORE_BREAKFAST BREAKFAST BEFORE_LUNCH LUNCH BEFORE_DINNER DINNER AFTER_DINNER "
    "SNACK ANYTIME".split()
)
MOODS = frozenset(
    "AMAZED AMUSED ANGRY ANNOYED ANXIOUS HAPPY CONTENT SAD WORRIED FRUSTRATED EXCITED CALM "
    "STRESSED ASHAMED BRAVE CONFIDENT DISAPPOINTED DISCOURAGED DISGUSTED DRAINED EMBARRASSED "
    "GRATEFUL GUILTY HOPEFUL HOPELESS INDIFFERENT IRRITATED JEALOUS JOYFUL LONELY OVERWHELMED "
    "PASSIONATE PEACEFUL PROUD RELIEVED SATISFIED SCARED SURPRISED ENERGIZED FATIGUED "
    "VERY_CALM VERY_STRESSED NEUTRAL AFRAID HURTING BORED BITTER ENVIOUS CONFUSED CURIOUS "
    "AWESTRUCK INSPIRED LONGING ACCOMPLISHED LOVING COMPASSIONATE DEPRESSED GOOD LOW_ENERGY "
    "OBSESSIVE_THOUGHTS PANIC PLAYFUL PLEASED SENSITIVE SLEEPY SWINGS UNHAPPY "
    "VERY_SELF_CRITICAL".split()
)
VALENCES = frozenset("UNPLEASANT BASELINE PLEASANT".split())
SYMPTOMS = frozenset(
    "CRAMPS HEADACHE TENDER_BREASTS ACNE SICK BLOATED HOT_FLASHES PMS COUGH FEVER "
    "DIFFICULTY_BREATHING BACK_PAIN SHAKINESS HUNGER SWEATING ANXIETY THIRST "
    "FREQUENT_URINATION BLURRED_VISION OTHER SEX_DRIVE_HIGH SEX_DRIVE_MEDIUM SEX_DRIVE_LOW "
    "HEART_PALPITATIONS FAINTING CHEST_PAIN FATIGUE CONFUSION DIZZINESS ABDOMINAL_PAIN "
    "BLADDER_LEAKS BLEEDING_GUMS BRAIN_FOG BURNING_MOUTH CONSTIPATION CRAVINGS "
    "DECREASED_APPETITE DIARRHEA DRAWING_PAIN DRY_EYES DRY_HAIR DRY_SKIN EXHAUSTION FEEL_GOOD "
    "FOOD_AVERSIONS HAIR_LOSS HEARTBURN HYPERPIGMENTATION INCREASED_APPETITE INSOMNIA "
    "JOINT_PAIN LEG_CRAMPS NAUSEA NIGHT_SWEATS NORMAL_DIGESTION NORMAL_STOOL SLEEPINESS "
    "SWELLING VOMITING".split()
)
_ENUM = re.compile(r"[A-Z][A-Z0-9_]*")
_POINT_ID = re.compile(r"[A-Za-z0-9_-]+")


def enum(value: str, allowed: frozenset | None, what: str) -> str:
    """An API enum from a name a model or person wrote ("low energy" -> LOW_ENERGY)."""
    name = str(value).strip().upper().replace(" ", "_").replace("-", "_")
    if not _ENUM.fullmatch(name) or (allowed is not None and name not in allowed):
        known = f" One of: {', '.join(sorted(allowed))}." if allowed else ""
        raise WriteInputError(f"Unknown {what} {value!r}.{known}")
    return name


def positive(value, what: str, most: float | None = None) -> float:
    if not config.is_number(value) or value <= 0 or (most is not None and value > most):
        bound = f" and at most {most:g}" if most is not None else ""
        raise WriteInputError(f"{what} must be a number above 0{bound}.")
    return value


def account_zone():
    """The account's time zone from its settings, else its UTC offset, else this host's."""
    settings = api.get_settings()
    name = settings.get("timeZone")
    if isinstance(name, str) and name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    offset = settings.get("utcOffset")
    if isinstance(offset, str) and re.fullmatch(r"-?\d+(\.\d+)?s", offset):
        return timezone(timedelta(seconds=float(offset[:-1])))
    return datetime.now().astimezone().tzinfo


def when(text: str | None, zone) -> datetime:
    """A local ISO datetime as an aware one: naive is read in the account's zone."""
    if text is None:
        return datetime.now(zone)
    try:
        moment = datetime.fromisoformat(str(text).strip())
    except ValueError:
        raise WriteInputError(
            f"Unreadable time {text!r}. Use YYYY-MM-DDTHH:MM in the account's local time."
        ) from None
    return moment if moment.tzinfo else moment.replace(tzinfo=zone)


def _utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _offset(moment: datetime) -> str:
    return f"{int(moment.utcoffset().total_seconds())}s"


def sample_time(moment: datetime) -> dict:
    return {"physicalTime": _utc(moment), "utcOffset": _offset(moment)}


def interval(start: datetime, end: datetime | None = None) -> dict:
    """A session interval; an instant (a drink, a meal) lasts one second, as Google's examples."""
    end = end or start + timedelta(seconds=1)
    if end <= start:
        raise WriteInputError("The end time must be after the start time.")
    return {
        "startTime": _utc(start),
        "startUtcOffset": _offset(start),
        "endTime": _utc(end),
        "endUtcOffset": _offset(end),
    }


def _post(path: str, body: dict, operation: str) -> dict:
    """One POST, never re-sent. No response content reaches any message."""
    if config.OFFLINE_MODE:
        raise api.HealthOfflineError(
            "Offline mode is on (GOOGLE_HEALTH_MCP_OFFLINE); writes are disabled."
        )
    try:
        token = refresh_google_token()
    except TokenRefused as e:
        raise api.HealthAuthError(
            "Could not obtain an access token. Run: google-health-mcp auth"
        ) from e
    except RefreshNetworkError as e:
        raise api.HealthNetworkError("Network error. Check your connection.") from e
    req = urllib.request.Request(
        f"{config.GOOGLE_API_BASE}/{path}",
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise api.HealthRateLimitError(api._reset_seconds(e)) from e
        if e.code in (401, 403):
            raise api._classify_google_api_error(e) from e
        unsure = " It may have been saved; check before logging it again." if e.code >= 500 else ""
        raise api.HealthAPIError(f"API error {e.code} for {operation}.{unsure}") from e
    except (OSError, http.client.HTTPException) as e:
        raise api.HealthNetworkError(
            "Network error. Check your connection; the write may have been saved."
        ) from e
    try:
        answer = json.loads(raw)
    except (ValueError, RecursionError) as e:
        raise api.HealthAPIError(f"Google returned an unreadable response for {operation}.") from e
    if not isinstance(answer, dict) or answer.get("error"):
        raise api.HealthAPIError(f"Google did not complete the {operation}.")
    return answer


def create(data_type: str, payload: dict) -> str | None:
    """Create one data point; its id (never the resource name, which holds the user id)."""
    field, _ = WRITERS[data_type]
    answer = _post(
        f"users/me/dataTypes/{data_type}/dataPoints", {field: payload}, f"{data_type} create"
    )
    name = (answer.get("response") or {}).get("name")
    point = name.rsplit("/", 1)[-1] if isinstance(name, str) else None
    return point if point and _POINT_ID.fullmatch(point) else None


def delete(data_type: str, point: str) -> bool:
    """Delete a point this server logged; whether its days' cache was refreshed.

    Only a point in the ledger: an id a tracker or the app wrote is well formed
    too, and is never this tool's to delete.
    """
    days = _logged_days(data_type, point)
    if days is None:
        raise WriteInputError(
            f"Cannot delete that. data_type is one of: {', '.join(WRITERS)}, and "
            "data_point_id is the id a log tool on this server returned for it."
        )
    _post(
        f"users/me/dataTypes/{data_type}/dataPoints:batchDelete",
        {"names": [f"users/me/dataTypes/{data_type}/dataPoints/{point}"]},
        f"{data_type} batchDelete",
    )
    return _best_effort(_after_delete, data_type, point, *days)


def log(data_type: str, payload: dict, first: date, last: date) -> tuple[str | None, bool]:
    """Create a point, record it as this server's, refresh its days' cache.

    The ledger is proven writable before anything is sent: a point created
    while it cannot be recorded could never be deleted here. After the create
    everything is best-effort, as the point exists whatever happens and its id
    must still reach the caller, or a retry logs it twice.
    """
    try:
        conn = _open()
    except Exception as e:  # noqa: BLE001 - refused before anything is sent
        logger.debug("could not open the ledger: %s", type(e).__name__)
        raise WriteInputError(_UNRECORDABLE) from None
    try:
        try:
            conn.execute("INSERT INTO fork_written_points VALUES ('', '', '', '')")
            conn.rollback()
        except sqlite3.Error as e:
            logger.debug("the ledger is not writable: %s", type(e).__name__)
            raise WriteInputError(_UNRECORDABLE) from None
        point = create(data_type, payload)
        return point, _best_effort_on(conn, _after_create, data_type, point, first, last)
    finally:
        conn.close()


_UNRECORDABLE = (
    "The cache could not record this log as one this server made, so nothing was "
    "logged: a log it cannot record could not be deleted here."
)


#: Which points this server created, so delete can refuse any other, and the
#: local days each covers, so a delete refreshes them. Fork-only, in the cache.
_LEDGER = """CREATE TABLE IF NOT EXISTS fork_written_points (
    data_type TEXT NOT NULL,
    point_id TEXT NOT NULL,
    first_date TEXT NOT NULL,
    last_date TEXT NOT NULL,
    PRIMARY KEY (data_type, point_id)
)"""


def _open():
    conn = db.get_db()
    conn.execute(_LEDGER)
    return conn


def _best_effort(step, *args) -> bool:
    try:
        conn = _open()
    except Exception as e:  # noqa: BLE001 - the write is done; only its cache is at stake
        logger.debug("cache step after a write failed: %s", type(e).__name__)
        return False
    try:
        return _best_effort_on(conn, step, *args)
    finally:
        conn.close()


def _best_effort_on(conn, step, *args) -> bool:
    try:
        return step(conn, *args)
    except Exception as e:  # noqa: BLE001 - the write is done; only its cache is at stake
        logger.debug("cache step after a write failed: %s", type(e).__name__)
        return False


def _after_create(conn, data_type, point, first, last) -> bool:
    if point:
        conn.execute(
            "INSERT OR REPLACE INTO fork_written_points VALUES (?, ?, ?, ?)",
            (data_type, point, first.isoformat(), last.isoformat()),
        )
        conn.commit()
    if not _refresh(conn, data_type, first, last):
        return False
    if data_type == "exercise":
        return _store_workout(conn, point, first, last)
    return True


def _store_workout(conn, point, first, last) -> bool:
    """Store a logged workout even on a day an import recorded.

    Upstream leaves such a day to the import, so a workout both hold is not
    stored twice; one this server just logged is in no import. Its day is
    normalised by upstream's own handler in a scratch cache holding no import,
    and only that workout's row is copied across. A later sync keeps it, as
    Google still holds it, and removes it once deleted.
    """
    query = "SELECT log_id FROM exercises WHERE date BETWEEN ? AND ?"
    if not point or _holds(conn, query, point, first, last):
        return bool(point)
    scratch = db.get_db(":memory:")
    try:
        google_sync.GOOGLE_SYNC_HANDLERS["exercises"](scratch, first, last)
        for name in _names(scratch, query, point, first, last):
            row = dict(
                scratch.execute("SELECT * FROM exercises WHERE log_id = ?", (name,)).fetchone()
            )
            db.save_exercise(conn, row.pop("log_id"), row)
    finally:
        scratch.close()
    conn.commit()
    return _holds(conn, query, point, first, last)


def _after_delete(conn, data_type, point, first, last) -> bool:
    if data_type != "sleep":
        return _refresh(conn, data_type, first, last)
    # The sleep syncs only upsert, so a deleted session's rows stay unless
    # removed here; the re-sync then rebuilds the night from what is left.
    # A session's night is its local end date, as upstream dates it.
    night = last.isoformat()
    query = "SELECT session_id FROM sleep_sessions WHERE date BETWEEN ? AND ?"
    gone = [{"session_id": n} for n in _names(conn, query, point, last, last)]
    db.remove_rows(conn, "sleep_sessions", gone)
    written = conn.execute(
        "SELECT 1 FROM sleep WHERE date = ? AND provider = ?", (night, google_sync.PROVIDER)
    ).fetchone()
    if written:
        # Only the columns Google writes are withdrawn: an upsert leaves the
        # rest, so the night can hold an imported efficiency beside them.
        db.save_sleep(conn, {"date": night, **dict.fromkeys(_GOOGLE_SLEEP_COLUMNS)})
    refreshed = _refresh(conn, data_type, first, last)
    row = conn.execute("SELECT * FROM sleep WHERE date = ?", (night,)).fetchone()
    if written and all(row[k] is None for k in row.keys() if k not in ("date", "provider")):
        # A night left with no value at all is removed, as upstream's sweeps do.
        db.remove_rows(conn, "sleep", [{"date": night}])
        conn.commit()
    return refreshed


#: What upstream's nightly sleep sync writes: its sums, their span and count.
_GOOGLE_SLEEP_COLUMNS = (
    "total_minutes",
    "sleep_period_minutes",
    "deep_minutes",
    "light_minutes",
    "rem_minutes",
    "wake_minutes",
    "start_time",
    "end_time",
    "sessions",
)


def _refresh(conn, data_type, first, last) -> bool:
    """Re-fetch the account-local days through upstream's normalisers.

    The handlers directly rather than `run_sync`, which caps the range at the
    host's today: an account ahead of the host would have its day dropped.
    """
    cached = WRITERS[data_type][1]
    for table in cached:
        google_sync.GOOGLE_SYNC_HANDLERS[table](conn, first, last)
    conn.commit()
    return bool(cached)


def _names(conn, query, point, first, last) -> list[str]:
    suffix = f"/dataPoints/{point}"
    rows = conn.execute(query, (first.isoformat(), last.isoformat())).fetchall()
    return [r[0] for r in rows if isinstance(r[0], str) and r[0].endswith(suffix)]


def _holds(conn, query, point, first, last) -> bool:
    return bool(point) and bool(_names(conn, query, point, first, last))


def _logged_days(data_type: str, point: str) -> tuple[date, date] | None:
    if data_type not in WRITERS or not _POINT_ID.fullmatch(str(point)):
        return None
    try:
        conn = _open()
        try:
            row = conn.execute(
                "SELECT first_date, last_date FROM fork_written_points "
                "WHERE data_type = ? AND point_id = ?",
                (data_type, point),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        raise WriteInputError("Could not read which points this server logged.") from None
    return None if row is None else (date.fromisoformat(row[0]), date.fromisoformat(row[1]))
