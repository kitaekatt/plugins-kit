"""Terminal shell integration for ``BOOTSTRAP_PROJECT_PYTHON`` (interface-v3, section 5).

``ensure`` keeps three things in step with the ``shell_hook`` setting:

* the two resolver templates (``shell/project-python.sh`` and
  ``shell/project-python.ps1``) copied into ``<data_dir>/shell/``;
* one marked line in ``<home>/.bashrc`` (and ``<home>/.zshrc`` on macOS)
  that sources the shell template from its CANONICAL location
  (``$HOME/.claude/plugins/data/<mkt>/bootstrap/shell/``), never from a
  redirected data root;
* on Windows, one marked line in each EXISTING PowerShell ``profile.ps1``
  (``<Documents>/WindowsPowerShell`` and ``<Documents>/PowerShell``).

PowerShell profiles are never created. A profile is touched only when it
exists, carries no Authenticode signature block, uses a recognised encoding
(UTF-8 without BOM, UTF-8 with BOM, UTF-16LE with BOM -- preserved on write),
and the effective execution policy of its PowerShell edition lets it run
scripts (RemoteSigned, Unrestricted, Bypass). Everything else is skipped with a
verbose note. The effective policy is read from the registry and the
PowerShell 7 config files -- never by spawning PowerShell.

Disabling (``enabled=False``) removes every line carrying ``MARKER`` from the
same files and leaves every other line intact (a file that ended with a
newline before the line was added is byte-identical after the removal).

``ensure`` is inert (one verbose entry, no reads, no writes) when
``interpreter_env.ISOLATION_ENV`` or ``CLAUDE_BOOTSTRAP_DATA_ROOT`` is set:
the first is the test-suite isolation signal, the second marks a session whose
data tree is not the canonical one the rc line names. ``home`` and
``documents`` are injectable; callers pass ``path_check._home()`` because
``env_var_check._rc_files`` ignores a redirected ``$HOME`` on Windows.

Stdlib and sibling imports only.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from .atomic_write import write_atomic
from .interpreter_env import ISOLATION_ENV

MARKER = "# Added by bootstrap (project-python)"
SH_TEMPLATE = "project-python.sh"
PS_TEMPLATE = "project-python.ps1"
TEMPLATE_SUBDIR = "shell"
TEMPLATE_DIR = Path(__file__).resolve().parent.parent / TEMPLATE_SUBDIR
DATA_ROOT_ENV = "CLAUDE_BOOTSTRAP_DATA_ROOT"
SIGNATURE_MARK = "# SIG # Begin signature block"

# Policies under which a local, unsigned profile and the dot-sourced template run.
ALLOWING_POLICIES = frozenset({"remotesigned", "unrestricted", "bypass"})

# PowerShell editions: profile directory under Documents -> policy sources.
WINDOWS_POWERSHELL = "WindowsPowerShell"
POWERSHELL_CORE = "PowerShell"
_EDITIONS = (WINDOWS_POWERSHELL, POWERSHELL_CORE)
_POLICY_KEYS = {
    WINDOWS_POWERSHELL: (
        r"SOFTWARE\Policies\Microsoft\Windows\PowerShell",
        r"SOFTWARE\Microsoft\PowerShell\1\ShellIds\Microsoft.PowerShell",
    ),
    POWERSHELL_CORE: (
        r"SOFTWARE\Policies\Microsoft\PowerShellCore",
        None,
    ),
}
_PWSH_CONFIG = "powershell.config.json"
_PWSH_POLICY_KEY = "Microsoft.PowerShell:ExecutionPolicy"

_MARKETPLACE_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def rc_line(marketplace: str) -> str:
    """The marked bash/zsh rc line for ``marketplace`` (no trailing newline)."""
    path = f"$HOME/.claude/plugins/data/{marketplace}/bootstrap/shell/{SH_TEMPLATE}"
    return f'[ -f "{path}" ] && . "{path}"  {MARKER}'


def profile_line(marketplace: str) -> str:
    """The marked PowerShell profile line for ``marketplace`` (no newline)."""
    path = f"$HOME\\.claude\\plugins\\data\\{marketplace}\\bootstrap\\shell\\{PS_TEMPLATE}"
    return f'if (Test-Path "{path}") {{ . "{path}" }}  {MARKER}'


def rc_targets(home: str, current_os: str) -> list[str]:
    """``<home>/.bashrc``, plus ``<home>/.zshrc`` on macOS."""
    targets = [os.path.join(home, ".bashrc")]
    if current_os == "macos":
        targets.append(os.path.join(home, ".zshrc"))
    return targets


def _skip_registry() -> bool:
    return sys.platform != "win32" or bool(os.environ.get("BOOTSTRAP_SKIP_REGISTRY"))


def documents_dir(home: str, documents: str | None = None) -> str:
    """The Documents directory holding the PowerShell profile directories.

    An injected ``documents`` wins. Otherwise, on Windows (unless
    ``BOOTSTRAP_SKIP_REGISTRY`` is set) the expanded
    ``User Shell Folders\\Personal`` registry value, which follows a
    OneDrive-redirected Documents; else ``<home>/Documents``.
    """
    if documents:
        return documents
    if not _skip_registry():
        try:
            import winreg
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
            ) as key:
                value, _kind = winreg.QueryValueEx(key, "Personal")
            if value:
                return os.path.expandvars(value)
        except OSError:
            pass
    return os.path.join(home, "Documents")


def profile_paths(documents: str) -> list[tuple[str, str]]:
    """``[(profile_path, edition), ...]`` for both PowerShell editions."""
    return [(os.path.join(documents, edition, "profile.ps1"), edition)
            for edition in _EDITIONS]


def _registry_policy(edition: str) -> str | None:
    """The first defined policy from the registry scopes, in precedence order.

    MachinePolicy, UserPolicy (Group Policy: ``EnableScripts`` 0 means
    Restricted), then for Windows PowerShell CurrentUser and LocalMachine.
    None when nothing is defined or the registry is not consulted.
    """
    if _skip_registry():
        return None
    try:
        import winreg
    except ImportError:
        return None
    policy_key, shell_key = _POLICY_KEYS[edition]
    hives = (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER)
    for hive in hives:
        try:
            with winreg.OpenKey(hive, policy_key) as key:
                try:
                    enabled, _kind = winreg.QueryValueEx(key, "EnableScripts")
                except OSError:
                    enabled = None
                if enabled is not None:
                    if not enabled:
                        return "Restricted"
                    value, _kind = winreg.QueryValueEx(key, "ExecutionPolicy")
                    if value:
                        return str(value)
        except OSError:
            continue
    if shell_key is None:
        return None
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, shell_key) as key:
                value, _kind = winreg.QueryValueEx(key, "ExecutionPolicy")
        except OSError:
            continue
        if value and str(value).lower() != "undefined":
            return str(value)
    return None


def _config_policy(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    value = data.get(_PWSH_POLICY_KEY) if isinstance(data, dict) else None
    if isinstance(value, str) and value and value.lower() != "undefined":
        return value
    return None


def _default_policy(edition: str) -> str:
    """The built-in policy: RemoteSigned for PowerShell 7 and Windows Server,
    Restricted for Windows PowerShell on a client edition."""
    if edition == POWERSHELL_CORE:
        return "RemoteSigned"
    try:
        if sys.getwindowsversion().product_type != 1:  # type: ignore[attr-defined]
            return "RemoteSigned"
    except AttributeError:
        pass
    return "Restricted"


def effective_policy(edition: str, documents: str) -> str:
    """The execution policy a new ``edition`` window applies to profiles."""
    policy = _registry_policy(edition)
    if policy:
        return policy
    if edition == POWERSHELL_CORE:
        policy = _config_policy(os.path.join(documents, POWERSHELL_CORE, _PWSH_CONFIG))
        if policy:
            return policy
        if not _skip_registry():
            program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
            policy = _config_policy(
                os.path.join(program_files, "PowerShell", "7", _PWSH_CONFIG))
            if policy:
                return policy
    return _default_policy(edition)


def _sniff_encoding(data: bytes) -> tuple[str, bytes] | None:
    """``(codec, bom)`` for a recognised profile encoding, else None."""
    if data.startswith(b"\xef\xbb\xbf"):
        return "utf-8", b"\xef\xbb\xbf"
    if data.startswith(b"\xff\xfe\x00\x00"):
        return None  # UTF-32LE
    if data.startswith(b"\xff\xfe"):
        return "utf-16-le", b"\xff\xfe"
    if data.startswith((b"\xfe\xff", b"\x00\x00\xfe\xff")):
        return None  # UTF-16BE / UTF-32BE
    if b"\x00" in data:
        return None  # BOM-less UTF-16 or binary
    return "utf-8", b""


def _is_marked(line: str) -> bool:
    return line.rstrip().endswith(MARKER)


def _edit_lines(text: str, wanted: str | None, newline: str) -> str | None:
    """New text with every marked line removed and ``wanted`` appended.

    Returns None when ``text`` already holds exactly one marked line equal to
    ``wanted`` (or none when ``wanted`` is None).
    """
    lines = text.splitlines(keepends=True)
    marked = [ln for ln in lines if _is_marked(ln)]
    if wanted is None:
        if not marked:
            return None
    elif len(marked) == 1 and marked[0].rstrip("\r\n") == wanted:
        return None
    kept = "".join(ln for ln in lines if not _is_marked(ln))
    if wanted is None:
        return kept
    if kept and not kept.endswith(("\n", "\r")):
        kept += newline
    return kept + wanted + newline


def _failure(message: str) -> dict:
    return {
        "type": "shell_hook",
        "message": message,
        "agent_msg": (
            f"Bootstrap could not update terminal shell integration: {message}. "
            "Check the file's permissions; BOOTSTRAP_PYTHON and "
            "BOOTSTRAP_PROJECT_PYTHON still reach Claude sessions without it."
        ),
        "plugin": "bootstrap",
    }


def _sync_templates(data_dir: str, action_entries: list, ok_entries: list,
                    failures: list) -> None:
    dest_dir = os.path.join(data_dir, TEMPLATE_SUBDIR)
    changed = []
    for name in (SH_TEMPLATE, PS_TEMPLATE):
        src = TEMPLATE_DIR / name
        dst = os.path.join(dest_dir, name)
        try:
            source = src.read_bytes()
            content = source.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            action_entries.append(f"shell: template {name}: FAILED - {exc}")
            failures.append(_failure(f"template {name} unreadable ({exc})"))
            continue
        try:
            with open(dst, "rb") as fh:
                current = fh.read()
        except OSError:
            current = None
        if current == source:
            continue
        try:
            write_atomic(dst, content, newline="\n")
        except OSError as exc:
            action_entries.append(f"shell: template {name}: FAILED - {exc}")
            failures.append(_failure(f"could not write {dst} ({exc})"))
            continue
        changed.append(name)
    if changed:
        action_entries.append(
            f"shell: project-python templates updated ({', '.join(changed)})")
    else:
        ok_entries.append("shell: project-python templates ok")


def _sync_rc(path: str, wanted: str | None, action_entries: list,
             ok_entries: list, failures: list) -> None:
    label = os.path.basename(path)
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except FileNotFoundError:
        data = None
    except OSError as exc:
        action_entries.append(f"shell: {label}: FAILED - {exc}")
        failures.append(_failure(f"could not read {path} ({exc})"))
        return
    if data is None and wanted is None:
        ok_entries.append(f"shell: {label} has no project-python hook")
        return
    text = (data or b"").decode("utf-8", errors="surrogateescape")
    new = _edit_lines(text, wanted, "\n")
    if new is None:
        ok_entries.append(
            f"shell: {label} project-python hook "
            f"{'ok' if wanted else 'absent'}")
        return
    try:
        # In place, not replace: an rc file is often a symlink into a dotfiles
        # repository, and os.replace would swap the link for a plain file.
        with open(path, "wb") as fh:
            fh.write(new.encode("utf-8", errors="surrogateescape"))
    except OSError as exc:
        action_entries.append(f"shell: {label}: FAILED - {exc}")
        failures.append(_failure(f"could not write {path} ({exc})"))
        return
    verb = "added to" if wanted else "removed from"
    action_entries.append(f"shell: project-python hook {verb} {label}")


def _sync_profile(path: str, edition: str, documents: str, wanted: str | None,
                  action_entries: list, ok_entries: list,
                  failures: list) -> None:
    label = f"{edition}/profile.ps1"
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except FileNotFoundError:
        ok_entries.append(f"shell: {label} absent - not created")
        return
    except OSError as exc:
        ok_entries.append(f"shell: {label} unreadable - skipped ({exc})")
        return
    sniffed = _sniff_encoding(data)
    if sniffed is None:
        ok_entries.append(f"shell: {label} has an unrecognised encoding - skipped")
        return
    codec, bom = sniffed
    try:
        text = data[len(bom):].decode(codec)
    except UnicodeDecodeError:
        ok_entries.append(f"shell: {label} has an unrecognised encoding - skipped")
        return
    if SIGNATURE_MARK in text:
        ok_entries.append(f"shell: {label} is signed - skipped")
        return
    if wanted is not None:
        policy = effective_policy(edition, documents)
        if policy.lower() not in ALLOWING_POLICIES:
            ok_entries.append(
                f"shell: {label} skipped - execution policy {policy} does not run scripts")
            return
    newline = "\r\n" if ("\r\n" in text or "\n" not in text) else "\n"
    new = _edit_lines(text, wanted, newline)
    if new is None:
        ok_entries.append(
            f"shell: {label} project-python hook {'ok' if wanted else 'absent'}")
        return
    try:
        with open(path, "wb") as fh:
            fh.write(bom + new.encode(codec))
    except OSError as exc:
        action_entries.append(f"shell: {label}: FAILED - {exc}")
        failures.append(_failure(f"could not write {path} ({exc})"))
        return
    verb = "added to" if wanted else "removed from"
    action_entries.append(f"shell: project-python hook {verb} {label}")


def ensure(data_dir: str, current_os: str, *, enabled: bool, home: str,
           documents: str | None = None) -> tuple[list, list, list]:
    """Converge terminal shell integration; return ``(action_entries, ok_entries, failures)``.

    ``data_dir`` is the bootstrap plugin data dir
    (``<root>/<marketplace>/bootstrap``); its parent's name is the marketplace
    the rc and profile lines name. ``current_os`` is ``platform_detect``'s
    value ("windows", "macos", "ubuntu"). See the module docstring.
    """
    action_entries: list = []
    ok_entries: list = []
    failures: list = []

    if os.environ.get(ISOLATION_ENV):
        ok_entries.append(f"shell: project-python integration skipped ({ISOLATION_ENV} set)")
        return action_entries, ok_entries, failures
    if os.environ.get(DATA_ROOT_ENV):
        ok_entries.append(
            f"shell: project-python integration skipped ({DATA_ROOT_ENV} set)")
        return action_entries, ok_entries, failures

    marketplace = os.path.basename(os.path.dirname(os.path.normpath(data_dir)))
    if not _MARKETPLACE_RE.match(marketplace):
        ok_entries.append(
            f"shell: project-python integration skipped (marketplace name "
            f"{marketplace!r} is not a plain name)")
        return action_entries, ok_entries, failures

    if enabled:
        _sync_templates(data_dir, action_entries, ok_entries, failures)

    wanted_rc = rc_line(marketplace) if enabled else None
    for path in rc_targets(home, current_os):
        _sync_rc(path, wanted_rc, action_entries, ok_entries, failures)

    if current_os == "windows":
        docs = documents_dir(home, documents)
        wanted_ps = profile_line(marketplace) if enabled else None
        for path, edition in profile_paths(docs):
            _sync_profile(path, edition, docs, wanted_ps,
                          action_entries, ok_entries, failures)

    return action_entries, ok_entries, failures
