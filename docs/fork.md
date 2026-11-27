# Fork inventory

What this fork carries beyond upstream [partymola/google-health-mcp](https://github.com/partymola/google-health-mcp), so a merge from upstream knows what to keep and an upstream PR knows what to offer.

Kinds:

- **Kind 1, upstream-ready**: written in upstream's modules, style, tests and CHANGELOG format, meant to be offered upstream as a PR. Conflicts on merge are resolved in upstream's favour, then the change is re-applied.
- **Kind 2, fork-only**: kept apart under `src/google_health_mcp/fork/`, joined to upstream's code at as few hooks as possible.

## Changes

### Intraday heart rate and active minutes

- **Kind**: 1, upstream candidate. Not yet offered upstream.
- **What**: `health_get_heart_rate_intraday` returns heart rate through a night or a day, min/avg/max per 5 to 60 minutes with the peak's time, so an agent can tell when the pulse was high. `health_get_activity` fills its active-minute columns from Google's `active-minutes` daily rollup.
- **Location**:
  - `src/google_health_mcp/api.py`: `roll_up`, a rollup over buckets of physical time.
  - `src/google_health_mcp/tools/google_sync.py`: `sync_heart_rate_intraday` and its two registry entries; the active-minute columns in `sync_activity`.
  - `src/google_health_mcp/db.py`: table `heart_rate_intraday` (a day-list table), `save_heart_rate_intraday`, `latest_sleep_window`.
  - `src/google_health_mcp/config.py`: `heart_rate_intraday` in `CACHED_DATA_TYPES`; `heart-rate` and `active-minutes` in `GOOGLE_SCOPE_READERS`.
  - `src/google_health_mcp/tools/heart_tools.py`: the tool.
  - Tests: `tests/test_heart_rate_intraday.py`, `tests/test_google_rollup.py`, `tests/test_google_activity.py`, and one-line additions to the registry tests.
  - Docs: README tools table and reference, CHANGELOG under Unreleased, `docs/agents/schema.md`, `docs/agents/sync.md`.
- **Hooks**: none beyond upstream's own registries (`GOOGLE_SYNC_HANDLERS`, `HANDLER_SCOPES`, `_DAY_LIST_TABLES`, `_UPSERT_KEYS`, `_DATA_TABLE_MAP`). No new OAuth scope.
- **Not done**: intraday HRV and SpO2. Google has no `rollUp` for either, so they do not fit the same shape.
