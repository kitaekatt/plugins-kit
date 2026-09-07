"""Codex CLI detection and command construction -- the single source of truth.

The consumer is llm-scripting-kit's ``CodexCliBackend`` (detection plus the
argv it dispatches). Anything else that DISPATCHES codex belongs here too.

awesome-kit's orchestrate skill deliberately does NOT consume this module,
and the omission is a decision rather than an oversight: its
``orchestration_guidance.py`` is a stdlib-only policy renderer, and its
``detect_backend`` is a GENERIC backend detector driven by YAML, not a codex
one. Importing here would couple a generic renderer to a codex-specific
module and cost a manifest change, a version bump and a venv re-exec guard to
share three lines of platform trivia. It therefore keeps its own resolver;
the two must be changed together. Its detector is not exposed to the injection
guard below because it only ever runs a version probe built from trusted
config, never caller-supplied paths.

Stdlib only. ``bootstrap_lib`` is imported from contexts where no third-party
dependency is guaranteed to exist (SessionStart hooks, a plugin whose venv has
not been provisioned yet), so nothing here may import outside the stdlib.

Three empirical findings are encoded as hard behaviour rather than advice:

* A CLI installed by npm or scoop is ``codex.cmd`` on Windows, which
  CreateProcess cannot find from the bare name -- ``shutil.which`` is the whole
  fix -- and a batch launcher additionally has to be run through ``cmd /c``.
* ``-s workspace-write`` silently degrades to read-only on Windows unless
  ``-c windows.sandbox="unelevated"`` is passed. That config key was the single
  deciding variable; nothing in the CLI output announces the degradation.
* A RELATIVE ``-C`` combined with ``--add-dir`` silently voids the entire
  writable-root set: every write then fails while the process still exits 0.
  Hence every path this module emits must be absolute, enforced by raising.
"""

from __future__ import annotations

import ntpath
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence, Tuple, Union

CODEX_EXECUTABLE = "codex"

#: Probe values for `-c` keys whose type is not a plain enum-ish string.
_VERSION_RE = re.compile(
    r"^(?:codex(?:-cli)?\s+)?v?(\d+)\.(\d+)\.(\d+)",
    re.IGNORECASE,
)

PathLike = Union[str, "os.PathLike[str]"]


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


#: Characters cmd.exe re-parses as command syntax. VERIFIED on Python 3.12.9 /
#: Windows 11: `subprocess.list2cmdline` quotes only for spaces, tabs and
#: quotes -- per the MSVC argv convention, which cmd.exe does not follow -- so
#: an element carrying one of these WITHOUT a space reaches cmd.exe unquoted
#: and is executed as a separate command. Reproduced end to end: an argv
#: element `hello&echo>INJECTED.txt` created that file. Running the `.cmd`
#: directly instead of through `cmd /c` does NOT help; it is still re-parsed.
_CMD_METACHARACTERS = ("&", "|", "^", "<", ">")


def _reject_cmd_metacharacters(argv: Sequence[str]) -> None:
    """Refuse an argv that cmd.exe would re-parse into extra commands.

    Only reachable when a batch launcher put ``cmd`` in front (Windows,
    npm/scoop install). Refusing is deliberate: the alternative is silently
    executing caller-supplied text as a command, and every value here is a
    path, a model id, or a config pair, none of which legitimately needs shell
    syntax. A directory genuinely containing ``&`` is legal on Windows, so this
    raises a NAMED error rather than mangling the value.
    """
    for item in argv:
        hit = next((c for c in _CMD_METACHARACTERS if c in item), None)
        if hit is not None:
            raise ValueError(
                "refusing to launch through cmd.exe: argument %r contains %r, "
                "which cmd re-parses as command syntax (subprocess quotes only "
                "for spaces, so it would run as a separate command). Move the "
                "path off a directory containing shell metacharacters, or "
                "install Codex so it resolves to a real executable rather than "
                "a .cmd launcher." % (item, hit)
            )


def _is_cmd_launcher(value: str) -> bool:
    """Return whether ``value`` names cmd.exe, including a Windows path."""
    stem, _suffix = ntpath.splitext(ntpath.basename(value))
    return stem.casefold() == "cmd"


def _absolute(value: PathLike, param: str) -> str:
    """Return ``value`` as a str, refusing a relative path.

    A relative path here is not a style problem. VERIFIED: a relative ``-C``
    combined with ``--add-dir`` silently voids the ENTIRE writable-root set --
    every write inside the session fails while the process still exits 0, so
    the failure looks like the model declining to write.
    """
    text = str(value)
    # os.path.isabs, not Path.is_absolute: os.path is bound to this platform's
    # flavour at import, so the check cannot be perturbed by anything that
    # rebinds os.name later (the emitted argv still is, deliberately -- see the
    # Windows sandbox key).
    if not os.path.isabs(text):
        raise ValueError(
            "%s must be an absolute path (got %r): a relative -C combined with "
            "--add-dir silently voids the entire writable-root set, so every "
            "write fails while codex still exits 0" % (param, text)
        )
    return text


def build_codex_exec_argv(
    *,
    root: PathLike,
    scratch_dir: Optional[PathLike] = None,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    output_file: Optional[PathLike] = None,
    add_dirs: Sequence[PathLike] = (),
    sandbox: str = "workspace-write",
    network: bool = True,
    output_schema: Optional[PathLike] = None,
    json_events: bool = False,
    extra_config: Iterable[str] = (),
    argv_prefix: Optional[Sequence[str]] = None,
) -> list:
    """Build the ONE sanctioned ``codex exec`` invocation.

    Emitted shape (stdin ``-`` is ALWAYS last, so the prompt rides stdin and
    never argv)::

        <prefix> exec -s <sandbox>
            -c windows.sandbox="unelevated"                 # Windows only
            -c sandbox_workspace_write.network_access=true  # when network
            [-m MODEL] [-c model_reasoning_effort=EFFORT] [-c EXTRA]...
            -C <root> [--add-dir D]... [-o FILE] [--output-schema F] [--json]
            --skip-git-repo-check --color never -

    ``root``, every ``add_dirs`` entry, ``scratch_dir``, ``output_file`` and
    ``output_schema`` MUST be absolute; a relative one raises ValueError naming
    the parameter. See ``_absolute`` -- a relative ``-C`` alongside ``--add-dir``
    silently voids the whole writable-root set while the process still exits 0.

    ``windows.sandbox="unelevated"`` is emitted unconditionally on Windows:
    without it ``-s workspace-write`` silently degrades to read-only (VERIFIED
    as the single deciding variable).

    ``scratch_dir`` is appended to the add-dirs. The session scratchpad lives
    under TEMP, which is otherwise denied because ``exclude_tmpdir_env_var`` is
    commonly true.

    ``extra_config`` is an iterable of raw ``key=value`` strings, each appended
    as a further ``-c`` pair after the ones this function derives.

    ``argv_prefix=None`` resolves the launcher via ``resolve_cli`` and raises
    RuntimeError when Codex is not on PATH.
    """
    if argv_prefix is None:
        resolved = resolve_cli(CODEX_EXECUTABLE)
        if resolved is None:
            raise RuntimeError(
                "`%s` is not on PATH, so no Codex command can be built "
                "(install the Codex CLI or pass argv_prefix=)" % CODEX_EXECUTABLE
            )
        argv_prefix = resolved

    root_str = _absolute(root, "root")
    dirs = [_absolute(d, "add_dirs[%d]" % i) for i, d in enumerate(add_dirs)]
    if scratch_dir is not None:
        dirs.append(_absolute(scratch_dir, "scratch_dir"))

    argv = [str(part) for part in argv_prefix]
    argv += ["exec", "-s", str(sandbox)]
    if os.name == "nt":
        argv += ["-c", 'windows.sandbox="unelevated"']
    if network:
        argv += ["-c", "sandbox_workspace_write.network_access=true"]
    if model:
        argv += ["-m", str(model)]
    if effort:
        argv += ["-c", "model_reasoning_effort=%s" % effort]
    for item in extra_config:
        argv += ["-c", str(item)]
    argv += ["-C", root_str]
    for directory in dirs:
        argv += ["--add-dir", directory]
    if output_file is not None:
        argv += ["-o", _absolute(output_file, "output_file")]
    if output_schema is not None:
        argv += ["--output-schema", _absolute(output_schema, "output_schema")]
    if json_events:
        argv.append("--json")
    argv += ["--skip-git-repo-check", "--color", "never", "-"]
    # Only when a batch launcher forced a cmd.exe hop -- a real executable is
    # handed straight to CreateProcess and never re-parsed.
    if any(_is_cmd_launcher(part) for part in argv_prefix):
        _reject_cmd_metacharacters(argv)
    return argv

