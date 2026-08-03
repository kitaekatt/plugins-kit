"""Tests for secrets_kit.perms -- ACL tightening on Windows, chmod on POSIX.

The Windows case here is a regression suite for a real outage: tighten_dir
granted the owner a NON-inheritable ACE, so stripping the directory's inherited
ACEs left every file already inside with an empty DACL. secrets-kit's own
bootstrap.log became unwritable and the resulting PermissionError aborted the
bootstrap engine on every SessionStart.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(
    0, str(Path(__file__).resolve().parents[2] / "plugins" / "secrets-kit" / "lib")
)

from secrets_kit import SecretsError  # noqa: E402
from secrets_kit import perms  # noqa: E402

IS_WINDOWS = sys.platform.startswith("win")
windows_only = pytest.mark.skipif(not IS_WINDOWS, reason="ACL behavior is Windows-only")
posix_only = pytest.mark.skipif(IS_WINDOWS, reason="POSIX modes are decorative on Windows")


def _ace_count(path: Path) -> int:
    """Number of ACEs in the path's DACL, via PowerShell (0 == empty DACL)."""
    proc = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f"(Get-Acl '{path}').Access.Count",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
    )
    return int(proc.stdout.decode("utf-8", "replace").strip())


def _appendable(path: Path) -> bool:
    try:
        with open(path, "a"):
            return True
    except OSError:
        return False


@windows_only
def test_tighten_dir_keeps_preexisting_files_accessible(tmp_path):
    """The regression: files created BEFORE hardening must survive it.

    Ordering is the whole test. A file created after tighten_dir picks up the
    creating token's default DACL and looks fine either way, which is exactly
    why the bug shipped -- only pre-existing files are corrupted.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    log = data_dir / "bootstrap.log"
    log.write_text("existing entry\n")

    perms.tighten_dir(data_dir)

    assert _ace_count(log) > 0, "pre-existing file was left with an empty DACL"
    assert _appendable(log), "pre-existing file became unwritable by its owner"


@windows_only
def test_tighten_dir_covers_files_created_after(tmp_path):
    data_dir = tmp_path / "data"
    perms.tighten_dir(data_dir)

    created = data_dir / "state.json"
    created.write_text("{}\n")

    assert _ace_count(created) > 0
    assert _appendable(created)


@windows_only
def test_tighten_dir_stays_owner_only(tmp_path):
    """The inheritance flags must not widen WHO has access."""
    data_dir = tmp_path / "data"
    perms.tighten_dir(data_dir)

    proc = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f"(Get-Acl '{data_dir}').Access | "
            "ForEach-Object { $_.IdentityReference.Value }",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
    )
    principals = [
        line.strip()
        for line in proc.stdout.decode("utf-8", "replace").splitlines()
        if line.strip()
    ]
    assert principals, "directory ended up with no ACEs at all"
    expected = perms._current_windows_principal().lower()
    assert all(p.lower() == expected for p in principals), (
        f"tighten_dir granted access beyond the owner: {principals}"
    )


@windows_only
def test_tighten_file_is_not_inheritable(tmp_path):
    """A FILE takes a plain grant; inheritance flags are meaningless there."""
    target = tmp_path / "secret.txt"
    target.write_text("s3cret\n")

    perms.tighten(target, 0o600)

    assert _ace_count(target) == 1
    assert _appendable(target)


@windows_only
def test_tighten_leaves_0644_alone(tmp_path):
    """A public key is public -- 0644 keeps inherited permissions."""
    target = tmp_path / "id_ed25519.pub"
    target.write_text("ssh-ed25519 AAAA...\n")
    before = _ace_count(target)

    perms.tighten(target, 0o644)

    assert _ace_count(target) == before


@windows_only
def test_missing_username_is_a_real_failure(tmp_path, monkeypatch):
    """A secret written with the wrong ACL is not a partial success."""
    monkeypatch.delenv("USERNAME", raising=False)
    target = tmp_path / "secret.txt"
    target.write_text("s3cret\n")

    with pytest.raises(SecretsError):
        perms.tighten(target, 0o600)


@posix_only
def test_tighten_dir_chmods_0700(tmp_path):
    data_dir = tmp_path / "data"
    perms.tighten_dir(data_dir)
    assert (data_dir.stat().st_mode & 0o777) == 0o700


@posix_only
def test_tighten_chmods_to_mode(tmp_path):
    target = tmp_path / "secret.txt"
    target.write_text("s3cret\n")
    perms.tighten(target, 0o600)
    assert (target.stat().st_mode & 0o777) == 0o600


def test_open_private_creates_at_final_mode(tmp_path):
    """Decrypted material must never exist at a looser mode, even briefly."""
    target = tmp_path / "nested" / "secret.txt"

    fd = perms.open_private(target, 0o600)
    try:
        os.write(fd, b"s3cret\n")
    finally:
        os.close(fd)

    assert target.read_text() == "s3cret\n"
    if not IS_WINDOWS:
        assert (target.stat().st_mode & 0o777) == 0o600
