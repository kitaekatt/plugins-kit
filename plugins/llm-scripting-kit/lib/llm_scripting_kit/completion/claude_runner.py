"""Shared prompt-on-stdin CLI subprocess runner for completion pipelines.

Named for its first caller (``claude -p``) and kept there so the import path
stays stable, but :func:`run_cli_streaming` is transport-neutral: it writes a
prompt to stdin, drains both pipes, honours a timeout, and kills on a
caller-supplied stderr marker set. :class:`CodexCliBackend` drives it too.

This shared transport centralizes fixes for a main-thread-blocking
``readline()`` hang and a cp1252 encoding bug. The defensive details are the
point of this module and must be preserved:

- UTF-8 pipes (``encoding="utf-8"``, ``errors="replace"``) so CJK / non-Latin
  request bodies survive a Windows cp1252 locale codec instead of corrupting.
- Both stdout AND stderr drained on daemon threads. ``claude -p
  --output-format json`` emits stdout only at exit, so a main-thread
  ``readline()`` would block until EOF and the per-call timeout would never be
  checked -- a hung CLI would hang the run forever.
- A bounded per-call timeout that wakes exactly when the next action is due
  (deadline or heartbeat), raising the typed :class:`AgentTimeoutError`.
- A live stderr hard-stop kill: the moment a rate-limit / auth marker appears
  on stderr, the subprocess is killed instead of burning the whole timeout.

Leaf module: stdlib only. It must not import anything from a consuming
pipeline so it can sit underneath every backend without a dependency cycle.
The only legitimate per-caller differences -- the stderr log prefix and the
per-call timeout -- are parameters; everything else is shared.
"""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from pathlib import Path


# Hard-stop markers in claude -p's stderr that mean "this run is over, do not
# retry, surface to the caller as a clean halt". Shared verbatim so the same
# Claude Max / auth failures are treated identically across every pipeline.
HARD_STOP_STDERR_MARKERS = (
    # Claude Max rate limit
    "hit your limit",
    '"api_error_status":429',
    # Auth failure: 401 persists across every call until the user re-logs in to
    # the CLI. Treat like a rate limit -- stop the whole bulk run instead of
    # burning cost on guaranteed failures.
    '"api_error_status":401',
    "authentication_error",
    "invalid authentication credentials",
)


# Heartbeat cadence for long calls: print a "still running" line if the
# subprocess has gone silent on both stdout and stderr for this long. Without
# this a stuck call looks identical to "the LLM is thinking" in the terminal.
_CLAUDE_HEARTBEAT_S = 120


class AgentTimeoutError(RuntimeError):
    """Raised when the ``claude -p`` subprocess exceeds the per-call timeout.

    Subclasses RuntimeError so existing ``except RuntimeError`` / message
    handling keeps working; the distinct type lets a caller classify the
    timeout halt without parsing the message text. Carries the killed call's
    context (``cmd``, ``elapsed_s``, ``stdout``, ``stderr``) so a caller can
    dump diagnostics without re-deriving it.
    """

    def __init__(
        self,
        message: str,
        *,
        cmd: list[str],
        elapsed_s: int,
        stdout: str,
        stderr: str,
    ) -> None:
        super().__init__(message)
        self.cmd = cmd
        self.elapsed_s = elapsed_s
        self.stdout = stdout
        self.stderr = stderr


def looks_like_hard_stop(
    text: str, markers: tuple[str, ...] = HARD_STOP_STDERR_MARKERS,
) -> bool:
    """True if ``text`` contains any marker that should halt a bulk run.

    Covers both Claude Max rate limits (429 / "hit your limit") and auth
    failures (401 / authentication_error / invalid credentials). Both classes
    of error persist across subsequent calls and must halt the run instead of
    silently burning through work.
    """
    if not text:
        return False
    lower = text.lower()
    for marker in markers:
        if marker.lower() in lower:
            return True
    return False


def run_cli_streaming(
    cmd: list[str],
    request: str,
    cwd: Path,
    *,
    log_prefix: str,
    timeout_s: float,
    hard_stop_markers: tuple[str, ...] = HARD_STOP_STDERR_MARKERS,
    label: str = "claude -p",
    env: Mapping[str, str] | None = None,
) -> tuple[str, str, int]:
    """Run a prompt-on-stdin CLI with streamed stderr and a per-call timeout.

    Transport-neutral despite this module's name: every claude-specific detail
    is a parameter (``hard_stop_markers`` for the stderr kill vocabulary,
    ``label`` for the CLI named in raised error messages), so a second CLI
    transport reuses the drain/timeout machinery rather than re-deriving it.
    ``run_claude_streaming`` remains as a back-compat alias.

    Returns ``(stdout, stderr, returncode)``. Echoes stderr to the parent
    process in real time prefixed with ``log_prefix`` so rate-limit / backoff
    messages surface immediately.

    The pipes are explicitly UTF-8: a request may carry non-Latin text, and on
    Windows the default locale codec is cp1252, which cannot encode it.
    ``errors="replace"`` on the read side keeps a stray invalid byte in the
    CLI's output from killing the whole call.

    Both stdout and stderr are drained on daemon threads; the main thread only
    does a bounded ``proc.wait``. If any stderr line matches a hard-stop marker
    the subprocess is killed immediately and a ``RuntimeError`` is raised
    carrying that marker substring. On exceeding ``timeout_s`` the subprocess
    is killed and an :class:`AgentTimeoutError` is raised carrying ``cmd``,
    ``elapsed_s``, ``stdout``, and ``stderr``.
    """
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(cwd),
        bufsize=1,
        env=dict(env) if env is not None else None,
    )

    stderr_chunks: list[str] = []
    stdout_chunks: list[str] = []
    hard_stop_hit: list[str] = []

    def drain_stderr():
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_chunks.append(line)
            sys.stderr.write(f"{log_prefix} {line}")
            sys.stderr.flush()
            if (
                looks_like_hard_stop(line, hard_stop_markers)
                and not hard_stop_hit
            ):
                hard_stop_hit.append(line.strip())
                try:
                    proc.kill()
                except Exception:
                    pass

    def drain_stdout():
        assert proc.stdout is not None
        for line in proc.stdout:
            stdout_chunks.append(line)

    stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
    stderr_thread.start()
    stdout_thread = threading.Thread(target=drain_stdout, daemon=True)
    stdout_thread.start()

    try:
        proc.stdin.write(request)
        proc.stdin.close()
    except BrokenPipeError:
        pass

    start = time.monotonic()
    last_heartbeat = start
    while True:
        # Wait in deadline/heartbeat-sized slices instead of a fixed poll: the
        # wait wakes exactly when the next action (the per-call timeout or a
        # heartbeat line) is due. The 1.0s floor keeps a near-zero remainder
        # from degenerating into a busy spin, and is itself capped at the
        # deadline remainder so a short (test-sized) timeout is never
        # overshot by the floor.
        now = time.monotonic()
        remaining_deadline = timeout_s - (now - start)
        remaining_heartbeat = _CLAUDE_HEARTBEAT_S - (now - last_heartbeat)
        wait_s = max(
            min(1.0, remaining_deadline),
            min(remaining_deadline, remaining_heartbeat),
            0.0,
        )
        try:
            proc.wait(timeout=wait_s)
            break
        except subprocess.TimeoutExpired:
            pass
        elapsed = time.monotonic() - start
        if elapsed > timeout_s:
            proc.kill()
            proc.wait(timeout=5)
            stderr_thread.join(timeout=2)
            stdout_thread.join(timeout=2)
            full_stderr = "".join(stderr_chunks)
            full_stdout = "".join(stdout_chunks)
            stderr_tail = full_stderr[-2000:] or "<empty>"
            stdout_tail = full_stdout[-500:] or "<empty>"
            raise AgentTimeoutError(
                f"{label} exceeded {timeout_s}s timeout "
                f"(elapsed {int(elapsed)}s; likely rate-limit backoff "
                f"at the CLI layer).\n"
                f"stderr tail:\n{stderr_tail}\n"
                f"stdout tail:\n{stdout_tail}",
                cmd=cmd,
                elapsed_s=int(elapsed),
                stdout=full_stdout,
                stderr=full_stderr,
            )
        if time.monotonic() - last_heartbeat > _CLAUDE_HEARTBEAT_S:
            last_heartbeat = time.monotonic()
            sys.stderr.write(
                f"{log_prefix} (still running, elapsed {int(elapsed)}s)\n"
            )
            sys.stderr.flush()

    stderr_thread.join(timeout=5)
    stdout_thread.join(timeout=5)

    if hard_stop_hit:
        raise RuntimeError(
            f"{label} hard-stop error (rate limit or auth failure): "
            f"{hard_stop_hit[0]}"
        )

    return "".join(stdout_chunks), "".join(stderr_chunks), proc.returncode


#: Back-compat alias. content-pipeline-kit imports this name from the shared
#: lib, and a shared lib reaches every consumer at once with no version pin --
#: so the old name stays bound to the same object rather than being renamed out
#: from under them.
run_claude_streaming = run_cli_streaming


__all__ = [
    "AgentTimeoutError",
    "HARD_STOP_STDERR_MARKERS",
    "looks_like_hard_stop",
    "run_cli_streaming",
    "run_claude_streaming",
]
