"""Shared halt-reason taxonomy for completion pipelines.

A *halt* is an error condition that persists across subsequent calls --
retrying the next work item would fail identically, so the run should stop
cleanly instead of burning through the remaining corpus. Every transport
(OpenRouter HTTP, ``claude -p`` subprocess, ``codex exec`` subprocess)
converges on the same reasons, each through its own marker vocabulary.

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


# Codex CLI failure vocabulary. Codex does NOT emit claude's
# `"api_error_status":NNN` envelope, so _RATE_LIMIT_MARKERS / _AUTH_MARKERS
# above cannot classify it and these exist instead.
#
# PROVENANCE, stated plainly because it bounds how much these can be trusted:
# only the SHAPE of the failure is verified (a persistent usage cap and a
# logged-out CLI both fail every subsequent call, so both are halts). The exact
# WORDING below is GUESSED -- inferred from the common vocabulary of the
# ChatGPT/OpenAI surfaces codex fronts, not read off an observed codex run.
# Markers are therefore deliberately short and generic so a wording variant
# still matches; when a real codex failure is captured, replace them with the
# observed strings rather than adding to the guesses.
# Codex markers are STRUCTURAL, not prose, and that is the whole design.
#
# Codex writes its transcript to both stdout and stderr, so any marker a model
# could plausibly TYPE ("rate limit", "unauthorized") turns a healthy run that
# merely discusses the topic into a forged halt that aborts a bulk run. These
# strings are emitted by the CLI's own error path and are not English a model
# writes in passing, so they can be matched against a raw transcript safely.
#
# VERIFIED against a real failure (codex-cli 0.146.0, provoked by pointing
# CODEX_HOME at an empty dir):
#     ERROR: unexpected status 401 Unauthorized: Missing bearer or basic
#     authentication in header, url: https://api.openai.com/v1/responses
#     failed to connect to websocket: HTTP error: 401 Unauthorized
# The 429 forms mirror the 401 shapes and are UNVERIFIED -- no rate limit was
# provoked. Replace them with observed text when one is seen; do not add loose
# prose markers back.
_CODEX_RATE_LIMIT_MARKERS = (
    "unexpected status 429",
    "http error: 429",
)
_CODEX_AUTH_MARKERS = (
    "unexpected status 401",
    "http error: 401",
    "missing bearer or basic authentication",
)

#: Attributes a transport exception may carry its raw channels on. Scanned
#: because the MESSAGE deliberately holds no transcript -- see
#: :func:`classify_codex_exception`.
_CODEX_CHANNEL_ATTRS = ("stderr", "stdout")


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


def classify_codex_text(text: str) -> Optional[str]:
    """Map a codex CLI text channel (stderr / error body) to a halt kind.

    Rate-limit markers are checked before auth markers so a message carrying
    both classifies as :data:`HALT_RATE_LIMIT`, matching
    :func:`classify_halt_text`. Falls back to the claude/OpenAI marker set,
    which costs nothing and catches a message that quotes an upstream HTTP
    error verbatim. Returns ``None`` when nothing matches.
    """
    if not text:
        return None
    lower = text.lower()
    for marker in _CODEX_RATE_LIMIT_MARKERS:
        if marker in lower:
            return HALT_RATE_LIMIT
    for marker in _CODEX_AUTH_MARKERS:
        if marker in lower:
            return HALT_AUTH
    return classify_halt_text(lower)


def classify_codex_exception(exc: BaseException) -> Optional[str]:
    """Map an exception from the codex CLI transport to a halt kind.

    Typed check first, identically to :func:`classify_claude_exception`: the
    per-call timeout has a dedicated type (:class:`AgentTimeoutError`) and maps
    to :data:`HALT_RATE_LIMIT`, because a CLI-layer backoff is what a timeout
    usually is and both transports must halt-and-resume the same way. The
    substring checks are the fallback for an exception that carries only text.

    The message alone is NOT enough, and assuming it was is a real defect this
    guards against. ``codex_backend.CodexRunError`` deliberately keeps the
    transcript OFF its message (model-authored text there would let a healthy
    run forge a halt), which also means the evidence of a genuine 401 or 429 is
    not in the message either. Classifying on ``str(exc)`` alone therefore
    misses every true halt: a permanent auth failure reads as transient and a
    bulk run retries against a wall forever.

    So the carried channels are scanned too, via :data:`_CODEX_CHANNEL_ATTRS`.
    That is safe ONLY because the codex markers are structural CLI output
    rather than prose -- see their definition. Keep both halves of that
    bargain: transcripts stay off the message, and markers stay unforgeable.
    """
    if isinstance(exc, AgentTimeoutError):
        return HALT_RATE_LIMIT  # CLI-layer backoff is functionally a rate limit
    msg = (str(exc) or "").lower()
    reason = classify_codex_text(msg)
    if reason is not None:
        return reason
    for attr in _CODEX_CHANNEL_ATTRS:
        channel = getattr(exc, attr, None)
        if not isinstance(channel, str):
            continue
        reason = classify_codex_text(channel.lower())
        if reason is not None:
            return reason
    if "exceeded" in msg and "timeout" in msg:
        return HALT_RATE_LIMIT
    return None


def classify_opencode_exception(exc: BaseException) -> Optional[str]:
    """Map an OpenCode exception's transport-authored message to a halt.

    OpenCode's stdout is the answer and may contain arbitrary model prose, so
    its carried ``stdout`` / ``stderr`` channels are deliberately NOT scanned
    here. The backend keeps those channels on exception attributes for
    diagnostics and keeps them out of its message. A timeout is a transport
    failure under the OpenCode dispatch rule, not a persistent halt; checking
    its type first also makes this true if an injected runner supplied a
    message containing halt vocabulary.
    """
    if isinstance(exc, AgentTimeoutError):
        return None
    return classify_halt_text(str(exc))


__all__ = [
    "HALT_AUTH",
    "HALT_RATE_LIMIT",
    "HALT_INSUFFICIENT_CREDIT",
    "HaltError",
    "classify_halt_text",
    "classify_openai_exception",
    "classify_claude_exception",
    "classify_codex_text",
    "classify_codex_exception",
    "classify_opencode_exception",
]
