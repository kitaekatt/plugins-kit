"""Safe managed symlink resource."""

from __future__ import annotations

import ntpath
import os
from dataclasses import dataclass
from pathlib import Path
import tempfile

from .model import (
    Inspection,
    Operation,
    ResourceApplyError,
    ResourceResult,
    State,
    Status,
)


def _is_windows() -> bool:
    return os.name == "nt"


def _canonical(path: Path) -> str:
    resolved = os.path.realpath(os.path.abspath(path))
    if _is_windows():
        return ntpath.normcase(ntpath.normpath(resolved))
    return os.path.normcase(os.path.normpath(resolved))


def _reserve_backup(target: Path) -> Path:
    """Atomically hard-link a regular target to a collision-free backup name."""
    candidate = target.with_name(f"{target.name}.backup")
    index = 1
    while True:
        try:
            os.link(target, candidate, follow_symlinks=False)
            return candidate
        except FileExistsError:
            candidate = target.with_name(f"{target.name}.backup.{index}")
            index += 1


def _remove_if_present(path: Path | None) -> None:
    if path is not None and os.path.lexists(path):
        os.unlink(path)


def _link_spelling(path: Path) -> str | None:
    try:
        return os.readlink(path) if path.is_symlink() else None
    except OSError:
        return None


@dataclass(frozen=True)
class Symlink:
    """Declare ``target`` as a symlink to an existing ``source``.

    Regular files are preserved by default. Directories are never replaced.
    A temporary sibling symlink is atomically placed with ``os.replace``.
    """

    source: str | os.PathLike[str]
    target: str | os.PathLike[str]
    name: str = "symlink"
    backup: bool = True

    @property
    def source_path(self) -> Path:
        return Path(os.path.abspath(Path(self.source).expanduser()))

    @property
    def target_path(self) -> Path:
        return Path(os.path.abspath(Path(self.target).expanduser()))

    def inspect(self) -> Inspection:
        source = self.source_path
        target = self.target_path
        if not source.exists():
            return Inspection(State.ERROR, f"source does not exist: {source}")
        if os.path.abspath(source) == os.path.abspath(target):
            return Inspection(State.ERROR, f"source and target are the same path: {source}")
        if not os.path.lexists(target):
            return Inspection(State.MISSING, f"missing: {target}")
        if not target.is_symlink():
            try:
                if os.path.samefile(source, target):
                    return Inspection(
                        State.ERROR,
                        f"source and target resolve to the same file: {target}",
                    )
            except OSError:
                pass
            kind = "directory" if target.is_dir() else "regular file"
            return Inspection(State.DRIFTED, f"target is a {kind}: {target}")
        if _canonical(target) != _canonical(source):
            return Inspection(State.DRIFTED, f"symlink points to the wrong source: {target}")
        return Inspection(State.CURRENT, f"{target} -> {source}")

    def converge(
        self, before: Inspection, operation: Operation
    ) -> ResourceResult:
        source = self.source_path
        target = self.target_path
        if not source.exists():
            raise FileNotFoundError(f"source does not exist: {source}")
        if os.path.abspath(source) == os.path.abspath(target):
            raise ValueError(f"source and target are the same path: {source}")
        current = self.inspect()
        if operation is Operation.INSTALL and current.state is not State.MISSING:
            raise ResourceApplyError(
                f"install precondition changed: {current.detail}", after=current)
        if current.state is State.ERROR:
            raise ResourceApplyError(current.detail, after=current)
        if current.state is State.CURRENT:
            return ResourceResult(
                self.name, Status.UNCHANGED, before.state, current.state,
                current.detail)
        if os.path.lexists(target) and not target.is_symlink() and target.is_dir():
            raise IsADirectoryError(f"refusing to replace directory: {target}")

        target.parent.mkdir(parents=True, exist_ok=True)
        if operation is Operation.INSTALL:
            try:
                # Unlike os.replace, symlink creation fails atomically with EEXIST
                # if another process creates the target after our inspection.
                os.symlink(source, target, target_is_directory=source.is_dir())
            except Exception as exc:
                after = self.inspect()
                raise ResourceApplyError(
                    f"symlink create failed: {exc}", after=after,
                    rollback="not needed; create did not replace an existing target",
                ) from exc
            after = self.inspect()
            if after.state is not State.CURRENT:
                rollback = "invalid link no longer owned; left target untouched"
                if _link_spelling(target) == str(source):
                    _remove_if_present(target)
                    rollback = "removed invalid newly-created link"
                restored = self.inspect()
                raise ResourceApplyError(
                    f"symlink did not converge: {after.detail}", after=restored,
                    rollback=rollback,
                )
            return ResourceResult(
                self.name, Status.CHANGED, before.state, after.state,
                f"linked {target} -> {source}")

        temp_path: Path | None = None
        backup_path: Path | None = None
        old_link: str | None = None
        old_link_is_dir = False
        switched = False
        try:
            fd, temp_name = tempfile.mkstemp(
                prefix=f".{target.name}.link.", dir=target.parent)
            os.close(fd)
            os.unlink(temp_name)
            temp_path = Path(temp_name)
            os.symlink(source, temp_path, target_is_directory=source.is_dir())

            existing_regular = os.path.lexists(target) and not target.is_symlink()
            if existing_regular and self.backup:
                backup_path = _reserve_backup(target)
            elif target.is_symlink():
                old_link = os.readlink(target)
                old_link_is_dir = target.is_dir()
            os.replace(temp_path, target)
            temp_path = None
            switched = True
            after = self.inspect()
            if after.state is not State.CURRENT:
                raise RuntimeError(f"symlink did not converge: {after.detail}")
        except Exception as exc:
            rollback = "not needed; original target remains"
            try:
                owns_target = _link_spelling(target) == str(source)
                if switched and backup_path is not None and owns_target:
                    os.replace(backup_path, target)
                    backup_path = None
                    rollback = "restored original regular target from backup"
                elif switched and backup_path is not None:
                    rollback = "target changed concurrently; preserved backup"
                elif switched and old_link is not None and owns_target:
                    fd, rollback_name = tempfile.mkstemp(
                        prefix=f".{target.name}.rollback.", dir=target.parent)
                    os.close(fd)
                    os.unlink(rollback_name)
                    rollback_temp = Path(rollback_name)
                    os.symlink(old_link, rollback_temp,
                               target_is_directory=old_link_is_dir)
                    os.replace(rollback_temp, target)
                    rollback = "restored original symlink"
                elif switched and old_link is not None:
                    rollback = "target changed concurrently; did not overwrite it"
                elif backup_path is not None:
                    _remove_if_present(backup_path)
                    backup_path = None
                    rollback = "removed backup duplicate; original target remained"
            except Exception as rollback_exc:
                rollback = f"rollback failed: {rollback_exc}"
            after = self.inspect()
            raise ResourceApplyError(
                f"symlink update failed: {exc}",
                after=after,
                backup=str(backup_path) if backup_path is not None else None,
                rollback=rollback,
            ) from exc
        finally:
            _remove_if_present(temp_path)

        detail = f"linked {target} -> {source}"
        if backup_path is not None:
            detail += f"; backed up previous target to {backup_path}"
        return ResourceResult(
            self.name, Status.CHANGED, before.state, after.state, detail,
            str(backup_path) if backup_path is not None else None)
