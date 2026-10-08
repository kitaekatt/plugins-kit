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

import re
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .claude_runner import AgentTimeoutError


HALT_AUTH = "auth"
"""Bad or missing credentials (HTTP 401 / logged-out CLI)."""

HALT_RATE_LIMIT = "rate_limit"
"""Quota exhausted (HTTP 429 / Claude Max cap); clears after a window."""

HALT_INSUFFICIENT_CREDIT = "insufficient_credit"
"""Account credit exhausted (402) or suspended (403)."""

HALT_QUOTA = "quota"
"""Subscription pool spent (codex usage-limit exhaustion).

Unlike every other kind in this module, HALT_QUOTA is never produced by the
text classifiers here -- codex's default (non-``--json``) path emits no
``task_complete`` payload and no stderr marker for its own exhaustion, so
there is no channel this module's substring matchers could scan. It is
classified by :class:`~.codex_backend.CodexCliBackend` re-reading the
session rollout (``usage_budget.read_codex_pool``) after a non-zero exit and
setting the raised :class:`~.codex_backend.CodexRunError`'s ``halt_kind``
attribute, which ``classify_halt`` reports ahead of the text-based
classifier. See
docs/planning/quota-resilient-dispatch/declaration-format-design.md,
Decision 6."""


HALT_BACKPRESSURE = "backpressure"
"""Transient endpoint overload: HTTP 429 that is not a quota or credit
exhaustion, or HTTP 503 with queue/overload/timeout wording. Unlike every other
kind it clears on its own, so the right response is to retry after
``retry_after_s`` (or a backoff of the caller's choosing), not to stop the run.
See :func:`classify_backpressure`."""


class HaltError(Exception):
    """A failure that persists across subsequent calls -- stop the bulk run.

    Carries a machine-readable ``kind`` (one of :data:`HALT_AUTH`,
    :data:`HALT_RATE_LIMIT`, :data:`HALT_INSUFFICIENT_CREDIT`) so a bulk runner
    can halt-and-resume without parsing the message text.
    """

    def __init__(
        self, kind: str, detail: str = "", retry_after_s: Optional[float] = None
    ) -> None:
        self.kind = kind
        self.detail = detail
        #: Seconds the endpoint asked the caller to wait; set for backpressure.
        self.retry_after_s = retry_after_s
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


# OpenCode failure vocabulary, and the CHANNEL RULE that makes scanning safe.
#
# OpenCode passes the PROVIDER's error body through on stderr rather than
# emitting an envelope of its own, so _AUTH_MARKERS / _RATE_LIMIT_MARKERS above
# do most of the work for any provider that emits the standard shapes. These
# are the additions observed from providers that do not.
#
# PROVENANCE: "user not found" was READ OFF A REAL RUN (opencode 1.18.23,
# 2026-08-26) -- an OpenRouter provider with a deliberately invalid key exited 1
# with empty stdout and stderr `Error: User not found.`. Unlike the codex
# markers below-of-it, this is observed rather than guessed. It is also
# PROVIDER text, not OpenCode structure, so it generalises to OpenRouter and
# not necessarily to another provider; add observed strings as they are
# captured rather than inventing variants.
_OPENCODE_AUTH_MARKERS = ("user not found",)

# Scan STDERR ONLY -- never stdout. This is the bargain that lets OpenCode
# classify halts at all, and it rests on a probe rather than an assumption:
# asked to reply with the literal text "BANANA rate limit exceeded insufficient
# credit", the model's words arrived on STDOUT while stderr held 33 bytes of
# OpenCode's own `> build - <model>` framing (opencode 1.18.23, 2026-08-26).
# So stdout is model-authored and forgeable; stderr is transport-authored.
# Adding "stdout" here would let a healthy run that merely DISCUSSES a rate
# limit abort the caller's whole run.
_OPENCODE_CHANNEL_ATTRS = ("stderr",)


# -- backpressure -------------------------------------------------------------
#
# Behavioural reference: Databench's hand-rolled ``is_backpressure``. The status
# is read from the exception (``status_code`` / ``response.status_code``) or from
# the OpenAI SDK's message prefix ``Error code: NNN``; the server's own error
# code is read from the quoted ``'code': '...'`` field of the body.
_QUOTA_MARKERS = (
    "insufficient_quota",
    "quota",
    "billing",
    "credit",
    "usage limit",
    "hit your limit",
    "exceeded your current",
)
_BACKPRESSURE_SERVER_CODES = frozenset(
    {
        "request_queue_timeout",
        "request_queue_full",
        "queue_full",
        "server_busy",
        "server_overloaded",
        "overloaded",
        "overloaded_error",
        "rate_limit_exceeded",
        "too_many_requests",
    }
)
#: Wording that marks a 503 as overload (a bare 503 is an outage, not a halt).
_OVERLOAD_WORDS = ("queue", "overload", "busy", "capacity", "timeout", "timed out", "too many")
_STATUS_RE = re.compile(r"(?:error code|status(?: code)?)[:\s]+(\d{3})\b", re.IGNORECASE)
_SERVER_CODE_RE = re.compile(r"""['"]code['"]\s*:\s*['"]([A-Za-z0-9_.-]+)['"]""")
_RETRY_AFTER_TEXT_RE = re.compile(
    r"retry[-_ ]after['\"]?\s*[:=]?\s*['\"]?(\d+(?:\.\d+)?)"
    r"|try again in (\d+(?:\.\d+)?)\s*(ms|s|sec|seconds?)?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Backpressure:
    """A transient overload signal: the status and the wait the endpoint asked for."""

    status: int
    retry_after_s: Optional[float] = None


def _exception_status(exc: BaseException) -> Optional[int]:
    for holder in (exc, getattr(exc, "response", None)):
        status = getattr(holder, "status_code", None)
        if isinstance(status, int) and not isinstance(status, bool):
            return status
    match = _STATUS_RE.search(str(exc))
    return int(match.group(1)) if match else None


def _retry_after(exc: BaseException) -> Optional[float]:
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is None:
        headers = getattr(exc, "headers", None)
    if headers is not None:
        try:
            raw = headers.get("retry-after") or headers.get("Retry-After")
            if raw is not None:
                return max(0.0, float(raw))
        except (AttributeError, TypeError, ValueError):
            pass  # an HTTP-date or unusable header: fall through to the body hint
    match = _RETRY_AFTER_TEXT_RE.search(str(exc))
    if match:
        if match.group(1) is not None:
            return float(match.group(1))
        seconds = float(match.group(2))
        return seconds / 1000.0 if (match.group(3) or "").lower() == "ms" else seconds
    return None


def classify_backpressure(exc: BaseException) -> Optional[Backpressure]:
    """Report whether ``exc`` is transient endpoint overload, else None.

    - HTTP 429 is backpressure when it carries a positive overload signal (a
      Retry-After header or hint, or queue/overload/``rate_limit_exceeded``
      wording) and no quota or credit wording. A bare 429 with no signal keeps
      its existing :data:`HALT_RATE_LIMIT` classification.
    - HTTP 503 is backpressure only with queue/overload/busy/timeout wording
      (or a known server code such as ``request_queue_timeout``).

    A quota or credit message is never backpressure.
    """
    status = _exception_status(exc)
    if status not in (429, 503):
        return None
    text = str(exc).lower()
    if any(marker in text for marker in _QUOTA_MARKERS):
        return None
    retry_after = _retry_after(exc)
    codes = {code.lower() for code in _SERVER_CODE_RE.findall(str(exc))}
    signalled = (
        retry_after is not None
        or bool(codes & _BACKPRESSURE_SERVER_CODES)
        or any(word in text for word in _OVERLOAD_WORDS)
    )
    if not signalled:
        return None
    return Backpressure(status=status, retry_after_s=retry_after)


def halt_payload(kind: str, exc: Optional[BaseException] = None) -> Dict[str, Any]:
    """The ``{"kind": ..., ...}`` object a CLI envelope carries for a halt.

    Backpressure adds ``retry_after_s`` (a float or None); other kinds carry
    ``kind`` alone.
    """
    payload: Dict[str, Any] = {"kind": kind}
    if kind == HALT_BACKPRESSURE:
        own = getattr(exc, "retry_after_s", None)
        if own is not None:
            # A HaltError already carries the wait; it may have no HTTP status
            # for classify_backpressure to read it from.
            payload["retry_after_s"] = own
        else:
            signal = None if exc is None else classify_backpressure(exc)
            payload["retry_after_s"] = None if signal is None else signal.retry_after_s
    return payload


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
    # Transient overload first: a 429 that names a queue or a Retry-After is not
    # the quota exhaustion RateLimitError otherwise maps to.
    if classify_backpressure(exc) is not None:
        return HALT_BACKPRESSURE
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


def _classify_codex_structural(text: str) -> Optional[str]:
    """Match only the structural codex CLI markers -- no prose fallback.

    Safe against model-authored text: the codex markers are the CLI's own
    error-path output, never something a model could plausibly type in
    passing (see the provenance note on :data:`_CODEX_RATE_LIMIT_MARKERS`).
    Used for channels (stdout) where a healthy run can legitimately contain
    prose that happens to mention a rate limit or an auth failure.
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
    return None


def classify_codex_text(text: str) -> Optional[str]:
    """Map a codex CLI text channel (stderr / error body) to a halt kind.

    Rate-limit markers are checked before auth markers so a message carrying
    both classifies as :data:`HALT_RATE_LIMIT`, matching
    :func:`classify_halt_text`. Falls back to the claude/OpenAI marker set,
    which costs nothing and catches a message that quotes an upstream HTTP
    error verbatim. Returns ``None`` when nothing matches.

    The prose fallback is safe here only because the caller restricts this
    function to transport-authored text -- codex's own error path (an
    exception message) or stderr. It must never be applied to codex's
    stdout, which is model-authored: see
    :func:`classify_codex_exception`, which applies the structural-only
    :func:`_classify_codex_structural` to that channel instead.
    """
    if not text:
        return None
    structural = _classify_codex_structural(text)
    if structural is not None:
        return structural
    return classify_halt_text(text.lower())


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

    The prose fallback inside :func:`classify_codex_text` (the claude/OpenAI
    marker set, e.g. "hit your limit") is transport-safe on stderr -- codex's
    own error path -- but NOT on stdout, which is model-authored. So stdout is
    scanned with the structural-only matcher and stderr with the full one;
    applying the fallback to stdout would let a healthy run whose answer
    merely discusses hitting a limit classify as a persistent halt.
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
        classifier = (
            classify_codex_text if attr == "stderr" else _classify_codex_structural
        )
        reason = classifier(channel.lower())
        if reason is not None:
            return reason
    if "exceeded" in msg and "timeout" in msg:
        return HALT_RATE_LIMIT
    return None


def classify_opencode_exception(exc: BaseException) -> Optional[str]:
    """Map an OpenCode failure to a halt kind, scanning stderr but never stdout.

    The message alone is NOT enough, and treating it as enough is a real defect
    rather than a conservative default -- the same one
    :func:`classify_codex_exception` documents. ``OpencodeRunError``
    deliberately keeps the transcript off its message (model-authored text
    there would let a healthy run forge a halt), so the message is only ever
    the transport's own ``opencode run failed (exit N)``, which carries no halt
    vocabulary by construction. Classifying on ``str(exc)`` alone therefore
    misses EVERY true halt: a permanent auth failure reads as transient and a
    bulk run retries against a wall forever.

    So the carried STDERR channel is scanned as well, via
    :data:`_OPENCODE_CHANNEL_ATTRS` -- and stdout is not. That asymmetry is the
    whole design and it is probe-backed, not assumed: OpenCode puts the model's
    words on stdout and its own framing on stderr, so stdout is forgeable and
    stderr is not. See the comment on those constants for the two runs.

    A timeout stays a TRANSPORT failure here rather than a halt, unlike the
    codex sibling: under the OpenCode dispatch rule an unreachable server hangs
    rather than exiting, so the timeout IS the transport error, not a
    CLI-layer backoff. Checking its type first also keeps that true when an
    injected runner supplies a message containing halt vocabulary.
    """
    if isinstance(exc, AgentTimeoutError):
        return None
    reason = classify_halt_text(str(exc))
    if reason is not None:
        return reason
    for attr in _OPENCODE_CHANNEL_ATTRS:
        channel = getattr(exc, attr, None)
        if not isinstance(channel, str) or not channel:
            continue
        reason = classify_halt_text(channel)
        if reason is not None:
            return reason
        lowered = channel.lower()
        for marker in _OPENCODE_AUTH_MARKERS:
            if marker in lowered:
                return HALT_AUTH
    return None


__all__ = [
    "HALT_AUTH",
    "HALT_RATE_LIMIT",
    "HALT_INSUFFICIENT_CREDIT",
    "HALT_QUOTA",
    "HALT_BACKPRESSURE",
    "Backpressure",
    "classify_backpressure",
    "halt_payload",
    "HaltError",
    "classify_halt_text",
    "classify_openai_exception",
    "classify_claude_exception",
    "classify_codex_text",
    "classify_codex_exception",
    "classify_opencode_exception",
]
