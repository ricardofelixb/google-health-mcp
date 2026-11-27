"""Fork: the opt-in write setting, its scopes and the doctor finding (docs/fork.md)."""

import json
import subprocess
import sys
from urllib.parse import parse_qs, urlparse

import pytest

from google_health_mcp import auth, config, doctor
from google_health_mcp.fork import scopes

_PREFIX = "https://www.googleapis.com/auth/googlehealth."


def _requested(monkeypatch, on: bool) -> list[str]:
    monkeypatch.setattr(config, "WRITE_MODE", on)
    url = auth._google_auth_url("fictional-challenge", "fictional-client")
    return parse_qs(urlparse(url).query)["scope"][0].split()


class TestTheSetting:
    @pytest.mark.parametrize("value", ["1", "true", "YES", " on "])
    def test_a_truthy_value_turns_it_on(self, value):
        assert config.write_mode_from(value) is True

    @pytest.mark.parametrize("value", [None, "", "0", "off", "no", "nope"])
    def test_anything_else_leaves_it_off(self, value):
        assert config.write_mode_from(value) is False

    def test_off_by_default_in_a_fresh_interpreter(self):
        probe = "from google_health_mcp import config; print(config.WRITE_MODE)"
        env = {k: v for k, v in __import__("os").environ.items() if "GOOGLE_HEALTH" not in k}
        out = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True, env=env, timeout=60
        )
        assert out.stdout.strip() == "False"


class TestTheConsentRequest:
    def test_off_asks_for_exactly_the_upstream_scopes(self, monkeypatch):
        assert _requested(monkeypatch, False) == config.GOOGLE_SCOPES.split()

    def test_on_adds_each_writeonly_scope_once(self, monkeypatch):
        requested = _requested(monkeypatch, True)
        added = [s for s in requested if s not in config.GOOGLE_SCOPES.split()]
        assert requested[: len(config.GOOGLE_SCOPES.split())] == config.GOOGLE_SCOPES.split()
        assert sorted(added) == sorted(
            f"{_PREFIX}{n}.writeonly"
            for n in (
                "nutrition",
                "health_metrics_and_measurements",
                "activity_and_fitness",
                "sleep",
                "mindfulness",
                "logged_symptoms",
            )
        )

    def test_the_upstream_scope_constant_stays_read_only(self, monkeypatch):
        monkeypatch.setattr(config, "WRITE_MODE", True)
        assert all(s.endswith(".readonly") for s in config.GOOGLE_SCOPES.split())


class TestTheWriteGrant:
    def test_unknown_when_nothing_is_recorded(self):
        assert scopes.missing_write_scopes(None) is None
        assert scopes.missing_write_scopes("  ") is None

    def test_names_what_the_grant_lacks(self):
        granted = f"{_PREFIX}nutrition.writeonly {_PREFIX}sleep.readonly"
        missing = scopes.missing_write_scopes(granted)
        assert "nutrition" not in missing
        assert "sleep" in missing and "logged_symptoms" in missing


class TestTheDoctorFinding:
    @pytest.fixture
    def tokens(self, tmp_path, monkeypatch):
        path = tmp_path / "google_tokens.json"
        monkeypatch.setattr(config, "GOOGLE_TOKENS_PATH", path)
        monkeypatch.setattr(config, "OFFLINE_MODE", False)

        def write(scope):
            path.write_text(json.dumps({"refresh_token": "fictional", "scope": scope}))

        return write

    def test_absent_when_the_setting_is_off(self, tokens, monkeypatch):
        monkeypatch.setattr(config, "WRITE_MODE", False)
        tokens("")
        assert scopes.check_write_authorisation() == []
        assert not [f for f in doctor.run_checks() if f.check == "missing-write-scopes"]

    def test_doctor_runs_it(self, tokens, monkeypatch):
        monkeypatch.setattr(config, "WRITE_MODE", True)
        tokens(config.GOOGLE_SCOPES)
        assert [f for f in doctor.run_checks() if f.check == "missing-write-scopes"]

    def test_warns_and_sends_the_user_to_auth_when_a_write_scope_is_missing(
        self, tokens, monkeypatch
    ):
        monkeypatch.setattr(config, "WRITE_MODE", True)
        tokens(config.GOOGLE_SCOPES)
        (finding,) = scopes.check_write_authorisation()
        assert finding.severity == doctor.WARN
        assert finding.check == "missing-write-scopes"
        assert "logged_symptoms" in finding.detail
        assert "google-health-mcp auth" in finding.fix

    def test_ok_when_every_write_scope_is_held(self, tokens, monkeypatch):
        monkeypatch.setattr(config, "WRITE_MODE", True)
        tokens(scopes.requested_scopes())
        (finding,) = scopes.check_write_authorisation()
        assert finding.severity == doctor.OK
