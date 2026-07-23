"""Tests for openrouter_kit.account.check_account.

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

from openrouter_kit.account import AccountCheckError, check_account


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
