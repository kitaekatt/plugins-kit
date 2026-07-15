"""Hermetic linking and symlink-privilege helpers for bootstrap tests.

Symlink creation on Windows requires SeCreateSymbolicLinkPrivilege, which a
stock machine only has with Developer Mode enabled or an elevated shell.
Tests must be hermetic on a stock machine, so:

- Fixture plumbing that only needs "make this path mirror that tree" (the
  fake plugin roots that link the real bootstrap_lib/engine) goes through
  link_tree(), which falls back to an NTFS junction (no privilege needed)
  and finally a copy.
- Tests whose SUBJECT is real symlink behavior (the env.json symlinks
  feature, check_symlink/fix_symlink unit tests) skip via requires_symlinks
  when the privilege is absent; the product's own WinError-1314 deferral
  path is covered separately on unprivileged machines.
"""

import os
import shutil
import tempfile

import pytest


def _probe_symlink_privilege() -> bool:
    """One symlink attempt in a private tmp dir; True if the OS allows it."""
    probe_dir = tempfile.mkdtemp(prefix="symlink_probe_")
    try:
        target = os.path.join(probe_dir, "t")
        with open(target, "w"):
            pass
        try:
            os.symlink(target, os.path.join(probe_dir, "l"))
            return True
        except OSError:
            return False
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)


CAN_SYMLINK = _probe_symlink_privilege()

requires_symlinks = pytest.mark.skipif(
    not CAN_SYMLINK,
    reason="symlink creation requires Windows Developer Mode or an "
           "elevated shell",
)


def link_tree(link, target) -> None:
    """Make `link` mirror `target` (a directory or file), privilege-free.

    Fallback order: real symlink (cheapest; keeps privileged machines on
    the exact layout these fixtures always used) -> NTFS junction for
    directories (junctions need no privilege on Windows) -> copy. The copy
    fallback is safe here because fixture consumers only READ through the
    link; they never edit the real tree expecting the link to reflect it.
    """
    target = os.fspath(target)
    try:
        link.symlink_to(target)
        return
    except OSError:
        pass
    if os.path.isdir(target):
        try:
            import _winapi

            _winapi.CreateJunction(target, os.fspath(link))
            return
        except (ImportError, OSError):
            pass
        shutil.copytree(target, link,
                        ignore=shutil.ignore_patterns("__pycache__"))
    else:
        shutil.copy2(target, link)
