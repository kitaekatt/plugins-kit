"""Interpreter names for bootstrap manifest commands, sessions, and shells.

Manifest commands (a bootstrap.json ``tools`` check/install, an env.json
``env_checks`` check/fix) are opaque shell strings the engine hands to bash.
When one needs Python it must not guess a name on PATH: on Windows ``python3``
is usually absent and ``python`` may be any stranger's interpreter. Two names
carry the answer:

* ``BOOTSTRAP_PYTHON`` -- the bootstrap interpreter. ``begin_pass`` sets it to
  the interpreter the engine runs under, overriding any inherited value.
* ``BOOTSTRAP_PROJECT_PYTHON`` -- the project's interpreter by DEFAULT (never
  forced): ``begin_pass`` clears it, and ``export_project_default`` sets it
  from the normative resolution rule (``default_project_python``). A project
  that declares ``"project_python": false`` opted out: the name is not
  exported for it at all.

What this module writes, and what it leaves to callers:

* ``begin_pass`` touches ``os.environ`` only.
* ``export_project_default`` and ``export_project_python`` set
  ``os.environ`` and buffer the name for ``$CLAUDE_ENV_FILE`` through
  ``session_env.record`` (a no-op when that variable is unset).
* ``write_record`` / ``remove_record`` maintain the per-project record file
  ``<data_dir>/project_python/<key>`` that the SessionStart hook prelude and
  the throttled always lane read.
* Machine-wide persistence of ``BOOTSTRAP_PYTHON`` (rc files, the Windows
  registry) and the shell hook are the engine's ``_process_interpreter_env``;
  ``interpreter_env_settings`` only reads their two opt-outs.

Every path value uses the form bash consumes: absolute, forward slashes, and a
drive letter on Windows (``C:/...``).

The call-site expressions below are the copy-paste forms manifest authors use;
the ``:?`` form makes an engine that predates the export fail the command with
``MIN_VERSION_HINT`` instead of running whatever ``python`` is on PATH.

Stdlib and sibling imports only.
"""

from __future__ import annotations

import os
import posixpath
import re
import sys

from . import session_env, venv_check

ENGINE_VAR = "BOOTSTRAP_PYTHON"
PROJECT_VAR = "BOOTSTRAP_PROJECT_PYTHON"
MIN_VERSION = "0.120.0"
MIN_VERSION_HINT = f"requires bootstrap >= {MIN_VERSION}"
PLUGIN_CALL_SITE_EXPR = '"${BOOTSTRAP_PYTHON:?' + MIN_VERSION_HINT + '}"'
CALL_SITE_EXPR = (
    '"${BOOTSTRAP_PROJECT_PYTHON:-${BOOTSTRAP_PYTHON:?' + MIN_VERSION_HINT + '}}"'
)
# Discoverability anchors (interface-v3): the /bootstrap fact id, the reference
# document under skills/bootstrap/references/, and that document's H1. Lint
# messages, docs tests, and the SKILL fact resolve against these exact strings.
FACT_ID = "python_interpreter"
REFERENCE_DOC = "python-interpreter.md"
REFERENCE_HEADING = "Python interpreter variables"

# The bootstrap-owned standalone interpreter lives here, relative to home. This
# literal is MIRRORED (not linked) in session-bootstrap.sh, bootstrap.sh,
# bootstrap-display.sh, shell/project-python.sh, and defaults/config.json
# good_python_dir; a drift test asserts the spellings agree.
STANDALONE_DIR_REL = ".local/share/python-standalone"

# Test-isolation signal (set by tests/conftest.py for every test, inherited by
# engine subprocesses): when present, the engine performs NO persistence and NO
# shell-integration writes (rc files, PowerShell profiles, registry) for the
# interpreter names. Same role as BOOTSTRAP_SKIP_REGISTRY, one layer wider.
ISOLATION_ENV = "BOOTSTRAP_SKIP_SHELL_INTEGRATION"

# Layered-manifest key (read from USER layers only) carrying the two opt-outs.
LAYERED_KEY = "interpreter_env"
DEFAULT_PERSIST = True
DEFAULT_SHELL_HOOK = True

# Per-project record of the resolved project interpreter, under the engine
# data dir; the file name is the same key the SessionStart hook uses for its
# cooldown stamp. The `_global_` key (hook had no hash tool) is never recorded.
RECORD_SUBDIR = "project_python"
GLOBAL_KEY = "_global_"


def shell_path(path: str, *, windows: bool | None = None) -> str:
    """Return ``path`` in the form bash consumes.

    ``windows=None`` follows ``os.name == "nt"``. On Windows every backslash
    becomes a forward slash (``C:/x/python.exe``), which Git Bash accepts and
    which cannot be misread as an escape when an author echoes or interpolates
    the value. Elsewhere the path is returned unchanged.
    """
    if windows is None:
        windows = os.name == "nt"
    return path.replace("\\", "/") if windows else path


def standalone_python(home: str | None = None, *, windows: bool | None = None) -> str:
    """The deterministic bootstrap-owned interpreter path for this OS.

    Mirrors session-bootstrap.sh's WANT_PYTHON: ``<home>/.local/share/
    python-standalone/python/python.exe`` on Windows, ``<home>/.local/bin/
    python3`` elsewhere (the symlink the hook maintains). ``home`` defaults to
    ``path_check._home()`` so a redirected ``$HOME`` is honoured on Windows.
    Pure: builds a string, never touches the filesystem or the environment.
    """
    if windows is None:
        windows = os.name == "nt"
    if home is None:
        from .path_check import _home
        home = _home()
    if windows:
        path = os.path.join(home, ".local", "share", "python-standalone",
                            "python", "python.exe")
    else:
        path = os.path.join(home, ".local", "bin", "python3")
    return shell_path(path, windows=windows)


def project_key(project_dir: str) -> str:
    """sha1 of the raw project-dir string -- identical to the hook's _PROJECT_KEY.

    The hook hashes ``$PWD`` exactly as bash sees it (``printf '%s' "$PWD" |
    sha1sum``) and passes the same string as ``--project-key``; the engine
    hashes only when that flag is absent, and then this string verbatim --
    never a normalized form, or the two keys differ.
    """
    import hashlib
    return hashlib.sha1(project_dir.encode("utf-8")).hexdigest()


def begin_pass() -> str:
    """Reset both names at the start of a pass; return the ``ENGINE_VAR`` value.

    Clears any inherited ``PROJECT_VAR`` (a project interpreter from some other
    process must never leak into this pass) and sets ``ENGINE_VAR`` to this
    interpreter, overriding any inherited value. Touches ``os.environ`` only.
    """
    os.environ.pop(PROJECT_VAR, None)
    value = shell_path(sys.executable)
    os.environ[ENGINE_VAR] = value
    return value


def export_project_python(venv_dir: str) -> str | None:
    """Set ``PROJECT_VAR`` to the interpreter inside ``venv_dir``.

    Returns the exported value, or None (leaving the environment untouched)
    when ``venv_dir`` holds no interpreter. Sets ``os.environ`` and buffers the
    name for ``$CLAUDE_ENV_FILE`` (``session_env.record``); the caller decides
    whether the value is also written to the project record.
    """
    python_bin = venv_check._find_python(venv_dir)
    if not python_bin:
        return None
    value = shell_path(python_bin)
    os.environ[PROJECT_VAR] = value
    session_env.record(PROJECT_VAR, value)
    return value


# ---------------------------------------------------------------------------
# Path normalization (interface-v3 section 1)
# ---------------------------------------------------------------------------

_MSYS_DRIVE_RE = re.compile(r"^/([A-Za-z])(?=/|$)")
_DRIVE_RE = re.compile(r"^([A-Za-z]):")

#: Source reported when the project opted out (layered ``"project_python":
#: false``, or an opt-out record): no project value exists at all.
OPT_OUT = "opt_out"

#: The record file's entire content (one line, trailing newline) for an
#: opted-out project. Never an absolute path, so it cannot collide with a
#: recorded interpreter; the hook prelude compares the line to it verbatim.
RECORD_OPT_OUT = OPT_OUT

#: Sources the engine records per project. ``virtual_env`` is a property of
#: the launching shell and ``engine`` is the fallback, so neither is a fact
#: about the project worth replaying in a throttled session.
RECORD_SOURCES = ("venv", OPT_OUT)

#: Every source ``default_project_python`` can report, highest priority first.
SOURCES = (OPT_OUT, "virtual_env", "record", "venv", "engine")

#: The layered key a project uses to opt out; ``false`` is its only value.
OPT_OUT_KEY = "project_python"

#: Walk-up depth cap (section 1 step 4).
WALK_DEPTH_CAP = 64


def normalize_path(path: str, *, windows: bool | None = None) -> str:
    """The comparison and output form of ``path`` (section 1).

    On Windows: backslashes become ``/``; an MSYS ``/c/...`` prefix becomes
    ``C:/...``; the drive letter is upper-cased; ``X:`` and ``X:/`` both
    become the root ``X:/``; ``.``/``..`` segments and trailing slashes are
    removed. Elsewhere only the ``posixpath.normpath`` step applies (a
    ``/c/...`` path is an ordinary directory on POSIX). Pure string work; the
    filesystem is never consulted.
    """
    if windows is None:
        windows = os.name == "nt"
    if not path:
        return path
    if not windows:
        return posixpath.normpath(path)
    value = path.replace("\\", "/")
    value = _MSYS_DRIVE_RE.sub(lambda m: m.group(1).upper() + ":", value, count=1)
    drive = _DRIVE_RE.match(value)
    if drive:
        rest = value[2:]
        rest = posixpath.normpath("/" + rest) if rest else "/"
        # normpath keeps a leading "//"; a drive path never needs it.
        rest = "/" + rest.lstrip("/")
        return drive.group(1).upper() + ":" + rest
    return posixpath.normpath(value)


def _absolute(path: str, *, windows: bool | None = None) -> str:
    """``normalize_path`` of the absolute form of ``path``.

    Normalizes BEFORE ``abspath`` so an MSYS spelling (``/d/dev``) is read as
    the drive path it names rather than rooted on the current drive.
    """
    if windows is None:
        windows = os.name == "nt"
    first = normalize_path(path, windows=windows)
    if windows and os.name != "nt":
        # Pure-string Windows handling on a POSIX host (tests): no abspath.
        return first
    return normalize_path(os.path.abspath(first), windows=windows)


def _same_path(a: str, b: str, *, windows: bool | None = None) -> bool:
    if windows is None:
        windows = os.name == "nt"
    if windows:
        return a.casefold() == b.casefold()
    return a == b


def _is_root(path: str) -> bool:
    return path == "/" or bool(re.fullmatch(r"[A-Za-z]:/", path))


def _is_executable(path: str) -> bool:
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def _default_home(home: str | None) -> str:
    if home is not None:
        return home
    from .path_check import _home
    return _home()


def is_bootstrap_owned(executable: str, home: str | None = None) -> bool:
    """Whether ``executable`` is the bootstrap-owned standalone interpreter.

    True only when this process is not a venv (``sys.prefix ==
    sys.base_prefix``) AND the normalized realpath of ``executable`` lies
    inside ``<home>/STANDALONE_DIR_REL``. Informational: the engine uses it for
    a verbose note about what it runs under, never to choose the persisted
    value (that is always ``standalone_python()``).
    """
    if sys.prefix != sys.base_prefix:
        return False
    home = _default_home(home)
    try:
        real = os.path.realpath(executable)
        base = os.path.realpath(os.path.join(home, *STANDALONE_DIR_REL.split("/")))
    except (OSError, ValueError):
        return False
    real_n = normalize_path(real)
    base_n = normalize_path(base).rstrip("/")
    if os.name == "nt":
        real_n, base_n = real_n.casefold(), base_n.casefold()
    return real_n.startswith(base_n + "/")


def _record_path(record_dir: str, key: str | None) -> str | None:
    if not record_dir or not key or key == GLOBAL_KEY:
        return None
    if "/" in key or "\\" in key or key in (".", ".."):
        return None
    return os.path.join(record_dir, key)


def _read_record_line(path: str | None) -> str | None:
    """The record's single line, or None (missing, empty, several lines)."""
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            content = handle.read()
    except (OSError, UnicodeDecodeError):
        return None
    if content.endswith("\n"):
        content = content[:-1]
    if content.endswith("\r"):
        content = content[:-1]
    if not content or "\n" in content or "\r" in content:
        return None
    return content


def _read_record_file(path: str | None) -> str | None:
    content = _read_record_line(path)
    if content is None or not _is_executable(content):
        return None
    return content


def _record_says_opt_out(path: str | None) -> bool:
    return _read_record_line(path) == RECORD_OPT_OUT


def _venv_interpreter(venv_dir: str) -> str | None:
    """The executable interpreter inside ``venv_dir``, if any."""
    for rel in (("bin", "python"), ("Scripts", "python.exe")):
        candidate = os.path.join(venv_dir, *rel)
        if _is_executable(candidate):
            return candidate
    return None


def _qualifying_venv(candidate: str) -> str | None:
    """Section 1 step 4: ``pyvenv.cfg`` present AND an executable interpreter."""
    if not os.path.isfile(os.path.join(candidate, "pyvenv.cfg")):
        return None
    return _venv_interpreter(candidate)


def _walk_up(start: str, env, home: str) -> str | None:
    uv_env = env.get("UV_PROJECT_ENVIRONMENT") or ""
    name = ".venv"
    if uv_env:
        if os.path.isabs(uv_env) or _DRIVE_RE.match(uv_env):
            found = _qualifying_venv(_absolute(uv_env))
            if found:
                return found
        else:
            name = uv_env
    home_n = _absolute(home) if home else ""
    current = _absolute(start)
    for _ in range(WALK_DEPTH_CAP):
        found = _qualifying_venv(os.path.join(current, name))
        if found:
            return found
        if (home_n and _same_path(current, home_n)) or _is_root(current):
            return None
        parent = normalize_path(posixpath.dirname(current))
        if _DRIVE_RE.fullmatch(parent):
            parent += "/"
        if parent == current:
            return None
        current = parent
    return None


#: A project layer's opt-out, read the way the terminal resolvers read it
#: (shell/project-python.sh, project-python.ps1): any occurrence of the key
#: set to false.
_OPT_OUT_TEXT_RE = re.compile(r'"project_python"\s*:\s*false(?![A-Za-z0-9_])')
_PROJECT_LAYER_FILES = ("bootstrap.json", "bootstrap.local.json")


def _dir_opts_out(directory: str) -> bool:
    for leaf in _PROJECT_LAYER_FILES:
        try:
            with open(os.path.join(directory, ".claude", leaf), "r",
                      encoding="utf-8", errors="replace") as handle:
                if _OPT_OUT_TEXT_RE.search(handle.read()):
                    return True
        except OSError:
            continue
    return False


def walk_opted_out(start_dir: str, *, env=None, home: str | None = None) -> bool:
    """The terminal form of the opt-out: the nearest decisive directory upward.

    For a caller that knows only a working directory (the ``bootstrap`` CLI;
    the shell and PowerShell resolvers implement the same walk): from
    ``start_dir`` upward, the first directory that either opts out (its
    ``.claude/bootstrap.json`` or ``bootstrap.local.json`` declares
    ``"project_python": false``) or holds a qualifying venv (step 4's
    candidate) decides. The home directory is never read for an opt-out -- its
    ``.claude/bootstrap.json`` is the USER layer -- and the walk stops after
    it, at a root, or after ``WALK_DEPTH_CAP`` directories. The engine instead
    reads the project layers of ``--project-dir`` (``opted_out``).
    """
    env = os.environ if env is None else env
    home = _default_home(home)
    uv_env = env.get("UV_PROJECT_ENVIRONMENT") or ""
    name = ".venv"
    if uv_env and not (os.path.isabs(uv_env) or _DRIVE_RE.match(uv_env)):
        name = uv_env
    home_n = _absolute(home) if home else ""
    current = _absolute(start_dir)
    for _ in range(WALK_DEPTH_CAP):
        at_home = bool(home_n) and _same_path(current, home_n)
        if not at_home and _dir_opts_out(current):
            return True
        if _qualifying_venv(os.path.join(current, name)):
            return False
        if at_home or _is_root(current):
            return False
        parent = normalize_path(posixpath.dirname(current))
        if _DRIVE_RE.fullmatch(parent):
            parent += "/"
        if parent == current:
            return False
        current = parent
    return False


def default_project_python(
    start_dir: str,
    *,
    subdir: str | None = None,
    opted_out: bool = False,
    record_dir: str | None = None,
    key: str | None = None,
    env=None,
    home: str | None = None,
) -> tuple[str | None, str]:
    """Resolve the project interpreter by the normative rule (section 1).

    Returns ``(value, source)``; ``source`` is one of :data:`SOURCES`. The
    value is None exactly when ``source`` is :data:`OPT_OUT`, and never empty
    otherwise. Order:

    1. Opted out -- ``opted_out`` (the layered ``"project_python": false``),
       or an opt-out marker in the record ``<record_dir>/<key>`` -- means no
       project value at all.
    2. ``$VIRTUAL_ENV`` whose ``bin/python`` or ``Scripts/python.exe`` is
       executable.
    3. The record ``<record_dir>/<key>`` naming an executable (never for
       ``GLOBAL_KEY``).
    4. Walk-up from ``start_dir/subdir`` to the nearest qualifying venv.
    5. ``env[ENGINE_VAR]``, else ``standalone_python(home)``.

    Reads the filesystem and ``env`` (``os.environ`` by default); writes
    nothing.
    """
    env = os.environ if env is None else env
    record_path = _record_path(record_dir, key)
    if opted_out or _record_says_opt_out(record_path):
        return None, OPT_OUT
    home = _default_home(home)

    virtual_env = env.get("VIRTUAL_ENV") or ""
    if virtual_env:
        found = _venv_interpreter(_absolute(virtual_env))
        if found:
            return _absolute(found), "virtual_env"

    recorded = _read_record_file(record_path)
    if recorded:
        return _absolute(recorded), "record"
    start = os.path.join(start_dir, subdir) if subdir else start_dir
    found = _walk_up(start, env, home)
    if found:
        return _absolute(found), "venv"

    engine_value = env.get(ENGINE_VAR) or ""
    if engine_value:
        return normalize_path(engine_value), "engine"
    return normalize_path(standalone_python(home)), "engine"


def export_project_default(start_dir: str, **kwargs) -> tuple[str | None, str]:
    """Resolve (``default_project_python``) and export ``PROJECT_VAR``.

    Sets ``os.environ[PROJECT_VAR]`` and buffers the name for
    ``$CLAUDE_ENV_FILE``. For an opted-out project the name is instead
    removed from both (``session_env.forget`` also drops a line an earlier
    writer put in the env file). Takes the same keyword arguments as
    ``default_project_python``; returns its ``(value, source)``.
    """
    value, source = default_project_python(start_dir, **kwargs)
    if value is None:
        os.environ.pop(PROJECT_VAR, None)
        session_env.forget(PROJECT_VAR)
    else:
        os.environ[PROJECT_VAR] = value
        session_env.record(PROJECT_VAR, value)
    return value, source


# ---------------------------------------------------------------------------
# Per-project record file
# ---------------------------------------------------------------------------


def read_record(data_dir: str, key: str | None) -> str | None:
    """The recorded interpreter for ``key``, or None.

    None for ``GLOBAL_KEY``, a missing or empty record, a record holding more
    than one line, the opt-out marker (see ``record_opted_out``), or a value
    that is not an executable file.
    """
    if not data_dir:
        return None
    return _read_record_file(_record_path(os.path.join(data_dir, RECORD_SUBDIR), key))


def record_opted_out(data_dir: str, key: str | None) -> bool:
    """Whether the record for ``key`` is the opt-out marker."""
    if not data_dir:
        return False
    return _record_says_opt_out(
        _record_path(os.path.join(data_dir, RECORD_SUBDIR), key))


def write_record(data_dir: str, key: str | None, value: str | None, source: str) -> bool:
    """Record the project's resolution for ``key`` when the policy allows it.

    The policy: ``source`` in :data:`RECORD_SOURCES` and ``key`` is not
    ``GLOBAL_KEY``. Source ``venv`` records ``value`` (one line); source
    :data:`OPT_OUT` records :data:`RECORD_OPT_OUT` and ignores ``value``.
    Returns True when the record holds that line afterwards (an identical
    record is not rewritten), False when the policy refused or the write
    failed. The file is one line with a trailing newline -- the hook prelude
    reads it with ``read -r``, which reports failure on a final line without
    one.
    """
    if source not in RECORD_SOURCES or not data_dir:
        return False
    line = RECORD_OPT_OUT if source == OPT_OUT else value
    if not line or "\n" in line or "\r" in line:
        return False
    path = _record_path(os.path.join(data_dir, RECORD_SUBDIR), key)
    if not path:
        return False
    content = line + "\n"
    try:
        with open(path, "r", encoding="utf-8", newline="") as handle:
            if handle.read() == content:
                return True
    except (OSError, UnicodeDecodeError):
        pass
    try:
        from .atomic_write import write_atomic
        write_atomic(path, content, newline="\n")
    except OSError:
        return False
    return True


def remove_record(data_dir: str, key: str | None) -> bool:
    """Delete the record for ``key``; True only when a file was removed."""
    if not data_dir:
        return False
    path = _record_path(os.path.join(data_dir, RECORD_SUBDIR), key)
    if not path:
        return False
    try:
        os.remove(path)
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------
# Layered-manifest settings
# ---------------------------------------------------------------------------


def _opt_out_value(layered_manifest):
    """``(present, value)`` of the last ``project_python`` in the layers."""
    layers = layered_manifest
    if isinstance(layers, dict):
        layers = [layers]
    present, value = False, None
    for layer in layers or ():
        if isinstance(layer, dict) and OPT_OUT_KEY in layer:
            present, value = True, layer[OPT_OUT_KEY]
    return present, value


def project_python_opted_out(layered_manifest) -> bool:
    """Whether the project opted out of automatic project-interpreter resolution.

    True only for ``"project_python": false``. Accepts the MERGED layered
    manifest (a dict) or a list of layer dicts, lowest priority first (the
    last layer that sets the key wins); the key normally lives in
    ``<project>/.claude/bootstrap.json``. Any other value is treated as absent
    -- see ``project_python_note``.
    """
    present, value = _opt_out_value(layered_manifest)
    return present and value is False


def project_python_note(layered_manifest) -> str | None:
    """The one descriptive log line for an unusable ``project_python`` value.

    None when the key is absent or ``false``.
    """
    present, value = _opt_out_value(layered_manifest)
    if not present or value is False:
        return None
    return (
        f"python: {OPT_OUT_KEY} accepts only false (opt out of "
        f"{PROJECT_VAR}); {type(value).__name__} value ignored")


def interpreter_env_settings(
    user_layers,
    *,
    parse_errors: bool,
    project_layers=None,
) -> tuple[bool, bool, list[str]]:
    """``(persist, shell_hook, notes)`` from the ``interpreter_env`` key.

    Reads ``LAYERED_KEY`` from ``user_layers`` only (``~/.claude/
    bootstrap.json``, ``bootstrap.local.json``, and the bodies of the selected
    profile chain declared there), lowest priority first; a later layer's
    scalar wins. A ``project_layers`` entry that sets the key is ignored with a
    note: the two opt-outs are machine-wide and must not be decided by a
    repository. ``parse_errors=True`` returns ``(False, False, [note])`` so a
    broken layer that might hold an opt-out is never acted on as if it were
    absent -- the engine skips the whole step on that signal. Empty or None
    layers yield the defaults.
    """
    if parse_errors:
        return False, False, [
            f"python: {LAYERED_KEY} unknown (a layered manifest failed to parse) "
            "-- persistence and shell hook skipped this pass"]
    persist, shell_hook = DEFAULT_PERSIST, DEFAULT_SHELL_HOOK
    notes: list[str] = []
    for layer in user_layers or ():
        if not isinstance(layer, dict) or LAYERED_KEY not in layer:
            continue
        block = layer[LAYERED_KEY]
        if not isinstance(block, dict):
            notes.append(f"python: {LAYERED_KEY} must be an object -- ignored")
            continue
        for field, value in block.items():
            if field not in ("persist", "shell_hook"):
                notes.append(f"python: {LAYERED_KEY}.{field} is not a known key -- ignored")
            elif not isinstance(value, bool):
                notes.append(f"python: {LAYERED_KEY}.{field} must be true or false -- ignored")
            elif field == "persist":
                persist = value
            else:
                shell_hook = value
    if any(isinstance(layer, dict) and LAYERED_KEY in layer
           for layer in project_layers or ()):
        notes.append(
            f"python: {LAYERED_KEY} in a project manifest is ignored -- set it in "
            "~/.claude/bootstrap.json")
    return persist, shell_hook, notes
