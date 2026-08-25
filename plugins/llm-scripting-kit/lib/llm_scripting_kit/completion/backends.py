"""Provider backends behind one completion protocol.

The seam that lets a pipeline run the same completion-shaped task on either
transport, selected by configuration:

- :class:`OpenRouterBackend` -- an OpenAI-compatible HTTP completion via
  ``make_openai_client(endpoint=...)`` + ``chat.completions.create``. Costs
  real money per the provider's pricing. Consumes ``llm_scripting_kit``'s own
  client factory and model resolver so a named endpoint is honored end to end.
- :class:`ClaudeCliBackend` -- the local ``claude -p`` CLI in pure completion
  mode (no tools, JSON envelope) over :mod:`llm_scripting_kit.completion
  .claude_runner`. Auth and billing come from the CLI's own login (Claude Max /
  a Claude subscription), so the run is subscription-billed rather than metered
  per API call. Recorded cost is flat zero by design.

Both return :class:`LLMResponse` and classify their transport's persistent
failures into the shared halt taxonomy, so orchestrators can halt-and-resume
identically regardless of provider.

A *completion* here is strictly one system prompt + one user prompt -> one text
response. The claude CLI exposes no temperature / max_tokens controls, so
:class:`ClaudeCliBackend` accepts and ignores those options (documented). The
agentic features (a default ``--allowedTools`` set, ``--mcp-config``) are
deliberately absent; ``allowed_tools`` exists for read-only vision use and
nothing more.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from . import halt
from .claude_runner import (
    AgentTimeoutError,
    looks_like_hard_stop,
    run_claude_streaming,
)
from .types import BackendOptions, LLMResponse


# ---------------------------------------------------------------------------
# OpenRouter (OpenAI-compatible HTTP) backend
# ---------------------------------------------------------------------------


@dataclass
class OpenRouterBackend:
    """OpenAI-compatible HTTP completion via an ``llm_scripting_kit`` client.

    A caller may inject a pre-built ``client`` (the test seam / a custom HTTP
    client); when absent, ``make_openai_client(endpoint=...)`` builds one on
    first use -- lazy so callers passing a client never need the ``openai`` SDK.

    ``endpoint`` is passed through to both ``make_openai_client`` and
    ``resolve_model`` so a named OpenAI-compatible endpoint is honored end to
    end. A request ``model`` that is already a concrete slug is used as-is.

    THREAD SAFETY: one instance may be shared across worker threads, and the
    lazy client build is guarded by double-checked locking -- see
    :meth:`_ensure_client`.
    """

    endpoint: Optional[str] = None
    project_root: Optional[Path] = None
    client: Any = None
    name: str = field(default="openrouter", init=False)
    _build_lock: "threading.Lock" = field(
        default_factory=threading.Lock, init=False, repr=False, compare=False
    )

    def _ensure_client(self) -> Any:
        """Return the shared client, building it at most once.

        Double-checked locking, and every part of that shape is load-bearing:

        - The build MUST be serialized. It was an unsynchronized
          check-then-assign, so a concurrent first wave had every thread find
          ``client`` unset and build its own. Each build calls
          ``ssl.create_default_context(cafile=certifi.where())`` and takes a
          file descriptor -- measured downstream at 509 distinct clients and
          391 ``[Errno 24] Too many open files`` errors from 900 threads
          through a single shared transport (Windows' 512-entry table).
        - Only the BUILD is serialized, never the requests behind it. Callers
          take the lock only to construct; ``complete`` issues its HTTP call
          outside it, so the pool does not collapse to one in-flight request.
        - The fast path is lock-free, so the warm case (and ``classify_halt``,
          which the platform calls unguarded inside ``except`` and which must
          build nothing) never contends.
        - The gate opens on CLIENT EXISTS, not REQUEST SUCCEEDED: ``client`` is
          assigned as soon as the object is constructed. Gating on a successful
          request instead would leave the slot empty through the whole first
          round-trip and let the next wave build again.
        """
        if self.client is not None:
            return self.client
        with self._build_lock:
            # Re-check: another thread may have built while we waited.
            if self.client is None:
                from ..client import make_openai_client  # noqa: PLC0415
                self.client = make_openai_client(
                    project_root=self.project_root, endpoint=self.endpoint
                )
        return self.client

    def _resolve_model(self, model: str) -> str:
        """Resolve an alias to a concrete slug; a raw slug resolves to itself."""
        from ..models import resolve_model  # noqa: PLC0415
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
        on the static prefix; otherwise it is a plain string.
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
        if opts.extras:
            # The generic escape hatch: anything the caller puts in `extras`
            # rides as TOP-LEVEL request parameters (``reasoning_effort`` is the
            # motivating case). Omitted entirely when empty, so the request
            # shape for existing callers is byte-identical.
            create_kwargs["extra_body"] = dict(opts.extras)

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
        return halt.classify_openai_exception(exc)


# ---------------------------------------------------------------------------
# Claude CLI backend
# ---------------------------------------------------------------------------


def _resolve_claude_executable() -> str:
    """Locate the ``claude`` CLI via ``shutil.which``.

    Raises a clear error if claude cannot be found so a missing CLI surfaces as
    an actionable message rather than a cryptic subprocess failure.
    """
    found = shutil.which("claude")
    if found:
        return found
    raise RuntimeError(
        "Could not locate the `claude` CLI. Install it "
        "(https://claude.com/code) or ensure it is on PATH for this process."
    )


# Transient HTTP statuses worth retrying. 500 / 502 / 503 / 504 are server-side
# and typically clear within a minute; 429 / 401 are excluded -- those persist
# across calls and the caller's hard-stop path is the right response.
_RETRYABLE_TRANSIENT_STATUSES = frozenset([500, 502, 503, 504])


def _envelope_transient_status(stdout: str) -> Optional[int]:
    """Return the ``api_error_status`` if stdout signals a retryable 5xx.

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
    if status in _RETRYABLE_TRANSIENT_STATUSES:
        return status
    return None


@dataclass
class ClaudeCliBackend:
    """``claude -p`` completion transport over :mod:`.claude_runner`.

    Attributes:
        default_timeout_s: Per-call watchdog when the caller's options carry no
            ``timeout_s``. 900s default -- generous for a large completion,
            tight enough that a silent CLI-layer rate-limit backoff cannot
            stall a bulk run.
        retry_max_attempts / retry_cooldown_s: Transient-5xx retry budget (the
            envelope can carry api_error_status 5xx with exit 0). Hard stops
            (429/401) never retry.
        diagnostics_dir: Where timeout diagnostics dump. ``None`` disables
            dumping (the raised error still carries inline stdout/stderr tails).
        executable: Explicit path to the ``claude`` binary. ``None`` resolves it
            via ``shutil.which`` at call time.
        runner: The subprocess runner -- test seam; production is
            :func:`.claude_runner.run_claude_streaming`.

    Cost/usage: the JSON envelope's ``usage`` block is read best-effort (absent
    on older CLIs -> zeros). Recorded cost is flat zero by design -- Claude Max
    billing happens at the subscription, not per call.
    """

    default_timeout_s: float = 900.0
    retry_max_attempts: int = 3
    retry_cooldown_s: float = 60.0
    diagnostics_dir: Optional[Path] = None
    executable: Optional[str] = None
    runner: Callable[..., "tuple[str, str, int]"] = run_claude_streaming
    name: str = field(default="claude-cli", init=False)

    def complete(
        self,
        system: str,
        user: str,
        *,
        model: str,
        options: Optional[BackendOptions] = None,
    ) -> LLMResponse:
        # The claude CLI exposes no temperature / max_tokens knobs; they are
        # accepted for protocol compatibility and ignored. Tests stub this
        # backend via ``runner=`` (and optionally ``executable=``).
        opts = options or BackendOptions()
        timeout_s = (
            opts.timeout_s if opts.timeout_s is not None else self.default_timeout_s
        )
        cwd = opts.cwd if opts.cwd is not None else Path.cwd()
        executable = self.executable or _resolve_claude_executable()

        cmd = [
            executable,
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
        last_stdout = ""
        last_stderr = ""
        last_returncode = 0
        attempts_made = 0
        for attempt in range(self.retry_max_attempts):
            attempts_made = attempt + 1
            try:
                last_stdout, last_stderr, last_returncode = self.runner(
                    cmd,
                    user,
                    cwd,
                    log_prefix=opts.log_prefix,
                    timeout_s=timeout_s,
                )
            except AgentTimeoutError as exc:
                # A timeout is not retryable: the CLI was already killed after
                # the full per-call budget, so re-issuing would just burn
                # another budget's worth of wall time.
                self._dump_timeout_diagnostics(exc)
                raise
            transient = _envelope_transient_status(last_stdout)
            if transient is None or attempt == self.retry_max_attempts - 1:
                break
            sys.stderr.write(
                f"{opts.log_prefix} api_error_status={transient} (transient "
                f"server error); sleeping {self.retry_cooldown_s}s before "
                f"retry (attempt {attempt + 2}/{self.retry_max_attempts})\n"
            )
            sys.stderr.flush()
            time.sleep(self.retry_cooldown_s)

        stdout, stderr, returncode = last_stdout, last_stderr, last_returncode
        if returncode != 0:
            raise RuntimeError(
                f"claude -p failed (exit {returncode}):\n"
                f"stderr: {stderr}\nstdout: {stdout}"
            )

        data = json.loads(stdout)
        # Surface hard-stop markers (rate-limit OR auth failure) even when the
        # CLI returns exit 0. Claude Max daily/weekly caps come back as
        # api_error_status=429 with "hit your limit" in the body; auth failures
        # come back as api_error_status=401. Both persist across subsequent
        # calls, so raise with the raw error body -- halt classifiers match on
        # those substrings.
        status = data.get("api_error_status")
        result_body = (
            data.get("result", "") if isinstance(data.get("result"), str) else ""
        )
        if status in (429, 401) or looks_like_hard_stop(result_body):
            # The double quote before api_error_status is LOAD-BEARING, not
            # decoration: halt._RATE_LIMIT_MARKERS / _AUTH_MARKERS match only
            # the canonical `"api_error_status":NNN` or the bare
            # `api_error_status:NNN`. Emitting an opening paren there (the
            # original typo) produced a message this backend's OWN
            # classify_halt could not classify whenever the body carried no
            # marker of its own -- so call_llm raised a plain RuntimeError
            # instead of HaltError and a bulk loop kept spending against a
            # persistent failure. Keep the quote; keep the raise classifiable.
            raise RuntimeError(
                f'claude -p hard-stop error ("api_error_status":{status}): '
                f'{result_body or "unknown"}'
            )
        if data.get("is_error"):
            raise RuntimeError(
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

    def classify_halt(self, exc: BaseException) -> Optional[str]:
        return halt.classify_claude_exception(exc)

    def _dump_timeout_diagnostics(self, exc: AgentTimeoutError) -> None:
        """Best-effort timeout triage dump (cmd redacted of the prompt body).

        Disabled when ``diagnostics_dir`` is None. Never raises.
        """
        if self.diagnostics_dir is None:
            return
        ts = time.strftime("%Y%m%d_%H%M%S")
        path = self.diagnostics_dir / f"timeout_{ts}_{os.getpid()}.log"
        try:
            self.diagnostics_dir.mkdir(parents=True, exist_ok=True)
            redacted_cmd = []
            skip = False
            for arg in exc.cmd:
                if skip:
                    redacted_cmd.append(f"<{len(arg)} chars>")
                    skip = False
                    continue
                if arg == "--system-prompt":
                    skip = True
                redacted_cmd.append(arg)
            with path.open("w", encoding="utf-8") as f:
                f.write(f"timeout at {ts}, elapsed {exc.elapsed_s}s\n")
                f.write(f"cmd: {redacted_cmd}\n\n")
                f.write(f"===== stderr ({len(exc.stderr)} chars) =====\n")
                f.write(exc.stderr)
                if not exc.stderr.endswith("\n"):
                    f.write("\n")
                f.write(f"\n===== stdout ({len(exc.stdout)} chars) =====\n")
                f.write(exc.stdout)
        except Exception as dump_exc:  # noqa: BLE001 -- triage is best-effort
            sys.stderr.write(
                f"[claude-cli] diagnostic dump failed ({dump_exc}); continuing.\n"
            )


__all__ = ["OpenRouterBackend", "ClaudeCliBackend"]
