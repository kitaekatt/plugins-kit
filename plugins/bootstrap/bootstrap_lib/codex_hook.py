"""Codex project-hook installation and SessionStart preflight helpers.

The Claude bootstrap pass owns the small project-local adapter.  The adapter is
ephemeral, so it belongs under ``.codex/`` and is expected to be ignored by the
project's source-control rules.  The hook itself invokes the stable
``bootstrap codex-hook`` lever; it never embeds a versioned cache path.
"""

from __future__ import annotations

import copy
from contextlib import contextmanager
import json
import os
import shlex
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass


CODEX_DIRNAME = ".codex"
HOOKS_FILENAME = "hooks.json"
HOOK_MARKER = "bootstrap codex-hook"
SESSION_START_MATCHER = "^(startup|resume)$"
ADDITIONAL_CONTEXT_LIMIT = 5000
_TIMEOUT_SECONDS = 15.0


@dataclass(frozen=True)
class CodexHookInstallResult:
    changed: bool
    path: str


@dataclass(frozen=True)
class CodexIgnorePreflight:
    project_dir: str
    git_status: str
    p4_status: str

    @property
    def ready(self) -> bool:
        return self.git_status in ("ignored", "not_applicable") and self.p4_status in (
            "ignored", "not_applicable")


@dataclass(frozen=True)
class _Process:
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0


class CodexHookError(Exception):
    """The project Codex hook cannot be safely installed."""


def _bootstrap_commands() -> tuple[str, str]:
    """Return stable Unix and Windows commands for the generated hook."""
    home = os.environ.get("HOME") or os.path.expanduser("~")
    unix = shlex.quote(os.path.join(home, ".local", "bin", "bootstrap"))
    windows = os.path.join(home, ".local", "bin", "bootstrap.cmd")
    return unix + " " + HOOK_MARKER.split(" ", 1)[1], '"%s" %s' % (
        windows, HOOK_MARKER.split(" ", 1)[1])


def _hook_entry() -> dict:
    command, command_windows = _bootstrap_commands()
    return {
        "type": "command",
        "command": command,
        "commandWindows": command_windows,
        "statusMessage": "Running bootstrap",
        "timeout": 300,
        "additionalContextLimit": ADDITIONAL_CONTEXT_LIMIT,
    }


def _is_our_hook(value) -> bool:
    if not isinstance(value, dict):
        return False
    return _is_bootstrap_command(value.get("command")) or _is_bootstrap_command(
        value.get("commandWindows"), windows=True)


def _is_bootstrap_command(value, *, windows=False) -> bool:
    """Recognize this hook without deleting arbitrary commands by substring."""
    if not isinstance(value, str):
        return False
    parts = value.strip().rsplit(None, 1)
    if len(parts) != 2 or parts[1] != "codex-hook":
        return False
    command = parts[0].strip("'\"").replace("\\", "/")
    basename = command.rsplit("/", 1)[-1].lower()
    return basename == ("bootstrap.cmd" if windows else "bootstrap")


def _merge_hooks(document: dict) -> dict:
    if not isinstance(document, dict):
        raise CodexHookError(".codex/hooks.json must contain a JSON object")
    hooks = document.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise CodexHookError(".codex/hooks.json 'hooks' must be an object")
    groups = hooks.setdefault("SessionStart", [])
    if not isinstance(groups, list):
        raise CodexHookError(".codex/hooks.json 'hooks.SessionStart' must be a list")

    kept = []
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            kept.append(group)
            continue
        remaining = [hook for hook in group["hooks"] if not _is_our_hook(hook)]
        if remaining:
            updated = dict(group)
            updated["hooks"] = remaining
            kept.append(updated)

    kept.append({
        "matcher": SESSION_START_MATCHER,
        "hooks": [_hook_entry()],
    })
    hooks["SessionStart"] = kept
    return document


def _atomic_write(path: str, content: str) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".codex-hooks.", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
        os.replace(temporary, path)
    except Exception:
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise


@contextmanager
def _hooks_lock(path: str):
    """Serialize read/merge/write cycles across concurrent bootstrap passes."""
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def ensure_codex_hook(project_dir: str) -> CodexHookInstallResult:
    """Install or refresh this plugin's project-local Codex SessionStart hook."""
    project_dir = os.path.abspath(project_dir)
    if not os.path.isdir(project_dir):
        raise CodexHookError("project directory is not a directory: %s" % project_dir)

    codex_dir = os.path.join(project_dir, CODEX_DIRNAME)
    hooks_path = os.path.join(codex_dir, HOOKS_FILENAME)
    if os.path.islink(codex_dir):
        raise CodexHookError("refusing to write through symlinked %s" % codex_dir)
    if os.path.islink(hooks_path):
        raise CodexHookError("refusing to replace symlinked %s" % hooks_path)

    try:
        os.makedirs(codex_dir, exist_ok=True)
        with _hooks_lock(hooks_path + ".lock"):
            old_content = None
            document = {}
            mode = None
            if os.path.lexists(hooks_path):
                try:
                    mode = stat.S_IMODE(os.stat(hooks_path).st_mode)
                    with open(hooks_path, "r", encoding="utf-8") as stream:
                        old_content = stream.read()
                    document = json.loads(old_content)
                except (OSError, ValueError) as exc:
                    raise CodexHookError("cannot read %s: %s" % (hooks_path, exc)) from exc

            merged = _merge_hooks(document)
            new_content = json.dumps(merged, indent=2, ensure_ascii=False) + "\n"
            if old_content == new_content:
                return CodexHookInstallResult(False, hooks_path)
            if mode is not None:
                directory = os.path.dirname(hooks_path)
                fd, temporary = tempfile.mkstemp(prefix=".codex-hooks.", dir=directory)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                        stream.write(new_content)
                    os.chmod(temporary, mode)
                    os.replace(temporary, hooks_path)
                except Exception:
                    try:
                        os.remove(temporary)
                    except OSError:
                        pass
                    raise
            else:
                _atomic_write(hooks_path, new_content)
    except CodexHookError:
        raise
    except OSError as exc:
        raise CodexHookError("cannot write %s: %s" % (hooks_path, exc)) from exc
    return CodexHookInstallResult(True, hooks_path)


def _run_git(root: str, args) -> _Process:
    try:
        result = subprocess.run(
            ["git", "-C", root, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            timeout=_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return _Process(stderr=str(exc), returncode=127)
    return _Process(result.stdout or "", result.stderr or "", result.returncode)


def _run_p4(args) -> _Process:
    try:
        result = subprocess.run(
            ["p4", *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return _Process(stderr=str(exc), returncode=127)
    return _Process(result.stdout or "", result.stderr or "", result.returncode)


def resolve_project_root(project_dir: str) -> str:
    """Use the Git root when available; otherwise retain the named directory."""
    project_dir = os.path.realpath(os.path.abspath(project_dir))
    if not os.path.isdir(project_dir):
        return project_dir
    result = _run_git(project_dir, ["rev-parse", "--show-toplevel"])
    if result.returncode == 0 and result.stdout.strip():
        return os.path.realpath(result.stdout.strip())
    return project_dir


def _gitignore_status(project_dir: str) -> str:
    if shutil.which("git") is None:
        return "not_applicable"

    root = _run_git(project_dir, ["rev-parse", "--show-toplevel"])
    if root.returncode != 0:
        return "not_applicable"
    root_dir = os.path.realpath(root.stdout.strip())
    tracked = _run_git(project_dir, ["ls-files", "--", ".codex"])
    if tracked.returncode != 0:
        return "unknown"
    if tracked.stdout.strip():
        return "tracked"
    path = os.path.join(root_dir, ".gitignore")
    if not os.path.isfile(path):
        return "missing"

    result = _run_git(project_dir, [
        "check-ignore", "-v", "--no-index", "--", ".codex/",
    ])
    if result.returncode != 0:
        return "not_ignored" if result.returncode == 1 else "unknown"
    line = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
    fields = line.split(None, 2)
    if len(fields) < 2:
        return "unknown"
    # `git check-ignore -v` prints source, pattern, path.  The source is the
    # first field (the pattern can contain spaces, hence the bounded split).
    source = fields[0].split(":", 1)[0]
    source_path = source if os.path.isabs(source) else os.path.join(root_dir, source)
    try:
        local_rule = os.path.samefile(source_path, path)
    except OSError:
        local_rule = os.path.abspath(source_path) == os.path.abspath(path)
    return "ignored" if local_rule else "not_ignored"


def _p4ignore_status(project_dir: str) -> str:
    path = os.path.join(project_dir, ".p4ignore")
    local_evidence = os.path.isfile(path) or os.path.isfile(
        os.path.join(project_dir, "p4ignore.txt")) or bool(os.environ.get("P4CONFIG"))
    if shutil.which("p4") is None:
        return "unavailable" if local_evidence else "not_applicable"

    mapped = _run_p4([
        "-ztag", "-d", project_dir, "where",
        "%s/..." % os.path.join(project_dir, CODEX_DIRNAME),
    ])
    mapped_output = "%s\n%s" % (mapped.stdout, mapped.stderr)
    mapped_lower = mapped_output.lower()
    if "not in client view" in mapped_lower and not local_evidence:
        return "not_applicable"
    if (
        mapped.returncode != 0
        and not local_evidence
        and "depotfile" not in mapped_lower
        and "clientfile" not in mapped_lower
    ):
        return "not_applicable"
    if not os.path.isfile(path):
        return "missing"
    result = _run_p4([
        "-d", project_dir, "ignores", "-i", "-v",
        os.path.join(project_dir, ".codex"),
    ])
    output = "%s\n%s" % (result.stdout, result.stderr)
    output_lower = output.lower()
    if "ignored by" in output_lower and ".p4ignore" in output_lower:
        return "ignored"
    if "not ignored" in output_lower:
        return "not_ignored"
    return "unknown"


def _git_remediation(status: str) -> str:
    if status == "tracked":
        return (
            "The .codex directory is already tracked by Git. Do not remove it "
            "from the index automatically; ask the user how to handle the "
            "existing tracked hook, then add the anchored rule '/.codex/'."
        )
    if status == "missing":
        return "Create or update .gitignore and add the anchored rule '/.codex/'."
    if status == "unavailable":
        return "Verify .gitignore contains the anchored rule '/.codex/'."
    return "Modify .gitignore and add the anchored rule '/.codex/'."


def _p4_remediation(status: str) -> str:
    if status == "missing":
        return (
            "Create .p4ignore with the anchored rule '/.codex/'. If the file is "
            "already depot-managed, run 'p4 edit .p4ignore' before modifying it; "
            "if it is new, run 'p4 add .p4ignore' after creating it."
        )
    return (
        "Run 'p4 edit .p4ignore' before modifying .p4ignore, then add the "
        "anchored rule '/.codex/'."
    )


def codex_ignore_context(project_dir: str) -> str:
    """Return Codex-only remediation when either local ignore contract is open."""
    project_dir = resolve_project_root(project_dir)
    issues = []
    git_status = _gitignore_status(project_dir)
    if git_status not in ("ignored", "not_applicable"):
        issues.append("- .gitignore: %s" % _git_remediation(git_status))
    p4_status = _p4ignore_status(project_dir)
    if p4_status not in ("ignored", "not_applicable"):
        issues.append("- .p4ignore: %s" % _p4_remediation(p4_status))
    if not issues:
        return ""
    return (
        "Bootstrap Codex preflight found that the generated .codex directory is "
        "not fully protected from source control. Modify the named ignore file(s) "
        "now, then re-run the check:\n" + "\n".join(issues)
    )


def codex_ignore_preflight(project_dir: str) -> CodexIgnorePreflight:
    project_dir = resolve_project_root(project_dir)
    return CodexIgnorePreflight(
        project_dir=project_dir,
        git_status=_gitignore_status(project_dir),
        p4_status=_p4ignore_status(project_dir),
    )


def add_additional_context(response: dict, context: str) -> dict:
    """Append fixed preflight context to a Codex SessionStart response."""
    result = copy.deepcopy(response)
    if not context:
        return result
    output = result.setdefault("hookSpecificOutput", {})
    output["hookEventName"] = "SessionStart"
    existing = output.get("additionalContext", "")
    output["additionalContext"] = (
        "%s\n\n%s" % (existing, context) if existing else context
    )
    return result
