"""Fork: create and delete data points through the Google Health API (docs/fork.md).

Its own request function rather than `api.google_get`, for two reasons: that
one re-sends a request answered 500-503, and a re-sent create can log the same
meal twice; and it labels a request by data type and method, which reads
"list" for a create. Errors follow the same rule: fixed text, a status code and
the operation, never a response body or a resource name.
"""

import http.client
import json
import re
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .. import api, config
from ..auth import RefreshNetworkError, TokenRefused, refresh_google_token
from ..errors import GoogleHealthError
from ..tools import sync_tools


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


def delete(data_type: str, point: str) -> None:
    if data_type not in WRITERS or not _POINT_ID.fullmatch(str(point)):
        raise WriteInputError(
            f"Cannot delete that. data_type is one of: {', '.join(WRITERS)}; "
            "data_point_id is the id a log tool returned."
        )
    _post(
        f"users/me/dataTypes/{data_type}/dataPoints:batchDelete",
        {"names": [f"users/me/dataTypes/{data_type}/dataPoints/{point}"]},
        f"{data_type} batchDelete",
    )


def refresh_cache(data_type: str, first: date, last: date) -> bool:
    """Re-sync the written days through the upstream sync, so the read tools see it."""
    cached = list(WRITERS[data_type][1])
    if not cached:
        return False
    try:
        results = sync_tools.run_sync(cached, since=first.isoformat(), until=last.isoformat())
    except GoogleHealthError:
        return False
    return all(r.get("status") == "ok" for r in results.values())
