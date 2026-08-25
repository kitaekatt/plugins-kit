"""Tests for llm_scripting_kit.account.check_account.

The transport boundary is mocked at ``urllib.request.urlopen`` so we never
make real network calls. The test focuses on response interpretation:
mapping HTTP status codes to AccountStatus.failure_reason values, and
extracting fields from the ``data`` envelope OpenRouter returns.
"""

import io
import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from llm_scripting_kit.account import AccountCheckError, check_account


def _ok_response(payload):
    """Build an urlopen() context-manager mock that yields the given JSON."""
    body = json.dumps(payload).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.read.return_value = body
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.__exit__.return_value = None
    return mock_resp


class TestCheckAccount:
    def test_empty_key_raises(self):
        with pytest.raises(AccountCheckError, match="empty"):
            check_account("")

    def test_ok_extracts_data_fields(self):
        payload = {
            "data": {
                "label": "test-key (laptop)",
                "limit": None,
                "usage": 1.234,
                "is_free_tier": False,
                "rate_limit": {"requests": 200, "interval": "10s"},
            }
        }
        with patch("urllib.request.urlopen", return_value=_ok_response(payload)):
            status = check_account("sk-or-v1-test")
        assert status.ok is True
        assert status.label == "test-key (laptop)"
        assert status.usage == 1.234
        assert status.limit is None
        assert status.is_free_tier is False
        assert status.rate_limit == {"requests": 200, "interval": "10s"}
        assert status.failure_reason is None

    def test_401_returns_auth_failure(self):
        err = urllib.error.HTTPError(
            url="https://openrouter.ai/api/v1/auth/key",
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=io.BytesIO(b""),
        )
        with patch("urllib.request.urlopen", side_effect=err):
            status = check_account("sk-or-v1-bad")
        assert status.ok is False
        assert status.failure_reason == "auth"

    def test_402_returns_no_credit_failure(self):
        err = urllib.error.HTTPError(
            url="https://openrouter.ai/api/v1/auth/key",
            code=402,
            msg="Payment Required",
            hdrs=None,
            fp=io.BytesIO(b""),
        )
        with patch("urllib.request.urlopen", side_effect=err):
            status = check_account("sk-or-v1-broke")
        assert status.ok is False
        assert status.failure_reason == "no_credit"

    def test_unexpected_http_status_raises(self):
        err = urllib.error.HTTPError(
            url="https://openrouter.ai/api/v1/auth/key",
            code=503,
            msg="Service Unavailable",
            hdrs=None,
            fp=io.BytesIO(b""),
        )
        with patch("urllib.request.urlopen", side_effect=err):
            with pytest.raises(AccountCheckError, match="503"):
                check_account("sk-or-v1-key")

    def test_url_error_raises(self):
        err = urllib.error.URLError("connection refused")
        with patch("urllib.request.urlopen", side_effect=err):
            with pytest.raises(AccountCheckError, match="Network error"):
                check_account("sk-or-v1-key")

    def test_non_json_body_raises(self):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"<!DOCTYPE html><html>oops</html>"
        mock_resp.__enter__.return_value = mock_resp
        mock_resp.__exit__.return_value = None
        with patch("urllib.request.urlopen", return_value=mock_resp):
            with pytest.raises(AccountCheckError, match="non-JSON"):
                check_account("sk-or-v1-key")

    def test_missing_data_envelope_returns_ok_with_nones(self):
        # Defensive: if OpenRouter changes their schema, we still return ok
        # rather than crashing. Fields are None.
        with patch("urllib.request.urlopen", return_value=_ok_response({})):
            status = check_account("sk-or-v1-key")
        assert status.ok is True
        assert status.label is None
        assert status.usage is None


# ---------------------------------------------------------------------------
# Keyless models-probe + the non-raising probe_endpoint
# ---------------------------------------------------------------------------

from llm_scripting_kit.account import (  # noqa: E402 -- grouped with its tests
    EndpointProbe,
    check_models_probe,
    probe_endpoint,
)


KEYLESS_CFG = {
    "default_endpoint": "openrouter",
    "endpoints": {
        "openrouter": {
            "base_url": "https://openrouter.ai/api/v1",
            "key_env": "OPENROUTER_API_KEY",
            "account_check": "openrouter",
        },
        "keyless-local": {
            "base_url": "http://localhost:8080/v1",
            "key_env": None,
            "account_check": "models-probe",
        },
        "keyed-local": {
            "base_url": "http://localhost:8081/v1",
            "key_env": "SOME_LOCAL_KEY",
            "account_check": "models-probe",
        },
    },
    "models": {"gpt-mini": {"slug": "openai/gpt-4o-mini"}},
    "default": "gpt-mini",
}


@pytest.fixture
def keyless_config(monkeypatch):
    """Resolve endpoints from KEYLESS_CFG without touching any config file."""
    monkeypatch.setattr(
        "llm_scripting_kit.models.load_model_config", lambda **kw: KEYLESS_CFG
    )


class TestCheckModelsProbeKeyless:
    def test_keyless_sends_no_authorization_and_allows_an_empty_key(self):
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.return_value = _ok_response({})
            status = check_models_probe("", base_url="http://h.invalid/v1", keyless=True)
        assert status.ok is True
        req = mock_open.call_args[0][0]
        assert "Authorization" not in req.headers

    def test_non_keyless_empty_key_still_raises(self):
        with pytest.raises(AccountCheckError, match="empty"):
            check_models_probe("", base_url="http://h.invalid/v1")

    def test_keyed_probe_still_sends_the_bearer(self):
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.return_value = _ok_response({})
            check_models_probe("k", base_url="http://h.invalid/v1")
        req = mock_open.call_args[0][0]
        assert req.headers["Authorization"] == "Bearer k"


class TestProbeEndpoint:
    def test_keyless_endpoint_up(self, keyless_config):
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.return_value = _ok_response({})
            probe = probe_endpoint("keyless-local")
        assert isinstance(probe, EndpointProbe)
        assert probe.ok is True
        assert probe.endpoint == "keyless-local"
        assert probe.base_url == "http://localhost:8080/v1"
        assert probe.detail == "ok"
        req = mock_open.call_args[0][0]
        assert req.full_url == "http://localhost:8080/v1/models"
        assert "Authorization" not in req.headers

    def test_timeout_is_forwarded(self, keyless_config):
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.return_value = _ok_response({})
            probe_endpoint("keyless-local", timeout=7.5)
        assert mock_open.call_args[1]["timeout"] == 7.5

    def test_default_timeout_is_two_seconds(self, keyless_config):
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.return_value = _ok_response({})
            probe_endpoint("keyless-local")
        assert mock_open.call_args[1]["timeout"] == 2.0

    def test_network_failure_is_a_value_not_an_exception(self, keyless_config):
        err = urllib.error.URLError("connection refused")
        with patch("urllib.request.urlopen", side_effect=err):
            probe = probe_endpoint("keyless-local")
        assert probe.ok is False
        assert "connection refused" in probe.detail
        assert probe.base_url == "http://localhost:8080/v1"

    def test_http_error_is_a_value_not_an_exception(self, keyless_config):
        err = urllib.error.HTTPError(
            url="http://localhost:8080/v1/models", code=503,
            msg="Service Unavailable", hdrs=None, fp=io.BytesIO(b""),
        )
        with patch("urllib.request.urlopen", side_effect=err):
            probe = probe_endpoint("keyless-local")
        assert probe.ok is False
        assert "503" in probe.detail

    def test_unknown_endpoint_reports_down_without_raising(self, keyless_config):
        probe = probe_endpoint("no-such-endpoint")
        assert probe.ok is False
        assert "no-such-endpoint" in probe.detail
        assert probe.base_url is None

    def test_unreadable_registry_reports_down_without_raising(
        self, keyless_config, tmp_path, monkeypatch
    ):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        broken = tmp_path / "broken.yaml"
        broken.write_text("models: [unclosed\n", encoding="utf-8")
        monkeypatch.setenv("MODEL_ENDPOINTS_REGISTRY", str(broken))
        probe = probe_endpoint("whatever")
        assert probe.ok is False
        assert "broken.yaml" in probe.detail

    def test_keyed_endpoint_sends_a_bearer(self, keyless_config, monkeypatch):
        monkeypatch.setenv("SOME_LOCAL_KEY", "abc123")
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.return_value = _ok_response({})
            probe = probe_endpoint("keyed-local")
        assert probe.ok is True
        req = mock_open.call_args[0][0]
        assert req.headers["Authorization"] == "Bearer abc123"

    def test_keyed_endpoint_with_no_key_probes_as_down(
        self, keyless_config, monkeypatch, tmp_path
    ):
        monkeypatch.delenv("SOME_LOCAL_KEY", raising=False)
        monkeypatch.setattr(
            "llm_scripting_kit.api_key.USER_ENV_FILE", tmp_path / "nothing" / ".env"
        )
        with patch("urllib.request.urlopen") as mock_open:
            probe = probe_endpoint("keyed-local", project_root=str(tmp_path / "proj"))
        assert probe.ok is False
        assert "SOME_LOCAL_KEY" in probe.detail
        mock_open.assert_not_called()
