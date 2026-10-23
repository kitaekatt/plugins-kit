"""Is a usable Codex CLI installed? -- bootstrap's pre-provisioning probe.

The engine and the agent-skills check ask this question before any plugin venv
exists, so it is standard library only and answers from the CLI's own
``--version`` banner. It builds no command: ``codex exec`` command construction
belongs to ``llm_kit.adapters.codex_command`` in the ``katsoft-llm-kit``
distribution, which bootstrap's standalone interpreter never imports
(tests/bootstrap/test_standalone_closure.py).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Optional, Tuple

CODEX_EXECUTABLE = "codex"

_VERSION_RE = re.compile(
    r"^(?:codex(?:-cli)?\s+)?v?(\d+)\.(\d+)\.(\d+)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CodexDetection:
    """The outcome of looking for a usable Codex CLI.

    ``reason`` is the CLI's version line on success and a specific,
    human-readable failure reason otherwise. ``version`` may be None even when
    ``available`` is True: availability is decided by the CLI running at all,
    never by this module's ability to parse its version banner.
    """

    available: bool
    reason: str
    version: Optional[Tuple[int, int, int]] = None
    argv_prefix: Optional[Tuple[str, ...]] = None


# Detection spawns a subprocess; every orchestration render used to pay for a
# fresh one. Cached for the process lifetime.
_CACHE: Optional[CodexDetection] = None


def reset_detection_cache() -> None:
    """Drop the cached detection result (tests; a re-check after an install)."""
    global _CACHE
    _CACHE = None


def resolve_cli(name: str) -> Optional[Tuple[str, ...]]:
    """Resolve ``name`` to a launchable argv prefix, or None if not on PATH.

    The ``shutil.which`` step is what makes this work on Windows at all: an
    npm/scoop-installed CLI is ``name.cmd``, and CreateProcess does not consult
    PATHEXT the way a shell does. A resolved ``.cmd``/``.bat`` is additionally
    wrapped in ``cmd /c``, because a batch file is not an executable image.
    """
    resolved = shutil.which(name)
    if resolved is None:
        return None
    if os.name == "nt" and resolved.lower().endswith((".cmd", ".bat")):
        return ("cmd", "/c", resolved)
    return (resolved,)


def parse_codex_version(text: str) -> Optional[Tuple[int, int, int]]:
    """Parse a Codex version banner, e.g. ``codex-cli 0.146.0`` -> (0, 146, 0).

    Tolerates a leading ``codex`` / ``codex-cli``, a ``v`` prefix, trailing
    text, and pre-release suffixes. Returns None on anything unparseable and
    never raises -- a banner this cannot read must not make Codex unavailable.
    """
    if not text:
        return None
    try:
        first = str(text).strip().splitlines()[0].strip()
    except Exception:  # pragma: no cover - defensive
        return None
    if not first:
        return None
    match = _VERSION_RE.match(first)
    if match is None:
        return None
    try:
        return (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:  # pragma: no cover - regex already guarantees digits
        return None


def detect_codex(*, timeout: float = 10.0) -> CodexDetection:
    """Is a usable Codex CLI present? Cached for the process lifetime.

    Fails CLOSED on everything -- not on PATH, OSError, any SubprocessError
    (TimeoutExpired included), a nonzero exit. An undetectable backend that
    reports itself available is worse than one that disappears: callers render
    mechanics for, or dispatch to, a tool that is not there.
    """
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    _CACHE = _detect_codex_uncached(timeout=timeout)
    return _CACHE


def _detect_codex_uncached(*, timeout: float) -> CodexDetection:
    prefix = resolve_cli(CODEX_EXECUTABLE)
    if prefix is None:
        return CodexDetection(
            available=False,
            reason="`%s` not found on PATH" % CODEX_EXECUTABLE,
        )
    argv = [*prefix, "--version"]
    shown = "%s --version" % CODEX_EXECUTABLE
    try:
        proc = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return CodexDetection(
            available=False,
            reason="`%s` timed out after %gs" % (shown, timeout),
            argv_prefix=prefix,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return CodexDetection(
            available=False,
            reason="`%s` did not run (%s)" % (shown, type(exc).__name__),
            argv_prefix=prefix,
        )
    output = _decode(proc.stdout)
    if proc.returncode != 0:
        return CodexDetection(
            available=False,
            reason="`%s` exited %s" % (shown, proc.returncode),
            argv_prefix=prefix,
        )
    lines = [line.strip() for line in output.strip().splitlines() if line.strip()]
    banner = lines[0] if lines else "detected"
    return CodexDetection(
        available=True,
        reason=banner,
        version=parse_codex_version(banner),
        argv_prefix=prefix,
    )


def _decode(raw) -> str:
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return str(raw)
