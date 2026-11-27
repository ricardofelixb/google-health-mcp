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

### Write tools

- **Kind**: 2, fork-only, behind a setting off by default.

**What.** With `GOOGLE_HEALTH_MCP_WRITE=1` (any value `GOOGLE_HEALTH_MCP_OFFLINE`
accepts as true), the server:

- asks for six `.writeonly` scopes in the consent request, on top of upstream's
  `.readonly` ones: `nutrition`, `health_metrics_and_measurements`,
  `activity_and_fitness`, `sleep`, `mindfulness`, `logged_symptoms`;
- registers nine tools: `health_log_food`, `health_log_water`, `health_log_weight`,
  `health_log_body_fat`, `health_log_exercise`, `health_log_sleep`, `health_log_mood`,
  `health_log_symptoms` and `health_delete_logged`;
- has `doctor` warn (check `missing-write-scopes`) when the grant lacks any of
  those scopes, so the user knows to re-run `auth`.

Each create is `POST users/me/dataTypes/<type>/dataPoints` with the body Google's
DataPoint reference documents for that type, and returns the new point's id (the
last segment of the resource name; the full name carries the user id and is never
returned). Each created id is recorded, with the local days it covers, in a
fork-only `fork_written_points` table in the cache database.
`health_delete_logged` refuses any id not recorded there, since a point a tracker
or the app wrote has a well-formed id too, and sends `dataPoints:batchDelete` for
one that is. So a point logged before the cache was deleted can no longer be
deleted from here, only in the app. A log tool proves the table writable before it
sends anything, and refuses when it is not, since a point it could not record
could never be deleted here; the id is recorded on the same connection. If that
record still fails once the point exists, the log returns its id with
`deletable_here: false` and a note to delete it in the Fitbit app. Every log
reports `deletable_here`.

Local ISO times without an offset are read in the account's time zone, from
`users/me/settings` (`timeZone`, else `utcOffset`, else the host's). After a create
or delete the account-local days it covers are re-fetched through upstream's
normalisers (`GOOGLE_SYNC_HANDLERS`) on one connection, not through `run_sync`,
which caps the range at the host's today and would drop the day of an account
ahead of the host. That refresh is best-effort: a failure there reports
`cache_refreshed: false` and never hides the id of a point already created.
The hand-logged syncs remove what Google no longer holds, but the sleep syncs
only upsert, so deleting a sleep session also removes its `sleep_sessions` row
through `db.remove_rows`. On the nightly `sleep` row a Google upsert leaves the
columns it does not write that time, and `provider` names only the last writer, so
before the delete the night is built from Google's answer by `sync_sleep` in an
in-memory scratch cache, and only the columns holding exactly that value are
withdrawn: an imported `efficiency` or `deep_minutes` beside them stays. The
re-fetch then rebuilds the sums from the sessions left, and a night left with no
value is removed. Upstream leaves a day an import recorded to the import, so a workout
both hold is not stored twice. A workout this server just logged is in no import,
so when the re-fetch skipped it, its day is normalised again by upstream's
`sync_exercises` in an in-memory scratch cache holding no import and only that
workout's row is copied across. A later sync keeps it, since Google holds it, and
removes it once deleted; the day's other Google workouts stay the import's.
Moods and symptoms are write-only types: nothing reads them back, so nothing is cached for them.

Writes use their own POST in `fork/writes.py` rather than `api.google_get`, because
that one re-sends a request answered 500-503 and a re-sent create logs twice.
Errors follow AGENTS.md: fixed text, a status code and an operation such as
`weight create`, never a body or a resource name.
Google documents every write answer as a finished operation (`done: true` with a
`response`). A pending operation, or any other shape after a 2xx, is reported as
an error saying the write may still exist, never as `logged` or `deleted`.
There is no endpoint to poll an operation, so the caller is told to check first.

**Where.**

| File | What |
|---|---|
| `src/google_health_mcp/fork/scopes.py` | scope list, `requested_scopes()`, the doctor check |
| `src/google_health_mcp/fork/writes.py` | POST client, time handling, enums, create/delete, ledger of created ids, cache refresh |
| `src/google_health_mcp/fork/write_tools.py` | the MCP tools; registers them at import only when on |
| `tests/test_fork_write_setting.py` | setting, consent scopes, doctor finding |
| `tests/test_fork_writes.py` | each type's exact path and body, delete and its ownership check, cache refresh, refusals, error text, scope/writer parity, registration |

**Hook lines in upstream files** (each marked `# fork` or `Fork`):

- `src/google_health_mcp/config.py`: `write_mode_from()` and `WRITE_MODE`, below `OFFLINE_MODE`.
- `src/google_health_mcp/auth.py`: imports `requested_scopes` and uses it for the
  consent request's `scope` instead of `config.GOOGLE_SCOPES`.
- `src/google_health_mcp/doctor.py`: imports `check_write_authorisation` and lists
  it in `run_checks` after `check_authorisation`.
- `src/google_health_mcp/cli.py`: `from .fork import write_tools`, which registers
  the tools when on.
- `tests/conftest.py`: `_isolate_from_ambient_env` clears `GOOGLE_HEALTH_MCP_WRITE`
  and sets `config.WRITE_MODE = False`.
- `AGENTS.md`: the `fork/*.py` row in the routing table, which `test_seams` requires.
- `README.md`: the "Fork: write tools" section.

On an upstream merge, conflicts land only on those lines. `config.GOOGLE_SCOPES`
itself is untouched, so upstream's scope/reader invariant test still holds.

**Quick checks.**

```bash
.venv/bin/python -m pytest tests/test_fork_*.py
.venv/bin/python -m pytest tests/test_config.py tests/test_auth.py tests/test_google_auth.py \
  tests/test_doctor.py tests/test_authorisation.py tests/test_cli.py tests/test_seams.py \
  tests/test_tools.py tests/test_google_api.py
```

**Upstream candidate.** No: upstream is read-only by design (`config.py`: "Never a
writeonly scope").

## Known flaky tests

None known.
