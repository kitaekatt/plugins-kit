"""Completion backends behind one protocol, selected by process-level routing.

Three transports implement :class:`~content_pipeline.llm.platform.LLMBackend`:

- :class:`OpenRouterBackend` -- an OpenAI-compatible HTTP completion. Consumes
  ``openrouter_kit`` for the ready-made client and model resolution (lazy /
  optional import: the mock path works without it, and a caller may inject a
  ``client`` directly). ``endpoint=`` is passed through to
  ``make_openai_client`` / ``resolve_model``.
- :class:`ClaudeCliBackend` -- a THIN adapter over the local ``claude -p``
  CLI: spawn with UTF-8 pipes, a per-call timeout, transient-5xx retry, and
  hard-stop mapping. Deliberately minimal -- the proposal has this migrating
  down into ``openrouter_kit`` later (a "use Claude locally instead of an
  endpoint" backend) without this module's call-site interface changing. The
  subprocess is injected as a ``runner`` seam so tests never spawn.
- :class:`MockBackend` -- deterministic, scriptable responses. The always-wins
  test seam: no network, no subprocess. Scriptable with a FIFO queue, a
  content-addressed map, or an exception to raise (for retry / halt tests).

:func:`route` reads a process-level env var (``CONTENT_PIPELINE_LLM_BACKEND``)
and returns the active backend, mirroring loc's ``routing.py`` -- backend
selection is one process-wide switch rather than a parameter threaded through
every call site. The returned response's ``model`` reflects the model that
ACTUALLY ran (routing substitutes when the requested id means nothing to the
active transport), so audit stamping stays truthful.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

from content_pipeline.llm import platform
from content_pipeline.llm.platform import BackendOptions, LLMResponse

# ---------------------------------------------------------------------------
# OpenRouter (OpenAI-compatible HTTP) backend
# ---------------------------------------------------------------------------


@dataclass
class OpenRouterBackend:
    """OpenAI-compatible HTTP completion via an ``openrouter_kit`` client.

    The client and model resolution are consumed from ``openrouter_kit`` --
    imported lazily so the mock path (and this whole module) loads without the
    shared lib or the ``openai`` SDK installed. A caller may inject a
    pre-built ``client`` (the test seam / a custom HTTP client); when absent,
    ``make_openai_client(endpoint=...)`` builds one on first use.

    ``endpoint`` is passed through to both ``make_openai_client`` and
    ``resolve_model`` so a named OpenAI-compatible endpoint (see
    llm-scripting-kit's endpoints model) is honored end to end. A request
    ``model`` that is already a concrete slug is used as-is; ``resolve_model``
    is consulted only when the shared lib is available and the id is an alias.
    """

    endpoint: Optional[str] = None
    project_root: Optional[Path] = None
    client: Any = None
    name: str = field(default="openrouter", init=False)

    def _ensure_client(self) -> Any:
        if self.client is not None:
            return self.client
        try:
            from openrouter_kit import make_openai_client  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - env-dependent
            raise ImportError(
                "OpenRouterBackend needs the 'openrouter_kit' shared lib (from "
                "llm-scripting-kit) unless a client is injected. Declare it via "
                "shared_lib_imports, or pass client= for tests."
            ) from exc
        self.client = make_openai_client(
            project_root=self.project_root, endpoint=self.endpoint
        )
        return self.client

    def _resolve_model(self, model: str) -> str:
        """Resolve a model alias to a concrete slug when the shared lib is present.

        A raw slug (already concrete, e.g. contains ``/``) or an unavailable
        shared lib means the id is used verbatim -- the backend never fails
        just because ``openrouter_kit`` is absent when the caller passed a
        concrete slug.
        """
        try:
            from openrouter_kit import resolve_model  # noqa: PLC0415
        except ImportError:
            return model
        try:
            return resolve_model(
                model,
                endpoint=self.endpoint,
                project_root=str(self.project_root)
                if self.project_root is not None
                else None,
            )
        except Exception:  # noqa: BLE001 - a concrete slug resolves to itself
            return model

    def complete(
        self,
        system: str,
        user: str,
        *,
        model: str,
        options: Optional[BackendOptions] = None,
    ) -> LLMResponse:
        """Run one Chat-Completions call and normalize the result.

        Marks the (non-empty) system prompt with a ``cache_control: ephemeral``
        breakpoint so a provider that supports prompt caching serves the stable
        prefix from cache. When ``options.user_cache_prefix`` is set, the user
        message is emitted as a two-part content list with a second breakpoint
        on the static prefix; otherwise it is a plain string (byte-identical to
        the single-block shape).
        """
        opts = options or BackendOptions()
        client = self.client if self.client is not None else self._ensure_client()
        resolved_model = self._resolve_model(model)

        if system:
            system_content: Any = [
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
            ]
        else:
            system_content = system

        if opts.user_cache_prefix:
            user_content: Any = [
                {
                    "type": "text",
                    "text": opts.user_cache_prefix,
                    "cache_control": {"type": "ephemeral"},
                },
                {"type": "text", "text": user},
            ]
        else:
            user_content = user

        create_kwargs: Dict[str, Any] = {
            "model": resolved_model,
            "messages": [
                {"role": "system", "content": system_content},
                {"role": "user", "content": user_content},
            ],
            "temperature": opts.temperature,
            "max_tokens": opts.max_tokens,
        }
        if opts.timeout_s is not None:
            create_kwargs["timeout"] = opts.timeout_s

        start = time.monotonic()
        response = client.chat.completions.create(**create_kwargs)
        wall_ms = int((time.monotonic() - start) * 1000)

        text = response.choices[0].message.content or ""
        usage = getattr(response, "usage", None)
        input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        cache_hit_tokens = 0
        ptd = getattr(usage, "prompt_tokens_details", None)
        if isinstance(ptd, dict):
            cache_hit_tokens = int(ptd.get("cached_tokens", 0) or 0)
        elif ptd is not None:
            cache_hit_tokens = int(getattr(ptd, "cached_tokens", 0) or 0)
        if not cache_hit_tokens:
            cache_hit_tokens = int(getattr(usage, "prompt_cache_hit_tokens", 0) or 0)

        return LLMResponse(
            text=text,
            model=resolved_model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_hit_tokens=cache_hit_tokens,
            wall_ms=wall_ms,
            attempts=1,
            from_cache=False,
        )

    def classify_halt(self, exc: BaseException) -> Optional[str]:
        return platform.classify_openai_exception(exc)


# ---------------------------------------------------------------------------
# Claude CLI backend (thin adapter)
# ---------------------------------------------------------------------------


class ClaudeCliError(RuntimeError):
    """Raised when the ``claude -p`` subprocess fails non-transiently."""


class ClaudeCliTimeout(ClaudeCliError):
    """Raised when the ``claude -p`` subprocess exceeds its per-call timeout."""


# Transient envelope statuses worth one more attempt. 429 / 401 are excluded
# -- they persist across calls and are the caller's hard-stop path.
_RETRYABLE_STATUSES = frozenset([500, 502, 503, 504])


def _default_runner(
    cmd: List[str],
    request: str,
    cwd: Path,
    *,
    timeout_s: float,
) -> "tuple[str, str, int]":
    """Spawn ``claude -p`` with UTF-8 pipes and a bounded wait.

    The minimal generic core of gen-ops' ``claude_runner`` -- enough to run a
    completion and enforce a timeout. Returns ``(stdout, stderr, returncode)``.
    Raises :class:`ClaudeCliTimeout` on timeout. Injected as the ``runner``
    seam so tests never actually spawn.
    """
    import subprocess  # noqa: PLC0415

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(cwd),
    )
    try:
        stdout, stderr = proc.communicate(input=request, timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        stdout, stderr = proc.communicate()
        raise ClaudeCliTimeout(
            f"claude -p exceeded {timeout_s}s timeout"
        ) from exc
    return stdout, stderr, proc.returncode


@dataclass
class ClaudeCliBackend:
    """Thin ``claude -p`` completion adapter.

    Spawns the CLI in pure-completion mode (JSON output, no tools by default),
    enforces a per-call timeout, retries transient 5xx envelopes, and maps
    rate-limit / auth markers to a hard stop. Cost is flat zero (the CLI bills
    at its own subscription, not per call), so ``LLMResponse`` carries the
    usage the envelope reports but the platform prices it against no table.

    ``runner`` is the subprocess seam: ``(cmd, request, cwd, *, timeout_s) ->
    (stdout, stderr, returncode)``. Production is :func:`_default_runner`;
    tests inject a scripted stub.
    """

    default_timeout_s: float = 900.0
    retry_max_attempts: int = 3
    retry_cooldown_s: float = 60.0
    executable: str = "claude"
    runner: Callable[..., "tuple[str, str, int]"] = _default_runner
    name: str = field(default="claude-cli", init=False)

    def complete(
        self,
        system: str,
        user: str,
        *,
        model: str,
        options: Optional[BackendOptions] = None,
    ) -> LLMResponse:
        opts = options or BackendOptions()
        timeout_s = opts.timeout_s if opts.timeout_s is not None else self.default_timeout_s
        cwd = opts.cwd if opts.cwd is not None else Path.cwd()

        cmd = [
            self.executable,
            "-p",
            "--model",
            model,
            "--system-prompt",
            system,
            "--output-format",
            "json",
            "--no-session-persistence",
            "--permission-mode",
            "bypassPermissions",
            "--allowedTools",
            opts.allowed_tools if opts.allowed_tools is not None else "",
        ]
        if opts.effort is not None:
            cmd.extend(["--effort", opts.effort])

        start = time.monotonic()
        stdout = stderr = ""
        returncode = 0
        attempts_made = 0
        for attempt in range(self.retry_max_attempts):
            attempts_made = attempt + 1
            stdout, stderr, returncode = self.runner(
                cmd, user, cwd, timeout_s=timeout_s
            )
            transient = self._transient_status(stdout)
            if transient is None or attempt == self.retry_max_attempts - 1:
                break
            time.sleep(self.retry_cooldown_s)

        if returncode != 0:
            raise ClaudeCliError(
                f"claude -p failed (exit {returncode}): {stderr or stdout}"
            )

        data = json.loads(stdout)
        status = data.get("api_error_status")
        result_body = data.get("result", "") if isinstance(data.get("result"), str) else ""
        if status in (429, 401) or platform.classify_halt_text(result_body):
            raise ClaudeCliError(
                f'claude -p hard-stop (api_error_status={status}): '
                f'{result_body or "unknown"}'
            )
        if data.get("is_error"):
            raise ClaudeCliError(
                f"claude -p returned error: {data.get('result', 'unknown')}"
            )

        usage = data.get("usage") or {}
        wall_ms = int((time.monotonic() - start) * 1000)
        return LLMResponse(
            text=data["result"],
            model=model,
            input_tokens=int(usage.get("input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            cache_hit_tokens=int(usage.get("cache_read_input_tokens", 0) or 0),
            wall_ms=wall_ms,
            attempts=attempts_made,
            from_cache=False,
        )

    @staticmethod
    def _transient_status(stdout: str) -> Optional[int]:
        """Return the envelope ``api_error_status`` if it is a retryable 5xx.

        A 500 has been observed with both exit 0 and exit 1, so the envelope is
        inspected regardless of returncode.
        """
        try:
            data = json.loads(stdout)
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        status = data.get("api_error_status")
        return status if status in _RETRYABLE_STATUSES else None

    def classify_halt(self, exc: BaseException) -> Optional[str]:
        if isinstance(exc, ClaudeCliTimeout):
            return platform.HALT_RATE_LIMIT  # CLI-layer backoff manifests as a timeout
        return platform.classify_halt_text(str(exc))


# ---------------------------------------------------------------------------
# Mock backend (test seam)
# ---------------------------------------------------------------------------


@dataclass
class MockBackend:
    """Deterministic, scriptable completion backend for tests.

    Scriptable three ways:

    - ``responses`` -- a FIFO queue drained one per call. Each entry is a
      ``str`` (the response text), a ``dict`` (``{text, model?, input_tokens?,
      ...}``), an :class:`LLMResponse` (served verbatim), or an ``Exception``
      instance (raised, to exercise retry / halt paths).
    - ``keyed_responses`` -- a ``{substring: entry}`` map: the first key found
      in the user prompt wins (order-independent concurrency tests).
    - both empty -- every call raises ``RuntimeError("MockBackend exhausted")``.

    ``classify_halt`` maps a raised entry to a halt kind when its message
    carries a marker, so a scripted ``HaltError``-shaped exception halts.
    Every call's kwargs are recorded on ``self.calls``.
    """

    responses: Optional[List[Any]] = None
    keyed_responses: Optional[Dict[str, Any]] = None
    default_model: str = "mock-model"
    name: str = field(default="mock", init=False)
    calls: List[Dict[str, Any]] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self._queue: List[Any] = list(self.responses) if self.responses else []

    def _coerce(self, entry: Any, model: str) -> LLMResponse:
        if isinstance(entry, LLMResponse):
            return entry
        if isinstance(entry, str):
            return LLMResponse(text=entry, model=model)
        if isinstance(entry, dict):
            return LLMResponse(
                text=str(entry.get("text", "")),
                model=str(entry.get("model", model)),
                input_tokens=int(entry.get("input_tokens", 0)),
                output_tokens=int(entry.get("output_tokens", 0)),
                cache_hit_tokens=int(entry.get("cache_hit_tokens", 0)),
                wall_ms=int(entry.get("wall_ms", 0)),
            )
        raise TypeError(
            f"MockBackend: unsupported response entry {type(entry).__name__}"
        )

    def complete(
        self,
        system: str,
        user: str,
        *,
        model: str,
        options: Optional[BackendOptions] = None,
    ) -> LLMResponse:
        self.calls.append(
            {"system": system, "user": user, "model": model, "options": options}
        )
        effective_model = model or self.default_model
        if self.keyed_responses is not None:
            for substring, entry in self.keyed_responses.items():
                if substring in user:
                    if isinstance(entry, BaseException):
                        raise entry
                    return self._coerce(entry, effective_model)
            raise RuntimeError(
                f"MockBackend keyed_responses: no key matched (keys: "
                f"{list(self.keyed_responses)})"
            )
        if not self._queue:
            raise RuntimeError("MockBackend exhausted")
        entry = self._queue.pop(0)
        if isinstance(entry, BaseException):
            raise entry
        return self._coerce(entry, effective_model)

    def classify_halt(self, exc: BaseException) -> Optional[str]:
        if isinstance(exc, platform.HaltError):
            return exc.kind
        return platform.classify_halt_text(str(exc))


# ---------------------------------------------------------------------------
# Process-level routing
# ---------------------------------------------------------------------------

BACKEND_ENV = "CONTENT_PIPELINE_LLM_BACKEND"
"""Process-level backend selection env var (empty / unset => openrouter)."""

MODEL_ENV = "CONTENT_PIPELINE_LLM_MODEL"
"""Optional override for the model a routed non-openrouter backend runs."""


def active_backend_name() -> str:
    """The process-active backend name (``"openrouter"`` when unset)."""
    return os.environ.get(BACKEND_ENV, "").strip() or "openrouter"


def set_active_backend(name: Optional[str]) -> None:
    """Set (or clear, with ``None`` / ``"openrouter"``) the active backend.

    Writes the env var rather than module state so worker subprocesses
    inherit the selection.
    """
    if name and name != "openrouter":
        os.environ[BACKEND_ENV] = name
    else:
        os.environ.pop(BACKEND_ENV, None)


def route(
    *,
    openrouter: Optional[Any] = None,
    claude_cli: Optional[Any] = None,
    mock: Optional[Any] = None,
) -> Any:
    """Return the process-active backend instance.

    Reads :data:`BACKEND_ENV`. A caller-supplied instance for the active name
    wins (the mock seam always wins so tests never route to a live transport);
    otherwise a default instance is constructed. Mirrors loc's ``routing.py``:
    backend selection is a process-wide switch, not a per-call parameter.
    """
    name = active_backend_name()
    if name == "mock":
        return mock if mock is not None else MockBackend()
    if name == "claude-cli":
        return claude_cli if claude_cli is not None else ClaudeCliBackend()
    return openrouter if openrouter is not None else OpenRouterBackend()


def routed_model(requested_model: str, *, backend_name: Optional[str] = None) -> str:
    """Resolve the model a routed call should run, with truthful substitution.

    An OpenRouter-style slug means nothing to the claude-cli transport, so a
    routed non-openrouter call substitutes :data:`MODEL_ENV` when the requested
    id does not already name that backend's family. The substituted id is what
    lands on ``LLMResponse.model`` and therefore on audit records.
    """
    name = backend_name or active_backend_name()
    if name == "claude-cli" and not requested_model.startswith("claude"):
        return os.environ.get(MODEL_ENV, "").strip() or requested_model
    return requested_model


__all__ = [
    "OpenRouterBackend",
    "ClaudeCliBackend",
    "ClaudeCliError",
    "ClaudeCliTimeout",
    "MockBackend",
    "BACKEND_ENV",
    "MODEL_ENV",
    "active_backend_name",
    "set_active_backend",
    "route",
    "routed_model",
]
