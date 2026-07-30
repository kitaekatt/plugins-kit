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

import re
import shutil
import stat
from pathlib import Path
from typing import Optional, Tuple

from . import SecretsError

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


def hooks_dir(clone_dir: Path) -> Path:
    """The clone's hooks directory, honoring a .git FILE (worktree/submodule)."""
    git_path = clone_dir / ".git"
    if git_path.is_file():
        # `gitdir: <path>` -- rare here, but silently installing into a
        # non-existent directory would leave the repo unguarded.
        content = git_path.read_text(encoding="utf-8").strip()
        if content.startswith("gitdir:"):
            target = content.split(":", 1)[1].strip()
            resolved = (clone_dir / target).resolve() if not Path(target).is_absolute() else Path(target)
            return resolved / "hooks"
    return git_path / "hooks"


def install(clone_dir: Path, *, force: bool = False) -> Tuple[bool, str]:
    """Ensure the clone has a current guard.

    Returns ``(changed, reason)``. Idempotent: an up-to-date guard is left
    alone and reports ``(False, "current")``.

    A hook that is present but carries NO version marker is treated as
    hand-written and left in place unless ``force`` -- overwriting someone's
    deliberate customization without asking would be its own kind of damage,
    and the caller surfaces it instead.
    """
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

    target_dir = hooks_dir(clone_dir)
    if not target_dir.is_dir():
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise SecretsError(f"cannot create {target_dir}: {e}")
    target = target_dir / HOOK_NAME

    if target.is_file() and not force:
        existing = target.read_text(encoding="utf-8", errors="replace")
        existing_version = _version_of(existing)
        if existing_version is None:
            return (False, "foreign hook left in place")
        if existing_version >= source_version:
            return (False, "current")

    shutil.copyfile(source, target)
    mode = target.stat().st_mode
    target.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return (True, f"installed guard v{source_version}")


def is_guarded(clone_dir: Path) -> bool:
    """True when the clone has an executable, version-marked guard."""
    target = hooks_dir(clone_dir) / HOOK_NAME
    if not target.is_file():
        return False
    return _version_of(target.read_text(encoding="utf-8", errors="replace")) is not None


def require_guard(clone_dir: Path) -> str:
    """Install if needed, then REFUSE to proceed if the clone is unguarded.

    Called before every authoring act. The refusal is the point: writing to a
    secrets repo with no guard is the one situation where doing nothing is
    strictly better than doing the requested thing.
    """
    changed, reason = install(clone_dir)
    if not is_guarded(clone_dir):
        raise SecretsError(
            f"the secrets clone at {clone_dir} has no pre-commit guard ({reason})",
            "Refusing to write to an unguarded secrets repo -- git history is "
            "permanent. Install the guard (or remove the conflicting "
            f"{hooks_dir(clone_dir) / HOOK_NAME}) and retry.",
        )
    return reason if changed else ""


def ensure_gitignore(clone_dir: Path) -> bool:
    """Write the deny-by-default .gitignore if the repo has none. True if written."""
    path = clone_dir / ".gitignore"
    if path.exists():
        return False
    path.write_text(GITIGNORE, encoding="utf-8")
    return True
