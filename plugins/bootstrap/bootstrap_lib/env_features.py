"""The five declarative env.json feature sections (E1 step 4).

Check/fix primitives for the engine's env phase handlers (engine.py
``_ENV_PHASES``): ``symlinks``, ``shell_rc`` (ensure + forbid),
``macos_defaults``, ``macos_hotkeys``, ``login_items`` -- the
bootstrap-env-refactor spec, sections 3.1/4.3. Personal data rides as entry
configuration; these functions implement each mechanism exactly once.

Semantics replicate env-config's implementations (config_manager.py,
action_executor.py, check_runner.py, checks.yaml), translated to the
engine idiom (env_var_check.py's shape): every feature is a check -> fix ->
authoritative re-check pair; checks are unprivileged and side-effect free;
fixes are idempotent (a second pass performs no writes). The engine
handlers own filtering (``entry_applies``), failure records, and the
macOS-only gating of the three macOS sections.

PATH is deliberately out of scope for every function here: PATH edits
belong exclusively to bootstrap ``path_entries`` (spec directive 3).
"""

import os
import plistlib
import re
import subprocess
import tempfile
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from .result import Result

HOTKEYS_DOMAIN = "com.apple.symbolichotkeys"


def _last_output_line(proc: "subprocess.CompletedProcess") -> str:
    """The last non-empty stderr/stdout line -- the descriptive-error tail."""
    for stream in (proc.stderr, proc.stdout):
        if not stream:
            continue
        text = stream.decode() if isinstance(stream, bytes) else stream
        lines = [ln for ln in text.strip().splitlines() if ln.strip()]
        if lines:
            return lines[-1].strip()
    return f"exit code {proc.returncode}"


# ---------------------------------------------------------------------------
# Path expansion
# ---------------------------------------------------------------------------

def expand_env_path(path: str) -> str:
    """Expand ``$VARS`` and ``~`` in a manifest path (spec 4.3).

    Manifest paths are ~-anchored except where an entry genuinely targets a
    variable-rooted location ($DEVROOT); bootstrap's env_vars phase has
    already exported those into the live process by the time the env phase
    runs. An unresolved variable is an error -- personalization refuses to
    guess.
    """
    expanded = os.path.expanduser(os.path.expandvars(path))
    if "$" in expanded:
        raise ValueError(
            f"unresolved variable in path {path!r} (expanded to {expanded!r});"
            " declare it via bootstrap.json env_vars"
        )
    return expanded


# ---------------------------------------------------------------------------
# symlinks (spec 3.1 feature 1)
# ---------------------------------------------------------------------------

def check_symlink(source: str, target: str) -> Result:
    """Target must be a symlink resolving to source, and source must exist.

    A dangling link "pointing at" a missing source fails here too: the
    manifest references a tracked file that is not on disk, which is a
    manifest/content error to surface, not a state to accept.
    """
    if not os.path.exists(source):
        return Result(
            passed=False, subject=target,
            message=f"source does not exist: {source}",
        )
    if not os.path.lexists(target):
        return Result(
            passed=False, subject=target,
            message=f"missing: {target}",
        )
    if not os.path.islink(target):
        return Result(
            passed=False, subject=target,
            message=f"exists but is not a symlink: {target}",
        )
    actual = os.path.realpath(target)
    wanted = os.path.realpath(source)
    if actual != wanted:
        return Result(
            passed=False, subject=target,
            message=f"points at {actual}, want {wanted}",
        )
    return Result(
        passed=True, subject=target,
        message=f"{target} -> {source}",
    )


def fix_symlink(source: str, target: str, backup: bool) -> Tuple[bool, str]:
    """Make target a symlink to source (env-config ConfigLinkManager semantics).

    A real file at target is preserved as a timestamped ``.backup_<ts>``
    sibling when ``backup`` is true, else removed. An existing symlink
    (wrong or dangling) is replaced without backup -- a link carries no
    content worth keeping. A directory at target is never replaced.
    """
    if not os.path.exists(source):
        return False, f"source does not exist: {source}"

    try:
        parent = os.path.dirname(target)
        if parent:
            os.makedirs(parent, exist_ok=True)

        backed_up = None
        if os.path.lexists(target):
            if os.path.islink(target):
                os.unlink(target)
            elif os.path.isdir(target):
                return False, f"target is a directory, refusing to replace: {target}"
            elif backup:
                backed_up = f"{target}.backup_{datetime.now():%Y%m%d_%H%M%S}"
                os.replace(target, backed_up)
            else:
                os.unlink(target)

        os.symlink(source, target)
    except OSError as e:
        return False, f"failed to create symlink {target} -> {source}: {e}"

    msg = f"linked {target} -> {source}"
    if backed_up:
        msg += f" (existing file backed up to {backed_up})"
    return True, msg


# ---------------------------------------------------------------------------
# shell_rc (spec 3.1 feature 2): ensure + forbid
# ---------------------------------------------------------------------------

# SHELL_NAME placeholder rendering per rc file, so
# `starship init SHELL_NAME` becomes `starship init bash`/`zsh`
# (env-config append_to_shell_config).
_SHELL_FOR = {".bashrc": "bash", ".zshrc": "zsh"}


def shell_rc_candidates() -> List[str]:
    """The shell rc files the feature manages, in a stable order."""
    home = os.path.expanduser("~")
    return [os.path.join(home, ".bashrc"), os.path.join(home, ".zshrc")]


def _existing_rc_files() -> List[str]:
    return [p for p in shell_rc_candidates() if os.path.isfile(p)]


def render_shell_content(content: str, rc_path: str) -> str:
    """Render an ensure block for a specific rc file (SHELL_NAME substitution)."""
    shell_name = _SHELL_FOR.get(os.path.basename(rc_path), "bash")
    return content.strip().replace("SHELL_NAME", shell_name)


def check_shell_ensure(name: str, content: str) -> Result:
    """The rendered block must be present in EVERY existing rc file.

    (The fix appends to every existing rc file, so its postcondition is the
    honest check; env-config's grep -q any-file diagnosis was weaker than
    its own fix.) No rc file at all = a fresh machine = check fails; the
    fix creates the platform default.
    """
    existing = _existing_rc_files()
    if not existing:
        return Result(
            passed=False, subject=name,
            message="no shell rc file exists (~/.bashrc or ~/.zshrc)",
        )
    missing = []
    for rc in existing:
        rendered = render_shell_content(content, rc)
        try:
            with open(rc) as f:
                text = f.read()
        except OSError as e:
            return Result(
                passed=False, subject=name, message=f"cannot read {rc}: {e}",
            )
        if rendered not in text:
            missing.append(os.path.basename(rc))
    if missing:
        return Result(
            passed=False, subject=name,
            message=f"block missing from {', '.join(missing)}",
        )
    return Result(
        passed=True, subject=name,
        message=f"present in {', '.join(os.path.basename(p) for p in existing)}",
    )


def fix_shell_ensure(content: str, current_os: str) -> Tuple[bool, str]:
    """Append the rendered block to every existing rc file where absent.

    On a fresh machine (no rc file), create the platform-default rc first --
    ~/.zshrc on macOS, ~/.bashrc elsewhere (env-config behavior). The block
    is only ever appended when absent, so a block never appears twice.
    """
    candidates = shell_rc_candidates()
    existing = _existing_rc_files()
    created = None
    if not existing:
        default_rc = candidates[1] if current_os == "macos" else candidates[0]
        try:
            with open(default_rc, "w"):
                pass
        except OSError as e:
            return False, f"cannot create {default_rc}: {e}"
        created = os.path.basename(default_rc)
        existing = [default_rc]

    appended = []
    for rc in existing:
        rendered = render_shell_content(content, rc)
        try:
            with open(rc) as f:
                text = f.read()
            if rendered in text:
                continue
            with open(rc, "a") as f:
                f.write(f"\n{rendered}\n")
        except OSError as e:
            return False, f"cannot write {rc}: {e}"
        appended.append(os.path.basename(rc))

    parts = []
    if created:
        parts.append(f"created {created}")
    if appended:
        parts.append(f"appended to {', '.join(appended)}")
    return True, "; ".join(parts) if parts else "already present"


def check_shell_forbid(name: str, pattern: str) -> Result:
    """The pattern must not match any line of any existing rc file.

    The pattern carries its own comment-exclusion (e.g. the TERM entry's
    ``^\\s*(export\\s+)?TERM=`` cannot match a ``#``-prefixed line), so a
    match = an uncommented violation. No rc file = trivially clean.
    """
    rx = re.compile(pattern)
    offending = []
    for rc in _existing_rc_files():
        try:
            with open(rc) as f:
                lines = f.read().splitlines()
        except OSError as e:
            return Result(
                passed=False, subject=name, message=f"cannot read {rc}: {e}",
            )
        for i, line in enumerate(lines, start=1):
            if rx.search(line):
                offending.append(f"{os.path.basename(rc)}:{i}")
    if offending:
        return Result(
            passed=False, subject=name,
            message=f"forbidden pattern matches {', '.join(offending)}",
        )
    return Result(
        passed=True, subject=name, message="no match in shell rc files",
    )


def fix_shell_forbid(pattern: str) -> Tuple[bool, str]:
    """Comment out every matching line (``# `` prefix) in every rc file.

    env-config comment_term_overrides semantics: matching lines are
    preserved as comments, never deleted. Files are rewritten only when a
    line actually changed.
    """
    rx = re.compile(pattern)
    commented = []
    for rc in _existing_rc_files():
        try:
            with open(rc) as f:
                lines = f.read().splitlines(keepends=True)
            changed = 0
            new_lines = []
            for line in lines:
                if rx.search(line):
                    new_lines.append(f"# {line}")
                    changed += 1
                else:
                    new_lines.append(line)
            if changed:
                with open(rc, "w") as f:
                    f.write("".join(new_lines))
                commented.append(f"{changed} line(s) in {os.path.basename(rc)}")
        except OSError as e:
            return False, f"cannot rewrite {rc}: {e}"
    if commented:
        return True, f"commented out {', '.join(commented)}"
    return True, "nothing to comment out"


# ---------------------------------------------------------------------------
# macos_defaults (spec 3.1 feature 3)
# ---------------------------------------------------------------------------

def defaults_expected_string(value) -> Optional[str]:
    """The canonical `defaults read` output for a manifest value.

    Supported value types are bool/int/string (env-config
    set_macos_defaults). Returns None for anything else (invalid entry).
    """
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return value
    return None


def check_macos_default(domain: str, key: str, value) -> Result:
    """`defaults read <domain> <key>` must equal the manifest value."""
    subject = f"{domain}.{key}"
    proc = subprocess.run(
        ["defaults", "read", domain, key], capture_output=True, text=True,
    )
    want = defaults_expected_string(value)
    if proc.returncode != 0:
        return Result(
            passed=False, subject=subject,
            message=f"not set (want {want!r})",
        )
    current = proc.stdout.strip()
    if current == want:
        return Result(passed=True, subject=subject, message=f"= {current!r}")
    return Result(
        passed=False, subject=subject,
        message=f"is {current!r}, want {want!r}",
    )


def _defaults_write_args(value) -> List[str]:
    if isinstance(value, bool):
        return ["-bool", "true" if value else "false"]
    if isinstance(value, int):
        return ["-int", str(value)]
    return ["-string", str(value)]


def fix_macos_default(domain: str, key: str, value) -> Tuple[bool, str]:
    """`defaults write <domain> <key>` with the typed flag (env-config)."""
    proc = subprocess.run(
        ["defaults", "write", domain, key, *_defaults_write_args(value)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return False, f"defaults write failed: {_last_output_line(proc)}"
    return True, f"set to {defaults_expected_string(value)!r}"


def flush_macos_defaults_cache() -> None:
    """The standard preference-cache flush after `defaults write` (spec 3.1).

    Best-effort by design: the writes themselves are already committed
    (and re-checked); the flush only nudges running apps to pick them up.
    """
    for proc_name in ("cfprefsd", "SystemUIServer"):
        subprocess.run(["killall", proc_name], capture_output=True)


# ---------------------------------------------------------------------------
# macos_hotkeys (spec 3.1 feature 4)
# ---------------------------------------------------------------------------

def read_symbolic_hotkeys() -> Tuple[Optional[Dict], str]:
    """Export and parse the symbolic-hotkeys plist.

    Returns ``(plist_dict, "")`` or ``(None, error)``. Side-effect free --
    this is the check side (env-config check_macos_keyboard_shortcuts).
    """
    try:
        proc = subprocess.run(
            ["defaults", "export", HOTKEYS_DOMAIN, "-"], capture_output=True,
        )
        if proc.returncode != 0:
            return None, (
                f"defaults export {HOTKEYS_DOMAIN} failed: "
                f"{_last_output_line(proc)}"
            )
        data = plistlib.loads(proc.stdout)
    except (OSError, plistlib.InvalidFileException, ValueError) as e:
        return None, f"failed to read symbolic hotkeys: {e}"
    if "AppleSymbolicHotKeys" not in data:
        return None, f"no AppleSymbolicHotKeys dict in {HOTKEYS_DOMAIN}"
    return data, ""


def hotkey_state(data: Dict, hid, parameters, enabled) -> Tuple[str, str]:
    """Compare one hotkey entry against the exported plist.

    Returns ``(status, detail)`` with status one of ``"ok"``,
    ``"mismatch"``, ``"missing"``. Plist keys are strings; ``enabled``
    compares as bool (the plist stores 0/1). A missing id is its own state:
    the fix only mutates existing hotkey slots (env-config behavior) and
    must fail descriptively rather than fabricate one.
    """
    hotkeys = data.get("AppleSymbolicHotKeys") or {}
    current = hotkeys.get(str(hid))
    if current is None:
        return "missing", f"id {hid} not present in {HOTKEYS_DOMAIN}"
    got_params = list(current.get("value", {}).get("parameters", []))
    got_enabled = bool(current.get("enabled", False))
    want_params = list(parameters)
    want_enabled = bool(enabled)
    if got_params == want_params and got_enabled == want_enabled:
        return "ok", f"parameters {got_params}, enabled {got_enabled}"
    return "mismatch", (
        f"want parameters {want_params} enabled {want_enabled}, "
        f"got parameters {got_params} enabled {got_enabled}"
    )


def apply_symbolic_hotkeys(data: Dict, entries: List[Dict]) -> Tuple[bool, str]:
    """Mutate the exported plist for ``entries`` and import it back.

    One export/import round-trip for the whole batch (env-config
    apply_macos_keyboard_shortcuts), followed by the cache flush + process
    restarts. Every entry's id must already exist in the plist (the
    handler pre-filters missing ids into failures).
    """
    hotkeys = data["AppleSymbolicHotKeys"]
    for entry in entries:
        slot = hotkeys[str(entry["id"])]
        slot.setdefault("value", {})["parameters"] = list(entry["parameters"])
        slot["enabled"] = bool(entry.get("enabled", True))

    tmpfile = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".plist", delete=False, mode="wb",
        ) as f:
            plistlib.dump(data, f, fmt=plistlib.FMT_XML)
            tmpfile = f.name
        proc = subprocess.run(
            ["defaults", "import", HOTKEYS_DOMAIN, tmpfile],
            capture_output=True,
        )
        if proc.returncode != 0:
            return False, f"defaults import failed: {_last_output_line(proc)}"
    except OSError as e:
        return False, f"failed to apply symbolic hotkeys: {e}"
    finally:
        if tmpfile and os.path.exists(tmpfile):
            os.unlink(tmpfile)

    _flush_hotkey_caches()
    return True, f"applied {len(entries)} hotkey remap(s)"


def _flush_hotkey_caches() -> None:
    """Flush preference cache + restart shortcut-handling processes (best-effort)."""
    for proc_name in ("cfprefsd", "screencaptureui", "SystemUIServer"):
        subprocess.run(["killall", proc_name], capture_output=True)
    subprocess.run(
        ["/System/Library/PrivateFrameworks/SystemAdministration.framework"
         "/Resources/activateSettings", "-u"],
        capture_output=True,
    )


# ---------------------------------------------------------------------------
# login_items (spec 3.1 feature 5)
# ---------------------------------------------------------------------------

def list_login_items() -> Tuple[Optional[List[str]], str]:
    """The names of every login item, via System Events.

    Returns ``(names, "")`` or ``(None, error)``.
    """
    proc = subprocess.run(
        ["osascript", "-e",
         'tell application "System Events" to get the name of every login item'],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return None, f"osascript login-item query failed: {_last_output_line(proc)}"
    out = proc.stdout.strip()
    if not out:
        return [], ""
    return [item.strip() for item in out.split(",")], ""


def check_login_item(name: str) -> Result:
    items, err = list_login_items()
    if items is None:
        return Result(passed=False, subject=name, message=err)
    if name in items:
        return Result(
            passed=True, subject=name, message="registered as a login item",
        )
    return Result(
        passed=False, subject=name,
        message=f"not a login item (current: {', '.join(items) or 'none'})",
    )


def add_login_item(path: str, hidden: bool) -> Tuple[bool, str]:
    """Register an app as a login item via System Events (env-config fix)."""
    hidden_s = "true" if hidden else "false"
    script = (
        'tell application "System Events" to make login item at end with '
        f'properties {{path:"{path}", hidden:{hidden_s}}}'
    )
    proc = subprocess.run(
        ["osascript", "-e", script], capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return False, f"osascript make login item failed: {_last_output_line(proc)}"
    return True, f"added login item ({path}, hidden {hidden_s})"
