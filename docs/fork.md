# Fork inventory

This fork of [partymola/google-health-mcp](https://github.com/partymola/google-health-mcp)
adds one feature, opt-in write tools. Upstream is read-only on purpose; everything
here is off unless `GOOGLE_HEALTH_MCP_WRITE` is set, and with it off the scope
string, the tool list and every upstream test are unchanged.

## Write tools (kind 2: fork-only, behind a setting off by default)

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
returned). `health_delete_logged` sends `dataPoints:batchDelete` with that id.
Local ISO times without an offset are read in the account's time zone, from
`users/me/settings` (`timeZone`, else `utcOffset`, else the host's). After a write
the day or days written are re-synced through upstream's `run_sync`, so the read
tools see the change at once. Moods and symptoms are write-only types: nothing
reads them back, so nothing is cached for them.

Writes use their own POST in `fork/writes.py` rather than `api.google_get`, because
that one re-sends a request answered 500-503 and a re-sent create logs twice.
Errors follow AGENTS.md: fixed text, a status code and an operation such as
`weight create`, never a body or a resource name.

**Where.**

| File | What |
|---|---|
| `src/google_health_mcp/fork/scopes.py` | scope list, `requested_scopes()`, the doctor check |
| `src/google_health_mcp/fork/writes.py` | POST client, time handling, enums, create/delete, cache refresh |
| `src/google_health_mcp/fork/write_tools.py` | the MCP tools; registers them at import only when on |
| `tests/test_fork_write_setting.py` | setting, consent scopes, doctor finding |
| `tests/test_fork_writes.py` | each type's exact path and body, delete, refusals, error text, scope/writer parity, registration |

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
