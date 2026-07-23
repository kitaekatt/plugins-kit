"""OpenRouter account validation via the public ``/auth/key`` endpoint.

Uses ``urllib`` from the standard library so this check has no third-party
dependencies and can run from the bootstrap engine's own venv.

OpenRouter's ``GET /auth/key`` returns a JSON document like::

    {
      "data": {
        "label": "sk-or-v1-... (display name)",
        "limit": null,
        "usage": 0.123,
        "is_free_tier": false,
        "rate_limit": {"requests": 200, "interval": "10s"}
      }
    }

A 401 means the key is bad or revoked; 402 means the OpenRouter account is
out of credit; other non-2xx responses are surfaced verbatim so the caller
can decide whether to retry or fail.
"""

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .constants import BASE_URL


class AccountCheckError(Exception):
    """Raised when the account check fails for a non-credential reason.

    For credential-specific failures (401, 402), prefer reading
    ``AccountStatus.ok`` and ``AccountStatus.failure_reason`` -- the call
    still returns a status object so the caller can render a fix-all hint.
    """


@dataclass(frozen=True)
class AccountStatus:
    """Snapshot of an OpenRouter account's credential health.

    ``ok`` is True when the key authenticated successfully and the account
    can make API calls. ``failure_reason`` is one of:

    - ``"auth"``       -- HTTP 401 (bad or revoked key)
    - ``"no_credit"``  -- HTTP 402 (account out of credit)
    - ``None``         -- success
    """

    ok: bool
    label: Optional[str]
    usage: Optional[float]
    limit: Optional[float]
    is_free_tier: Optional[bool]
    rate_limit: Optional[Dict[str, Any]]
    failure_reason: Optional[str]
    raw: Optional[Dict[str, Any]]


def check_account(
    api_key: str, *, timeout: float = 10.0, base_url: str = BASE_URL
) -> AccountStatus:
    """Validate ``api_key`` against an OpenRouter-style ``/auth/key`` endpoint.

    Args:
        api_key: The key to validate. An empty string or None raises
            ``AccountCheckError`` immediately rather than wasting a request.
        timeout: Socket timeout in seconds. Defaults to 10s -- the bootstrap
            session-start hook should not stall longer than that on a network
            hiccup.
        base_url: Endpoint base URL. Defaults to OpenRouter. Only endpoints that
            expose OpenRouter's proprietary ``/auth/key`` support this check.

    Returns:
        AccountStatus describing the key's health.

    Raises:
        AccountCheckError: For network/transport errors and unexpected HTTP
            statuses (anything other than 200, 401, 402). Use this to
            distinguish "the user's key is bad" (returns ok=False) from
            "we could not reach the endpoint" (raises).
    """
    if not api_key:
        raise AccountCheckError("api_key is empty -- nothing to check")

    req = urllib.request.Request(
        f"{base_url}/auth/key",
        headers={"Authorization": f"Bearer {api_key}"},
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            payload = json.loads(body)
            data = payload.get("data", {}) if isinstance(payload, dict) else {}
            return AccountStatus(
                ok=True,
                label=data.get("label"),
                usage=data.get("usage"),
                limit=data.get("limit"),
                is_free_tier=data.get("is_free_tier"),
                rate_limit=data.get("rate_limit"),
                failure_reason=None,
                raw=payload if isinstance(payload, dict) else None,
            )
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return _failure("auth")
        if e.code == 402:
            return _failure("no_credit")
        raise AccountCheckError(
            f"{base_url}/auth/key returned HTTP {e.code}: {e.reason}"
        ) from e
    except urllib.error.URLError as e:
        raise AccountCheckError(f"Network error contacting {base_url}: {e.reason}") from e
    except (TimeoutError, OSError) as e:
        raise AccountCheckError(f"Transport error contacting {base_url}: {e}") from e
    except json.JSONDecodeError as e:
        raise AccountCheckError(f"{base_url}/auth/key returned non-JSON body: {e}") from e


def check_models_probe(
    api_key: str, *, base_url: str, timeout: float = 10.0
) -> AccountStatus:
    """Validate ``api_key`` with a generic ``GET /models`` probe.

    For OpenAI-compatible endpoints that have no ``/auth/key`` equivalent. A 2xx
    means the key authenticated; 401 -> auth failure. Fields OpenRouter's
    ``/auth/key`` reports (usage, limit, ...) are unavailable here and stay None.
    """
    if not api_key:
        raise AccountCheckError("api_key is empty -- nothing to check")

    req = urllib.request.Request(
        f"{base_url}/models",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()  # drain; we only care that it authenticated
            return AccountStatus(
                ok=True, label=None, usage=None, limit=None, is_free_tier=None,
                rate_limit=None, failure_reason=None, raw=None,
            )
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return _failure("auth")
        if e.code == 402:
            return _failure("no_credit")
        raise AccountCheckError(
            f"{base_url}/models returned HTTP {e.code}: {e.reason}"
        ) from e
    except urllib.error.URLError as e:
        raise AccountCheckError(f"Network error contacting {base_url}: {e.reason}") from e
    except (TimeoutError, OSError) as e:
        raise AccountCheckError(f"Transport error contacting {base_url}: {e}") from e


def validate_endpoint(
    endpoint_cfg: Dict[str, Any], api_key: str, *, timeout: float = 10.0
) -> Optional[AccountStatus]:
    """Validate a key against an endpoint per its ``account_check`` mode.

    ``endpoint_cfg`` is a dict as returned by
    ``openrouter_kit.models.resolve_endpoint`` (needs ``base_url`` and
    ``account_check``). Modes:

    - ``"openrouter"`` -- OpenRouter's ``GET /auth/key`` (returns AccountStatus).
    - ``"models-probe"`` -- generic ``GET /models`` (returns AccountStatus).
    - ``"none"`` -- validation not supported; returns ``None`` (skipped).

    An unknown mode is treated as ``"none"``.
    """
    mode = endpoint_cfg.get("account_check") or "none"
    base_url = endpoint_cfg["base_url"]
    if mode == "openrouter":
        return check_account(api_key, timeout=timeout, base_url=base_url)
    if mode == "models-probe":
        return check_models_probe(api_key, base_url=base_url, timeout=timeout)
    return None


def _failure(reason: str) -> AccountStatus:
    return AccountStatus(
        ok=False,
        label=None,
        usage=None,
        limit=None,
        is_free_tier=None,
        rate_limit=None,
        failure_reason=reason,
        raw=None,
    )
