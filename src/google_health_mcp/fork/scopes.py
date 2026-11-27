"""Fork: the writeonly scopes the opt-in write tools need (docs/fork.md).

Imports only `config` at module level, so `auth` and `doctor` can hook it in
without a cycle.
"""

from .. import config

# Each writeonly scope requested when GOOGLE_HEALTH_MCP_WRITE is on, and the
# data types written under it. `test_every_write_scope_has_a_writer` holds this
# equal to what `fork.writes` can write, in both directions, as upstream does
# for its readers.
WRITE_SCOPE_WRITERS: dict[str, tuple[str, ...]] = {
    "nutrition": ("nutrition-log", "hydration-log"),
    "health_metrics_and_measurements": ("weight", "body-fat"),
    "activity_and_fitness": ("exercise",),
    "sleep": ("sleep",),
    "mindfulness": ("moods",),
    "logged_symptoms": ("symptoms",),
}
MISSING_WRITE_SCOPES = "missing-write-scopes"


def _url(name: str) -> str:
    return f"{config.GOOGLE_SCOPE_PREFIX}{name}.writeonly"


def requested_scopes() -> str:
    """The consent request's scope string: upstream's, plus the writes when on."""
    if not config.WRITE_MODE:
        return config.GOOGLE_SCOPES
    return " ".join([config.GOOGLE_SCOPES, *(_url(n) for n in WRITE_SCOPE_WRITERS)])


def missing_write_scopes(granted) -> list[str] | None:
    """The write scopes a grant lacks, by short name, or None if unrecorded."""
    if not isinstance(granted, str) or not granted.strip():
        return None
    held = set(granted.split())
    return [n for n in WRITE_SCOPE_WRITERS if _url(n) not in held]


def check_write_authorisation() -> list:
    """Doctor check: with writes on, the write scopes the grant still lacks."""
    from ..doctor import OK, WARN, Finding, _token_scope

    if not config.WRITE_MODE or config.OFFLINE_MODE:
        return []
    missing = missing_write_scopes(_token_scope())
    if missing is None:
        return []
    if not missing:
        return [
            Finding(
                "write authorisation",
                OK,
                "the grant holds every permission the write tools use",
                check=MISSING_WRITE_SCOPES,
            )
        ]
    return [
        Finding(
            "write authorisation",
            WARN,
            f"GOOGLE_HEALTH_MCP_WRITE is on but the grant lacks {', '.join(missing)} "
            "(writeonly), so logging those types is refused.",
            "Add the writeonly scopes on the Cloud console Data Access page, then run "
            "`google-health-mcp auth` and tick every permission.",
            check=MISSING_WRITE_SCOPES,
        )
    ]
