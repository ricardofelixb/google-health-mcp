"""Fork: MCP tools that log to the account, registered only with GOOGLE_HEALTH_MCP_WRITE on.

Each returns the created data point's id, which `health_delete_logged` takes
to undo a wrong log. Times are local ISO datetimes ("2026-03-10T22:00") read
in the account's time zone unless they carry an offset; omitted means now.
"""

from datetime import date

import anyio

from .. import config
from ..helpers import format_response, require_auth
from ..mcp_instance import mcp
from . import writes


async def _log(data_type: str, build) -> str:
    """Build in the account's zone, create, then refresh the cache for those days."""

    def _run():
        payload, first, last = build(writes.account_zone())
        point = writes.create(data_type, payload)
        refreshed = writes.refresh_cache(data_type, first.date(), last.date())
        return {
            "logged": data_type,
            "data_point_id": point,
            "local_time": first.isoformat(),
            "cache_refreshed": refreshed,
        }

    return format_response(await anyio.to_thread.run_sync(_run))


@require_auth
async def health_log_food(
    food: str,
    time: str | None = None,
    meal_type: str | None = None,
    calories: float | None = None,
    protein_g: float | None = None,
    carbs_g: float | None = None,
    fat_g: float | None = None,
) -> str:
    """Log something eaten, as free text ("banana pie"), with optional estimates.

    Args:
        food: what was eaten, as the person said it.
        time: local ISO datetime it was eaten. Default: now.
        meal_type: BREAKFAST, LUNCH, DINNER, SNACK, BEFORE_BREAKFAST, BEFORE_LUNCH,
            BEFORE_DINNER, AFTER_DINNER or ANYTIME.
        calories, protein_g, carbs_g, fat_g: optional estimates in kcal and grams.
    """
    if not isinstance(food, str) or not food.strip():
        raise writes.WriteInputError("food must name what was eaten.")

    def build(zone):
        at = writes.when(time, zone)
        body = {"interval": writes.interval(at), "foodDisplayName": food.strip()}
        if meal_type is not None:
            body["mealType"] = writes.enum(meal_type, writes.MEAL_TYPES, "meal type")
        for key, value, what in (
            ("energy", calories, "calories"),
            ("totalCarbohydrate", carbs_g, "carbs_g"),
            ("totalFat", fat_g, "fat_g"),
        ):
            if value is not None:
                unit = "kcal" if key == "energy" else "grams"
                body[key] = {unit: writes.positive(value, what)}
        if protein_g is not None:
            grams = writes.positive(protein_g, "protein_g")
            body["nutrients"] = [{"nutrient": "PROTEIN", "quantity": {"grams": grams}}]
        return body, at, at

    return await _log("nutrition-log", build)


@require_auth
async def health_log_water(milliliters: float, time: str | None = None) -> str:
    """Log a drink of water or other liquid, in millilitres (a glass is about 250).

    Args:
        milliliters: amount drunk.
        time: local ISO datetime. Default: now.
    """
    amount = writes.positive(milliliters, "milliliters", 10000)

    def build(zone):
        at = writes.when(time, zone)
        return {"interval": writes.interval(at), "amountConsumed": {"milliliters": amount}}, at, at

    return await _log("hydration-log", build)


def _sample(field: str, value):
    def build(zone, time):
        at = writes.when(time, zone)
        return {"sampleTime": writes.sample_time(at), field: value}, at, at

    return build


@require_auth
async def health_log_weight(kilograms: float, time: str | None = None) -> str:
    """Log a body weight in kilograms.

    Args:
        kilograms: the weight.
        time: local ISO datetime it was measured. Default: now.
    """
    grams = writes.positive(kilograms, "kilograms", 1000) * 1000
    return await _log("weight", lambda zone: _sample("weightGrams", grams)(zone, time))


@require_auth
async def health_log_body_fat(percentage: float, time: str | None = None) -> str:
    """Log a body fat percentage.

    Args:
        percentage: body fat, 0-100.
        time: local ISO datetime it was measured. Default: now.
    """
    value = writes.positive(percentage, "percentage", 100)
    return await _log("body-fat", lambda zone: _sample("percentage", value)(zone, time))


@require_auth
async def health_log_exercise(
    exercise_type: str,
    start: str,
    end: str,
    calories: float | None = None,
    distance_km: float | None = None,
    name: str | None = None,
) -> str:
    """Log a workout done without a tracker.

    Args:
        exercise_type: Google's type, e.g. RUNNING, WALKING, BIKING, SWIMMING, WEIGHTS,
            YOGA, HIKING, HIIT, PILATES, OTHER.
        start, end: local ISO datetimes.
        calories: optional kcal burned.
        distance_km: optional distance.
        name: display name; Google keeps its own for every type but OTHER.
    """
    kind = writes.enum(exercise_type, None, "exercise type")
    summary = {}
    if calories is not None:
        summary["caloriesKcal"] = writes.positive(calories, "calories")
    if distance_km is not None:
        summary["distanceMillimeters"] = writes.positive(distance_km, "distance_km") * 1e6

    def build(zone):
        first, last = writes.when(start, zone), writes.when(end, zone)
        body = {
            "interval": writes.interval(first, last),
            "exerciseType": kind,
            "displayName": (name or "").strip() or kind.replace("_", " ").capitalize(),
            "metricsSummary": summary,
        }
        return body, first, last

    return await _log("exercise", build)


@require_auth
async def health_log_sleep(start: str, end: str) -> str:
    """Log a sleep session (bedtime to wake-up) a tracker did not record.

    Args:
        start, end: local ISO datetimes, e.g. "2026-03-10T23:00" and "2026-03-11T07:00".
    """

    def build(zone):
        first, last = writes.when(start, zone), writes.when(end, zone)
        return {"interval": writes.interval(first, last)}, first, last

    return await _log("sleep", build)


@require_auth
async def health_log_mood(
    moods: list[str], valence: str | None = None, time: str | None = None
) -> str:
    """Log how the person feels: "I feel tired" -> FATIGUED or LOW_ENERGY.

    Args:
        moods: one or more of Google's moods, e.g. HAPPY, CALM, CONTENT, STRESSED, ANXIOUS,
            SAD, FATIGUED, LOW_ENERGY, SLEEPY, ENERGIZED, IRRITATED, OVERWHELMED, NEUTRAL.
            An unknown name is refused with the full list.
        valence: optional PLEASANT, BASELINE or UNPLEASANT.
        time: local ISO datetime. Default: now.
    """
    names = [writes.enum(m, writes.MOODS, "mood") for m in moods or ()]
    if not names:
        raise writes.WriteInputError("moods must name at least one mood.")
    tone = None if valence is None else writes.enum(valence, writes.VALENCES, "valence")

    def build(zone):
        at = writes.when(time, zone)
        body = {"sampleTime": writes.sample_time(at), "moods": names}
        if tone:
            body["valences"] = [tone]
        return body, at, at

    return await _log("moods", build)


@require_auth
async def health_log_symptoms(symptoms: list[str], time: str | None = None) -> str:
    """Log symptoms, e.g. FATIGUE, HEADACHE, NAUSEA, BLOATED, HEARTBURN, INSOMNIA, COUGH.

    Args:
        symptoms: one or more of Google's symptoms; an unknown name is refused with the list.
        time: local ISO datetime. Default: now.
    """
    names = [writes.enum(s, writes.SYMPTOMS, "symptom") for s in symptoms or ()]
    if not names:
        raise writes.WriteInputError("symptoms must name at least one symptom.")

    def build(zone):
        at = writes.when(time, zone)
        return {"sampleTime": writes.sample_time(at), "symptoms": names}, at, at

    return await _log("symptoms", build)


@require_auth
async def health_delete_logged(data_type: str, data_point_id: str, date: str | None = None) -> str:
    """Delete a data point a health_log_* tool created, to fix a wrong log.

    Args:
        data_type: the `logged` value the log tool returned, e.g. "weight".
        data_point_id: the `data_point_id` it returned.
        date: optional local "YYYY-MM-DD" it was logged for, to refresh the cache that day.
    """
    day = _day(date)

    def _run():
        writes.delete(data_type, data_point_id)
        refreshed = day is not None and writes.refresh_cache(data_type, day, day)
        return {"deleted": data_point_id, "data_type": data_type, "cache_refreshed": refreshed}

    return format_response(await anyio.to_thread.run_sync(_run))


def _day(text: str | None) -> date | None:
    if text is None:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise writes.WriteInputError(f"Unreadable date {text!r}. Use YYYY-MM-DD.") from None


WRITE_TOOLS = (
    health_log_food,
    health_log_water,
    health_log_weight,
    health_log_body_fat,
    health_log_exercise,
    health_log_sleep,
    health_log_mood,
    health_log_symptoms,
    health_delete_logged,
)


def register(server=mcp) -> None:
    for tool in WRITE_TOOLS:
        server.tool(structured_output=False)(tool)


if config.WRITE_MODE:
    register()
