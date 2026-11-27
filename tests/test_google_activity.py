"""Activity and active-zone minutes: one row a day, assembled from five sources.

The daily measurements each arrive whole. A day of activity does not: steps,
distance, floors and total calories are four separate rollups, the minute
columns come from a sixth-of-a-day's worth of listed intervals, and the whole
lot has to land in one row without a missing source blanking the others.
"""

from datetime import date
from unittest.mock import patch

import pytest

from google_health_mcp import db
from google_health_mcp.tools import google_sync


def _rollup_day(field, value, day=15):
    return {
        "civilStartTime": {"date": {"year": 2026, "month": 3, "day": day}, "time": {}},
        field: value,
    }


@pytest.fixture
def sync_activity(tmp_db):
    def run(rollups=None, levels=None):
        rollups = rollups or {}
        with (
            patch.object(
                google_sync.api, "daily_roll_up", side_effect=lambda t, s, e: rollups.get(t, [])
            ),
            patch.object(google_sync.api, "list_google_data_points", return_value=levels or []),
        ):
            count = google_sync.sync_activity(tmp_db, date(2026, 3, 1), date(2026, 4, 1))
        tmp_db.commit()
        return count

    return run


class TestTheSourcesMerge:
    def test_four_rollups_land_in_one_row(self, tmp_db, sync_activity):
        sync_activity(
            rollups={
                "steps": [_rollup_day("steps", {"countSum": "8000"})],
                "distance": [_rollup_day("distance", {"millimetersSum": "6500000"})],
                "floors": [_rollup_day("floors", {"countSum": "12"})],
                "total-calories": [_rollup_day("totalCalories", {"kcalSum": 2508.9})],
            }
        )
        rows = db.query_activity(tmp_db, "2026-03-15", "2026-03-15")
        assert len(rows) == 1
        assert rows[0]["steps"] == 8000
        assert rows[0]["floors"] == 12
        # As Google sent it: the column is INTEGER, and SQLite keeps a REAL
        # there rather than truncating it.
        assert rows[0]["calories_out"] == 2508.9

    def test_distance_converts_from_millimetres(self, tmp_db, sync_activity):
        """The API sends millimetres; the column has always held kilometres."""
        sync_activity(
            rollups={"distance": [_rollup_day("distance", {"millimetersSum": "6500000"})]}
        )
        assert db.query_activity(tmp_db, "2026-03-15", "2026-03-15")[0]["distance_km"] == 6.5

    def test_a_day_missing_from_one_source_keeps_the_others(self, tmp_db, sync_activity):
        """Sources are independent, so absence in one must not blank the row."""
        sync_activity(
            rollups={
                "steps": [_rollup_day("steps", {"countSum": "8000"})],
                "floors": [],
            }
        )
        row = db.query_activity(tmp_db, "2026-03-15", "2026-03-15")[0]
        assert row["steps"] == 8000
        assert row["floors"] is None

    def test_calories_never_come_from_adding_active_and_basal(self, tmp_db, sync_activity):
        """basal-energy-burned can return nothing at all for an account.

        Summing active and basal would produce a day's figure missing the part
        that is most of it, and look plausible. total-calories is the source.
        """
        sync_activity(
            rollups={
                "total-calories": [_rollup_day("totalCalories", {"kcalSum": 2508.9})],
                "active-energy-burned": [_rollup_day("activeEnergyBurned", {"kcalSum": 400.0})],
            }
        )
        assert db.query_activity(tmp_db, "2026-03-15", "2026-03-15")[0]["calories_out"] == 2508.9


class TestActiveZoneMinutes:
    def test_the_three_zones_map_to_their_columns(self, tmp_db):
        rollups = {
            "active-zone-minutes": [
                _rollup_day(
                    "activeZoneMinutes",
                    {
                        "sumInFatBurnHeartZone": "20",
                        "sumInCardioHeartZone": "8",
                        "sumInPeakHeartZone": "2",
                    },
                )
            ]
        }
        with patch.object(
            google_sync.api, "daily_roll_up", side_effect=lambda t, s, e: rollups.get(t, [])
        ):
            google_sync.sync_azm(tmp_db, date(2026, 3, 1), date(2026, 4, 1))
        tmp_db.commit()
        row = db.query_azm(tmp_db, "2026-03-15", "2026-03-15")[0]
        assert row["fat_burn_minutes"] == 20
        assert row["cardio_minutes"] == 8
        assert row["peak_minutes"] == 2
        assert row["total_minutes"] == 30

    def test_a_zone_the_response_omits_is_absent_not_zero(self, tmp_db):
        rollups = {
            "active-zone-minutes": [
                _rollup_day("activeZoneMinutes", {"sumInFatBurnHeartZone": "20"})
            ]
        }
        with patch.object(
            google_sync.api, "daily_roll_up", side_effect=lambda t, s, e: rollups.get(t, [])
        ):
            google_sync.sync_azm(tmp_db, date(2026, 3, 1), date(2026, 4, 1))
        tmp_db.commit()
        row = db.query_azm(tmp_db, "2026-03-15", "2026-03-15")[0]
        assert row["peak_minutes"] is None
        assert row["total_minutes"] == 20

    def test_a_point_with_no_zone_at_all_writes_nothing(self, tmp_db):
        """A row of date and provider alone adds no measurement and costs one.

        `provider` names the last writer, so writing it here would take the
        day away from whichever writer actually put a value in it.
        """
        db.save_azm(tmp_db, {"date": "2026-03-15", "total_minutes": 44})
        tmp_db.commit()
        rollups = {"active-zone-minutes": [_rollup_day("activeZoneMinutes", {})]}
        with patch.object(
            google_sync.api, "daily_roll_up", side_effect=lambda t, s, e: rollups.get(t, [])
        ):
            count = google_sync.sync_azm(tmp_db, date(2026, 3, 1), date(2026, 4, 1))
        tmp_db.commit()
        assert count == 0
        row = db.query_azm(tmp_db, "2026-03-15", "2026-03-15")[0]
        assert row["total_minutes"] == 44
        assert row["provider"] is None


class TestTheThreeStatesOfARollup:
    def test_a_day_with_no_point_is_unknown(self, tmp_db, sync_activity):
        sync_activity(rollups={"steps": []})
        assert db.query_activity(tmp_db, "2026-03-01", "2026-04-01") == []

    def test_a_point_with_no_value_is_a_measured_zero(self, tmp_db, sync_activity):
        """Only where zero is a real reading - a day can have no steps."""
        sync_activity(rollups={"steps": [_rollup_day("steps", {})]})
        assert db.query_activity(tmp_db, "2026-03-15", "2026-03-15")[0]["steps"] == 0

    def test_a_stored_value_is_corrected_to_zero_rather_than_left(self, tmp_db, sync_activity):
        db.save_activity(tmp_db, {"date": "2026-03-15", "steps": 8000})
        tmp_db.commit()
        sync_activity(rollups={"steps": [_rollup_day("steps", {})]})
        assert db.query_activity(tmp_db, "2026-03-15", "2026-03-15")[0]["steps"] == 0


def _minutes(*levels, day=15):
    return _rollup_day(
        "activeMinutes",
        {
            "activeMinutesRollupByActivityLevel": [
                {"activityLevel": level, "activeMinutesSum": str(minutes)}
                for level, minutes in levels
            ]
        },
        day=day,
    )


class TestTheMinuteColumns:
    """Google's daily `active-minutes` rollup fills the three activity levels.

    `activity-level` cannot: its raw list returns more minute-long segments
    than the day has minutes, because it is the per-source view and the
    sources overlap, and it has no daily rollup. `active-minutes` does, so the
    reconciliation is Google's own.
    """

    def test_each_level_lands_in_its_column(self, tmp_db, sync_activity):
        sync_activity(
            rollups={
                "active-minutes": [_minutes(("LIGHT", 190), ("MODERATE", 25), ("VIGOROUS", 12))]
            }
        )
        row = db.query_activity(tmp_db, "2026-03-15", "2026-03-15")[0]
        assert row["lightly_active_minutes"] == 190
        assert row["fairly_active_minutes"] == 25
        assert row["very_active_minutes"] == 12
        # Moderate and vigorous, as the column has always been defined.
        assert row["active_minutes"] == 37

    def test_a_level_the_response_omits_is_absent_not_zero(self, tmp_db, sync_activity):
        sync_activity(rollups={"active-minutes": [_minutes(("VIGOROUS", 12))]})
        row = db.query_activity(tmp_db, "2026-03-15", "2026-03-15")[0]
        assert row["very_active_minutes"] == 12
        assert row["fairly_active_minutes"] is None
        assert row["lightly_active_minutes"] is None
        # The total needs both of its parts; one alone would understate it.
        assert row["active_minutes"] is None

    def test_a_partial_point_withdraws_the_stored_total(self, tmp_db, sync_activity):
        """Vigorous alone changes one part, so the stored total no longer adds up."""
        db.save_activity(
            tmp_db,
            {
                "date": "2026-03-15",
                "fairly_active_minutes": 25,
                "very_active_minutes": 15,
                "active_minutes": 40,
            },
        )
        tmp_db.commit()
        sync_activity(rollups={"active-minutes": [_minutes(("VIGOROUS", 12))]})
        row = db.query_activity(tmp_db, "2026-03-15", "2026-03-15")[0]
        assert row["fairly_active_minutes"] == 25
        assert row["very_active_minutes"] == 12
        assert row["active_minutes"] is None

    def test_light_alone_leaves_a_stored_total_alone(self, tmp_db, sync_activity):
        """Neither of the total's parts changed, so the stored total still holds."""
        db.save_activity(
            tmp_db,
            {
                "date": "2026-03-15",
                "fairly_active_minutes": 25,
                "very_active_minutes": 15,
                "active_minutes": 40,
            },
        )
        tmp_db.commit()
        sync_activity(rollups={"active-minutes": [_minutes(("LIGHT", 190))]})
        row = db.query_activity(tmp_db, "2026-03-15", "2026-03-15")[0]
        assert row["lightly_active_minutes"] == 190
        assert row["active_minutes"] == 40

    def test_light_alone_names_no_active_total(self, tmp_db, sync_activity):
        """`active_minutes` excludes light activity, so light alone says nothing of it."""
        sync_activity(rollups={"active-minutes": [_minutes(("LIGHT", 190))]})
        row = db.query_activity(tmp_db, "2026-03-15", "2026-03-15")[0]
        assert row["lightly_active_minutes"] == 190
        assert row["active_minutes"] is None

    def test_a_point_with_no_level_writes_no_row(self, tmp_db, sync_activity):
        sync_activity(rollups={"active-minutes": [_rollup_day("activeMinutes", {})]})
        assert db.query_activity(tmp_db, "2026-03-01", "2026-04-01") == []

    def test_sedentary_is_left_null(self, tmp_db, sync_activity):
        """Nothing Google sends partitions a day, so it is not derived as the rest of one."""
        sync_activity(rollups={"active-minutes": [_minutes(("LIGHT", 190), ("MODERATE", 25))]})
        assert db.query_activity(tmp_db, "2026-03-15", "2026-03-15")[0]["sedentary_minutes"] is None

    def test_a_google_sync_without_minutes_leaves_them_null(self, tmp_db, sync_activity):
        sync_activity(rollups={"steps": [_rollup_day("steps", {"countSum": "8000"})]})
        row = db.query_activity(tmp_db, "2026-03-15", "2026-03-15")[0]
        assert row["steps"] == 8000
        for column in (
            "very_active_minutes",
            "fairly_active_minutes",
            "lightly_active_minutes",
            "sedentary_minutes",
            "active_minutes",
        ):
            assert row[column] is None, column

    def test_an_imported_sedentary_figure_is_kept(self, tmp_db, sync_activity):
        """Leaving the column unwritten is what preserves an imported history of it."""
        db.save_activity(
            tmp_db,
            {"date": "2026-03-15", "very_active_minutes": 45, "sedentary_minutes": 761},
        )
        tmp_db.commit()
        sync_activity(rollups={"active-minutes": [_minutes(("VIGOROUS", 12))]})
        row = db.query_activity(tmp_db, "2026-03-15", "2026-03-15")[0]
        assert row["very_active_minutes"] == 12
        assert row["sedentary_minutes"] == 761

    def test_nothing_fetches_activity_level(self, tmp_db):
        """Not fetched at all, so a sync does not pay for data it discards."""
        asked = []
        with (
            patch.object(
                google_sync.api, "daily_roll_up", side_effect=lambda t, s, e: asked.append(t) or []
            ),
            patch.object(
                google_sync.api,
                "list_google_data_points",
                side_effect=lambda t, s, e: asked.append(t) or [],
            ),
        ):
            google_sync.sync_activity(tmp_db, date(2026, 3, 1), date(2026, 3, 2))
        assert "activity-level" not in asked
