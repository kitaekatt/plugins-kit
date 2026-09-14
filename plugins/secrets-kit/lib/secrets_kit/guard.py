"""Install and verify the fleet-secrets pre-commit guard.

`.git/hooks` is not tracked by git, so the guard cannot ship inside the secrets
repo -- it has to be installed into every clone, on every machine, by us. That
makes installation an INVARIANT to enforce rather than a setup step to
document: a machine whose clone has no guard is one careless `git add` away
from a permanent leak, and it would look completely normal until then.

Hence:

- the hook is installed (and refreshed) idempotently before every authoring
  act, not once at setup time;
- it is COPIED rather than symlinked or sourced, because the plugin's cache
  path contains its version and moves on update -- a hook pointing into it
  would silently stop working after an upgrade;
- a version marker in the hook lets a newer plugin replace an older guard
  without clobbering something a human deliberately customized.
"""

import os
import re
import shutil
import stat
from pathlib import Path
from typing import Optional, Tuple

from . import SecretsError, repo as repo_mod

HOOK_NAME = "pre-commit"
_MARKER = re.compile(r"^# secrets-kit-guard-version:\s*(\d+)\s*$", re.MULTILINE)

# The .gitignore the seeded repo carries. Belt and braces with the hook's
# allowlist: the hook stops a deliberate `git add`, this stops the accidental
# `git add -A` from ever staging a stray plaintext file in the first place.
GITIGNORE = """\
# fleet-secrets: deny by default.
#
# Only encrypted blobs, the wrapped identity, and the manifest belong here. A
# plaintext file that is never staged can never be committed, and git history
# is permanent -- so this ignores everything and allows back the few paths that
# are safe. The pre-commit guard enforces the same allowlist for anything added
# with -f.
*

!.gitignore
!.gitattributes
!README.md
!manifest.json
!identity.age
!blobs/
!blobs/*.age
"""


def canonical_hook_path() -> Path:
    """The plugin's own copy of the hook."""
    return (
        Path(__file__).resolve().parent.parent.parent
        / "hooks"
        / "fleet-secrets-pre-commit"
    )


def _version_of(text: str) -> Optional[int]:
    match = _MARKER.search(text)
    return int(match.group(1)) if match else None


def _owned_hook_target(clone_dir: Path) -> Path:
    """Verify the ordinary local slot and Git's effective target before use."""
    remedy = (
        "Refusing to write outside the ordinary clone-owned hook slot. "
        "Use an ordinary local clone with unredirected .git/hooks/pre-commit; "
        "remove or correct the applicable core.hooksPath setting and retry."
    )
    try:
        root = clone_dir.resolve()
        target = root / ".git" / "hooks" / HOOK_NAME
        for path, directory, required in [
            (root / ".git", True, True),
            (target.parent, True, False),
            (target, False, False),
        ]:
            try:
                info = path.lstat()
            except FileNotFoundError:
                if required:
                    raise SecretsError(
                        f"effective pre-commit ownership cannot be verified: {path} is missing",
                        remedy,
                    )
                continue
            ordinary = (
                stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
            )
            shared_leaf = not directory and info.st_nlink != 1
            if not ordinary or path.resolve() != path or shared_leaf:
                raise SecretsError(
                    f"effective pre-commit ownership is unsupported at {path}", remedy
                )
        code, output = repo_mod._git(
            ["rev-parse", "--is-inside-work-tree", "--git-path", "hooks/pre-commit"],
            cwd=clone_dir,
            timeout=repo_mod.QUERY_TIMEOUT,
        )
        prefix = "true\n"
        reported = output[len(prefix):] if output.startswith(prefix) else ""
        if (
            code != 0 or not reported
            or any(char in reported for char in "\n\r\x00\ufffd")
        ):
            raise SecretsError(
                f"cannot determine the effective pre-commit target: {output or 'empty Git response'}",
                remedy,
            )
        effective = Path(reported)
        if not effective.is_absolute():
            effective = root / effective
        if effective.resolve() != target:
            raise SecretsError(
                f"effective pre-commit target {effective} is outside the owned slot {target}",
                remedy,
            )
        return target
    except (OSError, RuntimeError) as error:
        raise SecretsError(
            f"effective pre-commit ownership cannot be verified: {error}", remedy
        ) from error


def hooks_dir(clone_dir: Path) -> Path:
    """The verified ordinary clone-owned effective hooks directory."""
    return _owned_hook_target(clone_dir).parent


def install(clone_dir: Path, *, force: bool = False) -> Tuple[bool, str]:
    """Ensure the clone has a current guard.

    Returns ``(changed, reason)``. Idempotent: an up-to-date guard is left
    alone and reports ``(False, "current")``.

    A hook that is present but carries NO version marker is treated as
    hand-written and left in place unless ``force`` -- overwriting someone's
    deliberate customization without asking would be its own kind of damage,
    and the caller surfaces it instead.
    """
    return _install_at(_owned_hook_target(clone_dir), force=force)


def _install_at(target: Path, *, force: bool = False) -> Tuple[bool, str]:
    source = canonical_hook_path()
    if not source.is_file():
        raise SecretsError(
            f"the canonical guard is missing at {source}",
            "secrets-kit's own installation is incomplete; reinstall the plugin.",
        )
    source_text = source.read_text(encoding="utf-8")
    source_version = _version_of(source_text)
    if source_version is None:
        raise SecretsError(
            f"the canonical guard at {source} has no version marker",
            "It needs a '# secrets-kit-guard-version: N' line so clones can be "
            "upgraded safely.",
        )

    target_dir = target.parent
    if not target_dir.is_dir():
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise SecretsError(f"cannot create {target_dir}: {e}")
    if target.is_file() and not force:
        existing = target.read_text(encoding="utf-8", errors="replace")
        existing_version = _version_of(existing)
        if existing_version is None:
            return (False, "foreign hook left in place")
        if existing_version >= source_version:
            if os.name != "nt" and not os.access(target, os.X_OK):
                try:
                    target.chmod(
                        target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
                    )
                except OSError as error:
                    raise SecretsError(f"cannot restore executable guard at {target}: {error}") from error
                return (True, f"restored executable guard v{existing_version}")
            return (False, "current")

    shutil.copyfile(source, target)
    mode = target.stat().st_mode
    target.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return (True, f"installed guard v{source_version}")


def is_guarded(clone_dir: Path) -> bool:
    """True for an effective ordinary marked guard, executable on POSIX."""
    try:
        return _is_guarded_at(_owned_hook_target(clone_dir))
    except (SecretsError, OSError):
        return False


def _is_guarded_at(target: Path) -> bool:
    if not target.is_file():
        return False
    return (
        _version_of(target.read_text(encoding="utf-8", errors="replace")) is not None
        and (os.name == "nt" or os.access(target, os.X_OK))
    )


def require_guard(clone_dir: Path) -> str:
    """Install if needed, then REFUSE to proceed if the clone is unguarded.

    Called before every authoring act. The refusal is the point: writing to a
    secrets repo with no guard is the one situation where doing nothing is
    strictly better than doing the requested thing.
    """
    target = _owned_hook_target(clone_dir)
    changed, reason = _install_at(target)
    if not _is_guarded_at(target):
        raise SecretsError(
            f"the secrets clone at {clone_dir} has no pre-commit guard ({reason})",
            "Refusing to write to an unguarded secrets repo -- git history is "
            "permanent. Install the guard (or remove the conflicting "
            f"{target}) and retry.",
        )
    return reason if changed else ""


def ensure_gitignore(clone_dir: Path) -> bool:
    """Write the deny-by-default .gitignore if the repo has none. True if written."""
    path = clone_dir / ".gitignore"
    if path.exists():
        return False
    path.write_text(GITIGNORE, encoding="utf-8")
    return True
