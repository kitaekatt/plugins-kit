"""The pre-commit guard: installation, and whether it actually blocks.

The second half matters more than the first. A guard that installs cleanly and
then fails to refuse is worse than no guard at all, because it manufactures
confidence. So these tests drive the REAL shell hook through REAL `git commit`
in a temp repo, rather than asserting on its source text.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from secrets_kit import SecretsError, guard

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git required"
)

_ARMORED = "-----BEGIN AGE ENCRYPTED FILE-----\nZmFrZQo=\n-----END AGE ENCRYPTED FILE-----\n"
_BINARY_HEADER = "age-encryption.org/v1\n-> X25519 abc\nfake\n"


def _git(repo, *args, check=True):
    proc = subprocess.run(
        ["git"] + list(args),
        cwd=str(repo),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if check and proc.returncode != 0:
        raise AssertionError(f"git {args} failed: {proc.stdout}")
    return proc


@pytest.fixture
def repo(tmp_path):
    """A guarded, committable git repo standing in for fleet-secrets."""
    path = tmp_path / "fleet-secrets"
    path.mkdir()
    _git(path, "init", "--quiet")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "T")
    # Hooks must be allowed to run; some environments set core.hooksPath.
    _git(path, "config", "--unset-all", "core.hooksPath", check=False)
    (path / "blobs").mkdir()
    guard.install(path)
    return path


def _commit(repo, *paths, message="t"):
    _git(repo, "add", "-f", "--", *paths)
    return subprocess.run(
        ["git", "commit", "-m", message],
        cwd=str(repo),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


# --- installation ---------------------------------------------------------

def test_install_places_an_executable_hook(tmp_path):
    path = tmp_path / "r"
    (path / ".git" / "hooks").mkdir(parents=True)
    changed, reason = guard.install(path)
    hook = path / ".git" / "hooks" / "pre-commit"
    assert changed and "installed" in reason
    assert hook.is_file()
    if not sys.platform.startswith("win"):
        assert os.access(hook, os.X_OK)


def test_install_is_idempotent(tmp_path):
    path = tmp_path / "r"
    (path / ".git" / "hooks").mkdir(parents=True)
    guard.install(path)
    changed, reason = guard.install(path)
    assert changed is False
    assert reason == "current"


def test_install_creates_the_hooks_dir_if_absent(tmp_path):
    """A clone missing .git/hooks must not silently end up unguarded."""
    path = tmp_path / "r"
    (path / ".git").mkdir(parents=True)
    changed, _ = guard.install(path)
    assert changed
    assert guard.is_guarded(path)


def test_a_foreign_hook_is_not_clobbered(tmp_path):
    """Overwriting a hand-written hook without asking is its own kind of damage."""
    path = tmp_path / "r"
    hooks = path / ".git" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "pre-commit").write_text("#!/bin/sh\necho mine\n", encoding="utf-8")

    changed, reason = guard.install(path)
    assert changed is False
    assert "foreign" in reason
    assert "echo mine" in (hooks / "pre-commit").read_text()


def test_an_older_guard_is_upgraded(tmp_path):
    path = tmp_path / "r"
    hooks = path / ".git" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "pre-commit").write_text(
        "#!/bin/sh\n# secrets-kit-guard-version: 0\nexit 0\n", encoding="utf-8"
    )
    changed, reason = guard.install(path)
    assert changed and "installed" in reason


def test_require_guard_refuses_when_a_foreign_hook_blocks_installation(tmp_path):
    """Writing to an unguarded secrets repo must fail rather than proceed."""
    path = tmp_path / "r"
    hooks = path / ".git" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "pre-commit").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    with pytest.raises(SecretsError, match="no pre-commit guard"):
        guard.require_guard(path)


def test_gitignore_is_written_once(tmp_path):
    path = tmp_path / "r"
    path.mkdir()
    assert guard.ensure_gitignore(path) is True
    text = (path / ".gitignore").read_text()
    assert text.startswith("# fleet-secrets: deny by default.")
    assert "*\n" in text
    assert "!blobs/*.age" in text
    # Never overwrite an existing one.
    assert guard.ensure_gitignore(path) is False


# --- does it actually refuse? --------------------------------------------

def test_encrypted_blob_and_manifest_are_allowed(repo):
    (repo / "blobs" / "ha-token.txt.age").write_text(_ARMORED, encoding="utf-8")
    (repo / "manifest.json").write_text('{"version": 1}', encoding="utf-8")
    proc = _commit(repo, "blobs/ha-token.txt.age", "manifest.json")
    assert proc.returncode == 0, proc.stdout


def test_binary_age_header_is_also_accepted(repo):
    """age's non-armored output must not be mistaken for plaintext."""
    (repo / "blobs" / "x.age").write_text(_BINARY_HEADER, encoding="utf-8")
    proc = _commit(repo, "blobs/x.age")
    assert proc.returncode == 0, proc.stdout


def test_plaintext_under_blobs_is_refused(repo):
    """The load-bearing case: a real secret that merely has the right path."""
    (repo / "blobs" / "ha-token.txt.age").write_text(
        "eyJhbGciOi.REAL_TOKEN_VALUE\n", encoding="utf-8"
    )
    proc = _commit(repo, "blobs/ha-token.txt.age")
    assert proc.returncode != 0
    assert "REFUSED" in proc.stdout
    assert "NOT age ciphertext" in proc.stdout


def test_a_stray_plaintext_file_is_refused(repo):
    (repo / "ha-token.txt").write_text("secret\n", encoding="utf-8")
    proc = _commit(repo, "ha-token.txt")
    assert proc.returncode != 0
    assert "not an allowed path" in proc.stdout


def test_an_unwrapped_identity_is_refused(repo):
    (repo / "identity.age").write_text(
        "AGE-SECRET-KEY-1QQQQQQQQQQQQQQQQQQ\n", encoding="utf-8"
    )
    proc = _commit(repo, "identity.age")
    assert proc.returncode != 0
    assert "REFUSED" in proc.stdout


def test_a_secret_key_hiding_in_an_allowed_path_is_refused(repo):
    """The second net: right path, catastrophic content."""
    (repo / "manifest.json").write_text(
        '{"recipient": "age1x", "note": "AGE-SECRET-KEY-1LEAKED"}', encoding="utf-8"
    )
    proc = _commit(repo, "manifest.json")
    assert proc.returncode != 0
    assert "master key in plaintext" in proc.stdout


def test_the_guard_reads_the_index_not_the_worktree(repo):
    """Stage plaintext, then make the worktree look innocent.

    A guard that inspects the working tree would pass this and commit the
    staged plaintext -- the exact sleight of hand it has to survive.
    """
    blob = repo / "blobs" / "x.age"
    blob.write_text("PLAINTEXT SECRET\n", encoding="utf-8")
    _git(repo, "add", "-f", "--", "blobs/x.age")
    blob.write_text(_ARMORED, encoding="utf-8")  # worktree now looks fine

    proc = subprocess.run(
        ["git", "commit", "-m", "t"],
        cwd=str(repo),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert proc.returncode != 0
    assert "NOT age ciphertext" in proc.stdout


def test_one_bad_file_blocks_the_whole_commit(repo):
    """Partial acceptance would leave the secret staged for the next commit."""
    (repo / "blobs" / "good.age").write_text(_ARMORED, encoding="utf-8")
    (repo / "blobs" / "bad.age").write_text("plaintext\n", encoding="utf-8")
    proc = _commit(repo, "blobs/good.age", "blobs/bad.age")
    assert proc.returncode != 0
    assert "bad.age" in proc.stdout


def test_refusal_names_the_encrypt_path_instead_of_just_saying_no(repo):
    """A guard that blocks without telling you the right move invites --no-verify."""
    (repo / "creds.txt").write_text("hunter2\n", encoding="utf-8")
    proc = _commit(repo, "creds.txt")
    assert "secrets-kit add" in proc.stdout
    assert "--no-verify" in proc.stdout


def test_deleting_a_blob_is_allowed(repo):
    """`secrets-kit remove` deletes a blob; the guard must not block removals."""
    blob = repo / "blobs" / "x.age"
    blob.write_text(_ARMORED, encoding="utf-8")
    assert _commit(repo, "blobs/x.age").returncode == 0

    blob.unlink()
    _git(repo, "add", "-A", "--", "blobs")
    proc = subprocess.run(
        ["git", "commit", "-m", "remove"],
        cwd=str(repo),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout


def test_a_filename_with_a_space_fails_closed(repo):
    """The hook word-splits the staged list; that must never fail OPEN.

    A path containing a space splits into fragments, none of which match the
    allowlist, so the catch-all refuses. Pinning it because the alternative --
    a fragment accidentally matching an allowed name and letting the file
    through -- is the one way this design could leak.
    """
    (repo / "my token.txt").write_text("secret\n", encoding="utf-8")
    proc = _commit(repo, "my token.txt")
    assert proc.returncode != 0
    assert "REFUSED" in proc.stdout


def test_a_space_filename_prefixed_with_an_allowed_name_still_fails(repo):
    (repo / "manifest.json extra.txt").write_text("secret\n", encoding="utf-8")
    proc = _commit(repo, "manifest.json extra.txt")
    assert proc.returncode != 0
    assert "REFUSED" in proc.stdout
