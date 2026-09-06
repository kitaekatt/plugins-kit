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
from pathlib import Path
from typing import Any, Dict, Optional

from .constants import BASE_URL


def _get(
    url: str, headers: Dict[str, str], timeout: float
) -> "tuple[bytes, Optional[BaseException]]":
    """Shared ``GET`` seam for ``check_account``, ``check_models_probe`` and
    ``probe_endpoint``: one urllib Request/urlopen/HTTPError/URLError/OSError
    ladder instead of three copies.

    Returns ``(body_bytes, None)`` on success. On failure returns
    ``(b"", exc)`` -- the CAUGHT exception itself
    (``urllib.error.HTTPError``, ``urllib.error.URLError``, or another
    ``OSError``/``TimeoutError``), never translated here. Translation stays
    with each caller deliberately: a 401/402 means something different to
    ``check_account``/``check_models_probe`` (a credential-specific
    ``AccountStatus`` value, never an exception) than to ``probe_endpoint``
    (an ``EndpointProbe`` with no special-cased status codes at all), and the
    message wording each already produces must not change underneath a
    caller that parses it.
    """
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read(), None
    except urllib.error.HTTPError as e:
        return b"", e
    except urllib.error.URLError as e:
        return b"", e
    except (TimeoutError, OSError) as e:
        return b"", e


class AccountCheckError(Exception):
    """Raised when the account check fails for a non-credential reason.

    For credential-specific failures (401, 402), prefer reading
    ``AccountStatus.ok`` and ``AccountStatus.failure_reason`` -- the call
    still returns a status object so the caller can render a remediation hint.
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

    url = f"{base_url}/auth/key"
    raw, exc = _get(url, {"Authorization": f"Bearer {api_key}"}, timeout)
    if exc is not None:
        if isinstance(exc, urllib.error.HTTPError):
            if exc.code == 401:
                return _failure("auth")
            if exc.code == 402:
                return _failure("no_credit")
            raise AccountCheckError(f"{url} returned HTTP {exc.code}: {exc.reason}") from exc
        if isinstance(exc, urllib.error.URLError):
            raise AccountCheckError(f"Network error contacting {base_url}: {exc.reason}") from exc
        raise AccountCheckError(f"Transport error contacting {base_url}: {exc}") from exc

    try:
        body = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        # A UnicodeDecodeError is a ValueError subclass, so left unhandled it
        # would escape as a bare ValueError instead of the AccountCheckError
        # every other decode/transport failure in this module raises.
        raise AccountCheckError(f"{url} returned a non-UTF-8 body: {e}") from e
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as e:
        raise AccountCheckError(f"{url} returned non-JSON body: {e}") from e
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


def check_models_probe(
    api_key: str, *, base_url: str, timeout: float = 10.0, keyless: bool = False
) -> AccountStatus:
    """Validate ``api_key`` with a generic ``GET /models`` probe.

    For OpenAI-compatible endpoints that have no ``/auth/key`` equivalent. A 2xx
    means the key authenticated; 401 -> auth failure. Fields OpenRouter's
    ``/auth/key`` reports (usage, limit, ...) are unavailable here and stay None.

    ``keyless`` True is for an endpoint that declares no credential at all (the
    norm for a locally hosted server): an empty ``api_key`` is accepted and NO
    Authorization header is sent. Keyed behavior is unchanged.
    """
    if not api_key and not keyless:
        raise AccountCheckError("api_key is empty -- nothing to check")

    headers = {} if keyless else {"Authorization": f"Bearer {api_key}"}
    url = f"{base_url}/models"
    raw, exc = _get(url, headers, timeout)
    if exc is not None:
        if isinstance(exc, urllib.error.HTTPError):
            if exc.code == 401:
                return _failure("auth")
            if exc.code == 402:
                return _failure("no_credit")
            raise AccountCheckError(f"{url} returned HTTP {exc.code}: {exc.reason}") from exc
        if isinstance(exc, urllib.error.URLError):
            raise AccountCheckError(f"Network error contacting {base_url}: {exc.reason}") from exc
        raise AccountCheckError(f"Transport error contacting {base_url}: {exc}") from exc
    return AccountStatus(
        ok=True, label=None, usage=None, limit=None, is_free_tier=None,
        rate_limit=None, failure_reason=None, raw=None,
    )


def validate_endpoint(
    endpoint_cfg: Dict[str, Any], api_key: str, *, timeout: float = 10.0
) -> Optional[AccountStatus]:
    """Validate a key against an endpoint per its ``account_check`` mode.

    ``endpoint_cfg`` is a dict as returned by
    ``llm_scripting_kit.models.resolve_endpoint`` (needs ``base_url`` and
    ``account_check``). Modes:

    - ``"openrouter"`` -- OpenRouter's ``GET /auth/key`` (returns AccountStatus).
    - ``"models-probe"`` -- generic ``GET /models`` (returns AccountStatus).
    - ``"none"`` -- validation not supported; returns ``None`` (skipped).

    An unknown mode is treated as ``"none"``. An endpoint resolving with
    ``key_env`` None is keyless: the models probe then sends no Authorization
    header and tolerates an empty ``api_key``.
    """
    mode = endpoint_cfg.get("account_check") or "none"
    base_url = endpoint_cfg["base_url"]
    if mode == "openrouter":
        return check_account(api_key, timeout=timeout, base_url=base_url)
    if mode == "models-probe":
        return check_models_probe(
            api_key,
            base_url=base_url,
            timeout=timeout,
            keyless=endpoint_cfg.get("key_env") is None,
        )
    return None


@dataclass(frozen=True)
class EndpointProbe:
    """Result of a non-raising reachability ping.

    ``detail`` is ``"ok"`` on success, else the resolve or network failure text.

    ``resolved`` is False only when the endpoint NAME itself could not be
    resolved (unknown endpoint, unreadable/dangling model-endpoints registry)
    -- no request was ever attempted. True for every other case, including a
    resolved endpoint whose key could not be found and a resolved endpoint
    that answered with a network/HTTP failure: a request target existed, even
    though sending or completing the request did not succeed.
    ``reachability.check_transport`` reads this to report STATUS_UNKNOWN
    ("I could not check") rather than STATUS_UNREACHABLE ("I checked and it
    is down") for the ``resolved=False`` case.
    """

    ok: bool
    endpoint: str
    base_url: Optional[str]
    detail: str
    resolved: bool = True


def probe_endpoint(
    name: Optional[str] = None,
    *,
    timeout: float = 2.0,
    project_root: Optional[str] = None,
) -> EndpointProbe:
    """Quick ``GET {base_url}/models`` ping of a named endpoint. Never raises.

    A keyless endpoint is probed with no Authorization header; a keyed one has
    its key resolved and sent as a Bearer, and a key that does not resolve
    reports ``ok=False`` naming the variable rather than crashing. Resolve
    failures (unknown endpoint, unreadable registry) and network failures are
    likewise returned as ``ok=False`` with the reason in ``detail`` -- this is
    an "is it up?" question whose answer is a value, not an exception.

    The default ``timeout`` is 2s: a reachable endpoint answers in milliseconds,
    while a dead host burns the whole budget, so the ceiling is what a caller
    on the interactive path actually waits.
    """
    from .models import resolve_endpoint  # noqa: PLC0415 -- avoids a cycle

    label = name or "<default>"
    try:
        ep = resolve_endpoint(name, project_root=project_root)
    except Exception as e:  # noqa: BLE001 -- any resolve defect is "not usable"
        return EndpointProbe(
            ok=False, endpoint=label, base_url=None, detail=str(e), resolved=False
        )

    label = ep.get("name") or label
    base_url = ep.get("base_url")
    if not base_url:
        return EndpointProbe(
            ok=False, endpoint=label, base_url=None, detail="endpoint has no base_url"
        )

    headers: Dict[str, str] = {}
    key_env = ep.get("key_env")
    if key_env is not None:
        from .api_key import get_api_key  # noqa: PLC0415 -- avoids a cycle

        try:
            result = get_api_key(
                Path(project_root) if project_root is not None else None,
                endpoint=label,
            )
        except Exception as e:  # noqa: BLE001
            return EndpointProbe(
                ok=False,
                endpoint=label,
                base_url=base_url,
                detail=f"cannot resolve key {key_env}: {e}",
            )
        if not result.key:
            return EndpointProbe(
                ok=False,
                endpoint=label,
                base_url=base_url,
                detail=f"no API key resolved ({key_env} unset)",
            )
        headers["Authorization"] = f"Bearer {result.key}"

    try:
        _raw, exc = _get(f"{base_url}/models", headers, timeout)
    except Exception as e:  # noqa: BLE001 -- a malformed URL etc.; _get itself
        # only catches HTTPError/URLError/OSError/TimeoutError, so anything
        # else (e.g. ValueError from a malformed base_url) still lands here.
        return EndpointProbe(
            ok=False, endpoint=label, base_url=base_url, detail=f"unreachable: {e}"
        )
    if exc is None:
        return EndpointProbe(ok=True, endpoint=label, base_url=base_url, detail="ok")
    if isinstance(exc, urllib.error.HTTPError):
        return EndpointProbe(
            ok=False, endpoint=label, base_url=base_url,
            detail=f"HTTP {exc.code}: {exc.reason}",
        )
    if isinstance(exc, urllib.error.URLError):
        return EndpointProbe(
            ok=False, endpoint=label, base_url=base_url,
            detail=f"unreachable: {exc.reason}",
        )
    return EndpointProbe(
        ok=False, endpoint=label, base_url=base_url, detail=f"unreachable: {exc}"
    )


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
