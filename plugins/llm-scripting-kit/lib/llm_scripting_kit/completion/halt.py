"""Shared halt-reason taxonomy for completion pipelines.

A *halt* is an error condition that persists across subsequent calls --
retrying the next work item would fail identically, so the run should stop
cleanly instead of burning through the remaining corpus. Both transports
(OpenRouter HTTP, ``claude -p`` subprocess) converge on the same reasons.

Reason constants are plain strings (not an enum) because they are persisted
into audit records and matched by CLI exit-code mappers -- the string values
are the stable contract. The marker matchers accept every shape observed in
the wild: JSON-quoted (``"api_error_status":429``) and bare
(``api_error_status:429``).
"""
from __future__ import annotations

from typing import Optional

from .claude_runner import AgentTimeoutError


HALT_AUTH = "auth"
"""Bad or missing credentials (HTTP 401 / logged-out CLI)."""

HALT_RATE_LIMIT = "rate_limit"
"""Quota exhausted (HTTP 429 / Claude Max cap); clears after a window."""

HALT_INSUFFICIENT_CREDIT = "insufficient_credit"
"""Account credit exhausted (402) or suspended (403)."""


class HaltError(Exception):
    """A failure that persists across subsequent calls -- stop the bulk run.

    Carries a machine-readable ``kind`` (one of :data:`HALT_AUTH`,
    :data:`HALT_RATE_LIMIT`, :data:`HALT_INSUFFICIENT_CREDIT`) so a bulk runner
    can halt-and-resume without parsing the message text.
    """

    def __init__(self, kind: str, detail: str = "") -> None:
        self.kind = kind
        self.detail = detail
        super().__init__(f"{kind}: {detail}" if detail else kind)


# All matching is done on lowercased text; every marker below is lowercase.
_RATE_LIMIT_MARKERS = (
    "hit your limit",
    '"api_error_status":429',
    "api_error_status:429",
)
_AUTH_MARKERS = (
    '"api_error_status":401',
    "api_error_status:401",
    "authentication_error",
    "invalid authentication credentials",
)


def classify_halt_text(text: str) -> Optional[str]:
    """Map a provider text channel (error body / stderr) to a halt kind.

    Rate-limit markers are checked before auth markers, so a message carrying
    both classifies as :data:`HALT_RATE_LIMIT`. Returns ``None`` when no marker
    matches.
    """
    if not text:
        return None
    lower = text.lower()
    for marker in _RATE_LIMIT_MARKERS:
        if marker in lower:
            return HALT_RATE_LIMIT
    for marker in _AUTH_MARKERS:
        if marker in lower:
            return HALT_AUTH
    return None


def classify_openai_exception(exc: BaseException) -> Optional[str]:
    """Map an OpenAI-SDK exception (or one wrapping it) to a halt kind.

    Returns :data:`HALT_AUTH`, :data:`HALT_RATE_LIMIT`, or
    :data:`HALT_INSUFFICIENT_CREDIT` for the known persistent failures; ``None``
    otherwise. The ``openai`` import is optional -- when absent, the
    text-marker fallback still catches the common shapes. Recurses on
    ``__cause__`` so a wrapped SDK exception is still classified.
    """
    try:
        import openai  # noqa: PLC0415
    except ImportError:
        openai = None  # type: ignore[assignment]
    if openai is not None:
        auth_error = getattr(openai, "AuthenticationError", None)
        if auth_error is not None and isinstance(exc, auth_error):
            return HALT_AUTH
        rate_error = getattr(openai, "RateLimitError", None)
        if rate_error is not None and isinstance(exc, rate_error):
            return HALT_RATE_LIMIT
        # HTTP 402 (insufficient credit) maps to the base APIStatusError class
        # -- the SDK has no named 402 subclass. HTTP 403 (suspended account) is
        # also a hard stop: every subsequent call fails identically.
        status_error = getattr(openai, "APIStatusError", None)
        if status_error is not None and isinstance(exc, status_error):
            if getattr(exc, "status_code", None) in (402, 403):
                return HALT_INSUFFICIENT_CREDIT
    from_text = classify_halt_text(str(exc))
    if from_text is not None:
        return from_text
    cause = getattr(exc, "__cause__", None)
    if cause is not None and cause is not exc:
        return classify_openai_exception(cause)
    return None


def classify_claude_exception(exc: BaseException) -> Optional[str]:
    """Map an exception from the claude CLI transport to a halt kind.

    Typed check first: the per-call timeout has a dedicated exception type
    (:class:`AgentTimeoutError`), so classification does not depend on message
    wording -- a CLI-layer rate-limit backoff manifests as a timeout, which is
    functionally a rate limit. The substring checks stay as a fallback for a
    wrapped exception that only carries text.
    """
    if isinstance(exc, AgentTimeoutError):
        return HALT_RATE_LIMIT  # CLI-layer backoff is functionally a rate limit
    msg = (str(exc) or "").lower()
    reason = classify_halt_text(msg)
    if reason is not None:
        return reason
    if "exceeded" in msg and "timeout" in msg:
        return HALT_RATE_LIMIT
    return None


__all__ = [
    "HALT_AUTH",
    "HALT_RATE_LIMIT",
    "HALT_INSUFFICIENT_CREDIT",
    "HaltError",
    "classify_halt_text",
    "classify_openai_exception",
    "classify_claude_exception",
]
