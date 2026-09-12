"""The pre-commit guard: installation, and whether it actually blocks.

The second half matters more than the first. A guard that installs cleanly and
then fails to refuse is worse than no guard at all, because it manufactures
confidence. So these tests drive the REAL shell hook through REAL `git commit`
in a temp repo, rather than asserting on its source text.
"""

import importlib.util
import json
import os
import stat
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from sk_testlib import copy_git_tree

from secrets_kit import SecretsError, guard, repo as repo_mod

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git required"
)

_ARMORED = "-----BEGIN AGE ENCRYPTED FILE-----\nZmFrZQo=\n-----END AGE ENCRYPTED FILE-----\n"
_BINARY_HEADER = "age-encryption.org/v1\n-> X25519 abc\nfake\n"
_EFFECTIVE_ARGS = ["rev-parse", "--is-inside-work-tree", "--git-path", "hooks/pre-commit"]


def _git(repo, *args, check=True):
    proc = subprocess.run(
        ["git"] + list(args),
        cwd=str(repo),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
    )
    if check and proc.returncode != 0:
        raise AssertionError(f"git {args} failed: {proc.stdout}")
    return proc


def _build_guarded_repo(path: Path) -> None:
    # Session setup must not consult a real global/system hook configuration.
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
        patch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
        _git(path, "init", "--quiet")
        _git(path, "config", "user.email", "t@example.com")
        _git(path, "config", "user.name", "T")
        _git(path, "config", "--unset-all", "core.hooksPath", check=False)
        (path / "blobs").mkdir()
        guard.install(path)


@pytest.fixture(scope="session")
def _guarded_repo_template(git_template):
    return git_template("guard-repo", _build_guarded_repo)


@pytest.fixture
def repo(tmp_path, monkeypatch, _guarded_repo_template):
    """A guarded, committable git repo standing in for fleet-secrets.

    A private copy of a per-process template (see `sk_testlib`) -- a real git
    repo with the real hook installed, which this test alone commits into.
    """
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    return copy_git_tree(_guarded_repo_template, tmp_path / "fleet-secrets")


def _commit(repo, *paths, message="t"):
    _git(repo, "add", "-f", "--", *paths)
    return subprocess.run(
        ["git", "commit", "-m", message],
        cwd=str(repo),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
    )


# --- installation ---------------------------------------------------------

def test_install_places_an_executable_hook(repo):
    path = repo
    (path / ".git" / "hooks" / "pre-commit").unlink()
    changed, reason = guard.install(path)
    hook = path / ".git" / "hooks" / "pre-commit"
    assert changed and "installed" in reason
    assert hook.is_file()
    if not sys.platform.startswith("win"):
        assert os.access(hook, os.X_OK)


def test_install_is_idempotent(repo):
    path = repo
    guard.install(path)
    changed, reason = guard.install(path)
    assert changed is False
    assert reason == "current"


def test_install_creates_the_hooks_dir_if_absent(repo):
    """A clone missing .git/hooks must not silently end up unguarded."""
    path = repo
    shutil.rmtree(path / ".git" / "hooks")
    changed, _ = guard.install(path)
    assert changed
    assert guard.is_guarded(path)


def test_a_foreign_hook_is_not_clobbered(repo):
    """Overwriting a hand-written hook without asking is its own kind of damage."""
    path = repo
    hooks = path / ".git" / "hooks"
    (hooks / "pre-commit").write_text("#!/bin/sh\necho mine\n", encoding="utf-8")

    changed, reason = guard.install(path)
    assert changed is False
    assert "foreign" in reason
    assert "echo mine" in (hooks / "pre-commit").read_text()


def test_an_older_guard_is_upgraded(repo):
    path = repo
    hooks = path / ".git" / "hooks"
    (hooks / "pre-commit").write_text(
        "#!/bin/sh\n# secrets-kit-guard-version: 0\nexit 0\n", encoding="utf-8"
    )
    changed, reason = guard.install(path)
    assert changed and "installed" in reason


def test_require_guard_refuses_when_a_foreign_hook_blocks_installation(repo):
    """Writing to an unguarded secrets repo must fail rather than proceed."""
    path = repo
    hooks = path / ".git" / "hooks"
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


@pytest.mark.parametrize("path", ["manifest.json", "README.md", ".gitignore", ".gitattributes"])
def test_a_secret_key_hiding_in_an_allowed_path_is_refused(repo, path):
    """The second net: right path, catastrophic content."""
    (repo / path).write_text(
        '{"recipient": "age1x", "note": "AGE-SECRET-KEY-1LEAKED"}', encoding="utf-8"
    )
    proc = _commit(repo, path)
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
    """The complete staged filename must satisfy the allowlist."""
    (repo / "my token.txt").write_text("secret\n", encoding="utf-8")
    proc = _commit(repo, "my token.txt")
    assert proc.returncode != 0
    assert "REFUSED" in proc.stdout


def test_a_space_filename_prefixed_with_an_allowed_name_still_fails(repo):
    (repo / "manifest.json extra.txt").write_text("secret\n", encoding="utf-8")
    proc = _commit(repo, "manifest.json extra.txt")
    assert proc.returncode != 0
    assert "REFUSED" in proc.stdout


@pytest.mark.parametrize("content", [b"dummy plaintext\n", b"AGE-SECRET-KEY-DUMMY\n"])
def test_a_filename_composed_of_allowed_names_is_refused(repo, content):
    path = "README.md manifest.json"
    (repo / path).write_bytes(content)
    proc = _commit(repo, path)
    assert proc.returncode != 0, proc.stdout
    assert "REFUSED" in proc.stdout
    assert path in proc.stdout
    assert _git(repo, "rev-parse", "--verify", "HEAD", check=False).returncode != 0


@pytest.mark.parametrize("path", [
    "blobs/token with space.age",
    "blobs/ token.age",
    "blobs/token[1].age",
    pytest.param("blobs/token[1]*.age", marks=pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX star filename")),
])
def test_complete_allowed_blob_names_are_not_split_or_expanded(repo, path):
    (repo / path).write_text(_ARMORED, encoding="utf-8")
    proc = _commit(repo, path)
    assert proc.returncode == 0, proc.stdout
    assert _git(repo, "show", f"HEAD:{path}").stdout == _ARMORED


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX filename controls")
@pytest.mark.parametrize("path", ['blobs/token\nother.age', 'blobs/token"quoted.age', 'blobs/token\\slash.age'])
def test_git_quoted_paths_are_explicitly_refused(repo, path):
    _git(repo, "config", "core.quotePath", "false")
    (repo / path).write_text(_ARMORED, encoding="utf-8")
    proc = _commit(repo, path)
    assert proc.returncode != 0, proc.stdout
    assert "quoted" in proc.stdout
    assert _git(repo, "rev-parse", "--verify", "HEAD", check=False).returncode != 0


@pytest.mark.parametrize("private_key", [False, True])
def test_binary_ciphertext_preserves_nul_bytes_and_scans_beyond_the_prefix(repo, private_key):
    data = _BINARY_HEADER.encode() + b"\x00" * (256 * 1024)
    if private_key:
        data += b"AGE-SECRET-KEY-DUMMY\n"
    (repo / "blobs" / "binary.age").write_bytes(data)
    proc = _commit(repo, "blobs/binary.age")
    if private_key:
        assert proc.returncode != 0
        assert "master key in plaintext" in proc.stdout
    else:
        assert proc.returncode == 0, proc.stdout
        actual = subprocess.run(["git", "show", "HEAD:blobs/binary.age"], cwd=repo, capture_output=True)
        assert actual.returncode == 0
        assert actual.stdout == data


def _status_home(home=None):
    return Path(home if home is not None else os.environ["HOME"]) / ".claude/plugins/data/plugins-kit/secrets-kit"


def _failing_hook_tool(tmp_path, monkeypatch, tool, body, create_status_home=True):
    """Replace one hook boundary while retaining real Git commit execution."""
    bin_dir = tmp_path / "fault-bin"
    bin_dir.mkdir()
    target = bin_dir / tool
    target.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    target.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    if tool == "git":
        # Git prepends its exec path to hook PATH. Override that path inside
        # this test so the shell hook reaches the fault proxy, not real Git.
        monkeypatch.setenv("GIT_EXEC_PATH", str(bin_dir))
    scratch = tmp_path / "hook-scratch"
    scratch.mkdir()
    monkeypatch.setenv("TMPDIR", str(scratch))
    status_home = _status_home()
    if create_status_home:
        status_home.mkdir(parents=True)
    return status_home


@pytest.mark.parametrize("operation", ["diff", "show"])
def test_git_index_failures_refuse_even_when_partial_output_looks_safe(repo, tmp_path, monkeypatch, operation):
    real_git = shlex.quote(shutil.which("git"))
    output = "README.md" if operation == "diff" else "-----BEGIN AGE ENCRYPTED FILE-----"
    invoked = tmp_path / "fault-invoked"
    body = (
        f'for arg do\n  if [ "$arg" = "{operation}" ]; then\n'
        f"    printf '%s\\n' '{operation}' >> {shlex.quote(str(invoked))}\n"
        f"    printf '%s\\n' '{output}'\n    exit 1\n  fi\ndone\n"
        f'exec {real_git} "$@"\n'
    )
    scratch = _failing_hook_tool(tmp_path, monkeypatch, "git", body)
    path = "README.md" if operation == "diff" else "blobs/x.age"
    (repo / path).write_text(_ARMORED, encoding="utf-8")
    proc = _commit(repo, path)
    expected_calls = [operation] * (2 if operation == "show" else 1)
    assert invoked.read_text().splitlines() == expected_calls
    assert proc.returncode != 0, proc.stdout
    assert "REFUSED" in proc.stdout
    expected = "enumerat" if operation == "diff" else "staged content"
    assert expected in proc.stdout
    assert list(scratch.iterdir()) == []
    assert _git(repo, "rev-parse", "--verify", "HEAD", check=False).returncode != 0


def test_a_private_key_scan_error_cannot_certify_safe_content(repo, tmp_path, monkeypatch):
    scratch = _failing_hook_tool(tmp_path, monkeypatch, "grep", "exit 2\n")
    (repo / "README.md").write_text("dummy metadata\n", encoding="utf-8")
    proc = _commit(repo, "README.md")
    assert proc.returncode != 0, proc.stdout
    assert "REFUSED" in proc.stdout
    assert "scan" in proc.stdout
    assert list(scratch.iterdir()) == []


@pytest.mark.parametrize("tool", ["head", "cat", "od", "tr"])
def test_ciphertext_prefix_inspection_errors_refuse(repo, tmp_path, monkeypatch, tool):
    scratch = _failing_hook_tool(tmp_path, monkeypatch, tool, "exit 1\n")
    (repo / "blobs" / "x.age").write_text(_ARMORED, encoding="utf-8")
    proc = _commit(repo, "blobs/x.age")
    assert proc.returncode != 0, proc.stdout
    assert "prefix" in proc.stdout
    assert list(scratch.iterdir()) == []


@pytest.mark.parametrize("boundary", ["prefix", "scan"])
@pytest.mark.parametrize("damage", [
    "missing", "empty", "invalid",
    pytest.param("unreadable", marks=pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX status-file permissions")),
])
def test_producer_status_cannot_be_missing_or_unverifiable(repo, tmp_path, monkeypatch, boundary, damage):
    tool = "tr" if boundary == "prefix" else "grep"
    real_tool = shlex.quote(shutil.which(tool))
    record = "git-head" if boundary == "prefix" else "git-scan"
    fault_seen = tmp_path / "status-damaged"
    action = {
        "missing": 'rm -f "$record"',
        "empty": ': > "$record"',
        "invalid": 'printf "not-a-status\\n" > "$record"',
        "unreadable": 'chmod 000 "$record"',
    }[damage]
    body = (
        f'{real_tool} "$@"\ntool_status=$?\n'
        'for dir in "$HOME"/.claude/plugins/data/plugins-kit/secrets-kit/.guard-status.*; do\n'
        '  [ -d "$dir" ] || continue\n'
        f'  record="$dir/{record}"\n  {action}\n'
        f"  printf '%s\\n' '{damage}' > {shlex.quote(str(fault_seen))}\n"
        'done\n'
        'exit "$tool_status"\n'
    )
    scratch = _failing_hook_tool(tmp_path, monkeypatch, tool, body)
    path = "blobs/x.age" if boundary == "prefix" else "README.md"
    (repo / path).write_text(_ARMORED, encoding="utf-8")
    proc = _commit(repo, path)
    assert fault_seen.read_text().strip() == damage
    assert proc.returncode != 0, proc.stdout
    expected = "prefix" if boundary == "prefix" else "complete staged content"
    assert expected in proc.stdout
    assert list(scratch.iterdir()) == []


def test_binary_nul_before_the_header_is_not_removed_to_fabricate_ciphertext(repo):
    (repo / "blobs" / "x.age").write_bytes(b"\x00" + _BINARY_HEADER.encode())
    proc = _commit(repo, "blobs/x.age")
    assert proc.returncode != 0, proc.stdout
    assert "NOT age ciphertext" in proc.stdout


def test_filename_data_is_not_evaluated_as_shell_commands(repo):
    path = "blobs/$(touch GOTCHA).age"
    (repo / path).write_text(_ARMORED, encoding="utf-8")
    proc = _commit(repo, path)
    assert proc.returncode == 0, proc.stdout
    assert not (repo / "GOTCHA").exists()
    assert _git(repo, "show", f"HEAD:{path}").stdout == _ARMORED


def test_a_prior_v1_guard_is_refreshed_to_the_canonical_hook(repo):
    path = repo
    hooks = path / ".git" / "hooks"
    target = hooks / "pre-commit"
    target.write_text("#!/bin/sh\n# secrets-kit-guard-version: 1\nexit 0\n", encoding="utf-8")
    changed, reason = guard.install(path)
    assert changed, reason
    assert "v2" in reason
    assert target.read_bytes() == guard.canonical_hook_path().read_bytes()


def test_unicode_blob_respects_an_existing_false_quote_setting(repo):
    _git(repo, "config", "core.quotePath", "false")
    path = "blobs/\u79d8\u5bc6.age"
    (repo / path).write_text(_ARMORED, encoding="utf-8")
    proc = _commit(repo, path)
    assert proc.returncode == 0, proc.stdout
    assert _git(repo, "show", f"HEAD:{path}").stdout == _ARMORED
    assert _git(repo, "ls-tree", "--name-only", "HEAD", "--", path).stdout.strip() == path


def test_unicode_blob_still_refuses_a_private_key_with_quote_setting_false(repo):
    _git(repo, "config", "core.quotePath", "false")
    path = "blobs/\u79d8\u5bc6.age"
    (repo / path).write_text(_ARMORED + "AGE-SECRET-KEY-DUMMY\n", encoding="utf-8")
    proc = _commit(repo, path)
    assert proc.returncode != 0, proc.stdout
    assert "master key in plaintext" in proc.stdout
    assert _git(repo, "rev-parse", "--verify", "HEAD", check=False).returncode != 0


@pytest.mark.parametrize("setting", [None, "true"])
def test_default_or_true_quote_setting_keeps_unicode_paths_refused(repo, setting):
    if setting is None:
        _git(repo, "config", "--unset-all", "core.quotePath", check=False)
    else:
        _git(repo, "config", "core.quotePath", setting)
    path = "blobs/\u79d8\u5bc6.age"
    (repo / path).write_text(_ARMORED, encoding="utf-8")
    proc = _commit(repo, path)
    assert proc.returncode != 0, proc.stdout
    assert "quoted" in proc.stdout
    assert _git(repo, "rev-parse", "--verify", "HEAD", check=False).returncode != 0


@pytest.mark.parametrize("allowed", [True, False])
@pytest.mark.parametrize("alias", [False, pytest.param(True, marks=pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX home symlink"))])
def test_status_channel_is_created_under_home_and_cleaned_without_using_tmpdir(repo, tmp_path, monkeypatch, isolated_user_home, allowed, alias):
    if alias:
        home_alias = tmp_path / "home alias"
        home_alias.symlink_to(isolated_user_home, target_is_directory=True)
        monkeypatch.setenv("HOME", str(home_alias))
    real_mktemp = shlex.quote(shutil.which("mktemp"))
    observed = tmp_path / "status-location"
    body = (
        f'created=$({real_mktemp} "$@")\ntool_status=$?\n'
        f"printf '%s\\n' \"$created\" > {shlex.quote(str(observed))}\n"
        'printf "%s\\n" "$created"\nexit "$tool_status"\n'
    )
    status_home = _failing_hook_tool(tmp_path, monkeypatch, "mktemp", body, create_status_home=False)
    assert not status_home.exists()
    path = "README.md" if allowed else "unallowed.txt"
    (repo / path).write_text("dummy metadata\n", encoding="utf-8")
    proc = _commit(repo, path)
    assert (proc.returncode == 0) is allowed, proc.stdout
    created = Path(observed.read_text().strip())
    assert created.parent == status_home.resolve()
    assert created.name.startswith(".guard-status.")
    assert status_home.is_dir()
    assert list(status_home.iterdir()) == []
    assert list(Path(os.environ["TMPDIR"]).iterdir()) == []
    assert not (repo / ".claude").exists()


@pytest.mark.parametrize("value", [None, "", "relative-home", "C:relative-home", "\\relative-home", "\\\\?\\C:\\relative-home", "\\\\.\\C:\\relative-home"])
def test_invalid_home_is_diagnosed_without_a_fallback(repo, tmp_path, monkeypatch, value):
    fallback = tmp_path / "fallback"
    fallback.mkdir()
    monkeypatch.setenv("TMPDIR", str(fallback))
    if value is None:
        monkeypatch.delenv("HOME")
    else:
        monkeypatch.setenv("HOME", value)
    (repo / "README.md").write_text("dummy metadata\n", encoding="utf-8")
    proc = _commit(repo, "README.md")
    assert proc.returncode != 0, proc.stdout
    assert "REFUSED" in proc.stdout
    assert "HOME" in proc.stdout
    assert list(fallback.iterdir()) == []
    assert not (repo / "relative-home").exists()
    assert _git(repo, "rev-parse", "--verify", "HEAD", check=False).returncode != 0


@pytest.mark.skipif(sys.platform.startswith("win"), reason="native POSIX path semantics")
@pytest.mark.parametrize("platform_labels", [False, True])
def test_native_drive_home_cannot_be_treated_as_a_relative_posix_directory(repo, tmp_path, monkeypatch, platform_labels):
    relative_home = repo / "C:" / "fixture-home"
    relative_home.mkdir(parents=True)
    fallback = tmp_path / "fallback"
    fallback.mkdir()
    monkeypatch.setenv("HOME", "C:/fixture-home")
    monkeypatch.setenv("TMPDIR", str(fallback))
    # Environment labels do not establish native Windows path support.
    if platform_labels:
        monkeypatch.setenv("OS", "Windows_NT")
        monkeypatch.setenv("MSYSTEM", "MINGW64")
    (repo / "README.md").write_text("dummy metadata\n", encoding="utf-8")
    proc = _commit(repo, "README.md")
    assert proc.returncode != 0, proc.stdout
    assert "HOME" in proc.stdout
    assert list(relative_home.iterdir()) == []
    assert list(fallback.iterdir()) == []


@pytest.mark.parametrize("kind", ["file", "absent"])
def test_unusable_home_is_diagnosed_without_a_fallback(repo, tmp_path, monkeypatch, kind):
    home = tmp_path / "unusable-home"
    if kind == "file":
        home.write_text("sentinel", encoding="utf-8")
    fallback = tmp_path / "fallback"
    fallback.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TMPDIR", str(fallback))
    (repo / "README.md").write_text("dummy metadata\n", encoding="utf-8")
    proc = _commit(repo, "README.md")
    assert proc.returncode != 0, proc.stdout
    assert "REFUSED" in proc.stdout
    assert "HOME" in proc.stdout
    assert list(fallback.iterdir()) == []
    if kind == "file":
        assert home.read_text() == "sentinel"
    else:
        assert not home.exists()


def test_plugin_home_creation_failure_is_diagnosed_without_a_fallback(repo, tmp_path, monkeypatch, isolated_user_home):
    (isolated_user_home / ".claude").write_text("sentinel", encoding="utf-8")
    fallback = tmp_path / "fallback"
    fallback.mkdir()
    monkeypatch.setenv("TMPDIR", str(fallback))
    (repo / "README.md").write_text("dummy metadata\n", encoding="utf-8")
    proc = _commit(repo, "README.md")
    assert proc.returncode != 0, proc.stdout
    assert "REFUSED" in proc.stdout
    assert "status home" in proc.stdout
    assert (isolated_user_home / ".claude").read_text() == "sentinel"
    assert list(fallback.iterdir()) == []


def test_unique_status_directory_creation_failure_refuses_without_a_fallback(repo, tmp_path, monkeypatch):
    status_home = _failing_hook_tool(tmp_path, monkeypatch, "mktemp", "exit 1\n")
    (repo / "README.md").write_text("dummy metadata\n", encoding="utf-8")
    proc = _commit(repo, "README.md")
    assert proc.returncode != 0, proc.stdout
    assert "REFUSED" in proc.stdout
    assert "status scratch" in proc.stdout
    assert list(status_home.iterdir()) == []
    assert list(Path(os.environ["TMPDIR"]).iterdir()) == []


def test_unique_status_cleanup_preserves_existing_plugin_data_and_peers(repo, tmp_path, monkeypatch, isolated_user_home):
    status_home = _status_home(isolated_user_home)
    peer = status_home / ".guard-status.peer"
    peer.mkdir(parents=True)
    (peer / "sentinel").write_text("keep", encoding="utf-8")
    (status_home / "identity.txt").write_text("dummy identity sentinel", encoding="utf-8")
    (repo / "README.md").write_text("dummy metadata\n", encoding="utf-8")
    proc = _commit(repo, "README.md")
    assert proc.returncode == 0, proc.stdout
    assert sorted(path.name for path in status_home.iterdir()) == [".guard-status.peer", "identity.txt"]
    assert (peer / "sentinel").read_text() == "keep"
    assert (status_home / "identity.txt").read_text() == "dummy identity sentinel"


# --- effective Git hook precondition --------------------------------------

@pytest.mark.skipif(os.name == "nt", reason="POSIX executable bits required")
def test_current_guard_execute_permission_is_repaired_before_real_commit(repo):
    hook = repo / ".git" / "hooks" / "pre-commit"
    before = hook.read_bytes()
    hook.chmod(hook.stat().st_mode & ~(stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
    note = guard.require_guard(repo)
    (repo / "dummy.txt").write_text("dummy plaintext", encoding="utf-8")
    proc = _commit(repo, "dummy.txt")
    assert proc.returncode != 0, f"unguarded prerequisite allowed a real commit: {proc.stdout}"
    assert "REFUSED" in proc.stdout
    assert os.access(hook, os.X_OK)
    assert hook.read_bytes() == before
    assert "executable" in note


@pytest.mark.parametrize("location", [
    "local", "global", "leading-space",
    pytest.param("leading-tab", marks=pytest.mark.skipif(os.name == "nt", reason="tab directory is POSIX-only")),
    pytest.param("trailing-space", marks=pytest.mark.skipif(os.name == "nt", reason="trailing space directory is POSIX-only")),
    pytest.param("newline", marks=pytest.mark.skipif(os.name == "nt", reason="LF directory is POSIX-only")),
])
def test_alternate_effective_target_refuses_before_real_plaintext_commit(repo, tmp_path, monkeypatch, location):
    hook = repo / ".git" / "hooks" / "pre-commit"
    before = hook.read_bytes()
    alternate = repo / ".git" / ("hooks " if location == "trailing-space" else "hooks\n" if location == "newline" else "other-hooks")
    if location in ["leading-space", "leading-tab"]:
        alternate = repo / (" .git" if location == "leading-space" else "\t.git") / "hooks"
    alternate.mkdir(parents=True)
    if location == "global":
        config = tmp_path / "controlled-global.config"
        _git(repo, "config", "--file", str(config), "core.hooksPath", str(alternate))
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    else:
        configured = (
            " .git/hooks" if location == "leading-space" else
            "\t.git/hooks" if location == "leading-tab" else str(alternate)
        )
        _git(repo, "config", "core.hooksPath", configured)
    real = repo_mod._git
    calls = []
    def record(args, **kwargs):
        calls.append((args, kwargs))
        return real(args, **kwargs)
    monkeypatch.setattr(repo_mod, "_git", record)
    try:
        guard.require_guard(repo)
    except SecretsError as exc:
        assert "effective pre-commit" in str(exc)
        assert "core.hooksPath" in str(exc)
    else:
        (repo / "dummy.txt").write_text("dummy plaintext", encoding="utf-8")
        proc = _commit(repo, "dummy.txt")
        pytest.fail(f"prerequisite accepted alternate target; real plaintext commit status={proc.returncode}: {proc.stdout}")
    assert calls == [(_EFFECTIVE_ARGS, {"cwd": repo, "timeout": repo_mod.QUERY_TIMEOUT})]
    assert hook.read_bytes() == before
    assert list(alternate.iterdir()) == []
    assert _git(repo, "rev-parse", "--verify", "HEAD", check=False).returncode != 0


@pytest.mark.parametrize("force", [False, True])
def test_alternate_foreign_hook_is_never_overwritten_even_with_force(repo, force):
    alternate = repo / ".git" / "shared-hooks"
    alternate.mkdir()
    target = alternate / "pre-commit"
    target.write_text("#!/bin/sh\necho foreign\n", encoding="utf-8")
    before = target.read_bytes()
    _git(repo, "config", "core.hooksPath", str(alternate))
    with pytest.raises(SecretsError, match="effective pre-commit"):
        guard.install(repo, force=force)
    assert target.read_bytes() == before


def test_force_replaces_foreign_hook_only_in_the_ordinary_local_scope(repo):
    target = repo / ".git" / "hooks" / "pre-commit"
    target.write_text("#!/bin/sh\necho foreign\n", encoding="utf-8")
    changed, reason = guard.install(repo, force=True)
    assert changed, reason
    assert target.read_bytes() == guard.canonical_hook_path().read_bytes()
    (repo / "blobs" / "positive.age").write_text(_ARMORED, encoding="utf-8")
    proc = _commit(repo, "blobs/positive.age")
    assert proc.returncode == 0, proc.stdout


@pytest.mark.parametrize("entry", ["require_guard", "install", "is_guarded"])
def test_guard_entrypoint_uses_one_bounded_real_effective_target_query(repo, monkeypatch, entry):
    real = repo_mod._git
    calls = []
    def record(args, **kwargs):
        calls.append((args, kwargs))
        return real(args, **kwargs)
    monkeypatch.setattr(repo_mod, "_git", record)
    result = getattr(guard, entry)(repo)
    assert result is not False
    assert calls == [(_EFFECTIVE_ARGS, {"cwd": repo, "timeout": repo_mod.QUERY_TIMEOUT})]


@pytest.mark.parametrize("response", [
    (124, "query timed out"), (127, "git unavailable"), (0, ""),
    (0, "warning\n.git/hooks/pre-commit"),
    (0, "false\n.git/hooks/pre-commit"), (0, "true\n"),
    (0, "true\n.git/hooks/pre-commit\nextra"),
    (0, "true\n.git/hooks/pre-commit\r"),
    (0, "true\n.git/hooks/pre-commit\x00"),
    (0, "true\n.git/hoo\ufffdks/pre-commit"),
    (1, "true\n.git/hooks/pre-commit"),
    (0, "true"), (0, "--unknown-flag\ntrue\n.git/hooks/pre-commit"),
])
def test_effective_target_query_failure_cannot_certify_or_modify_a_hook(repo, monkeypatch, response):
    target = repo / ".git" / "hooks" / "pre-commit"
    before = target.read_bytes()
    calls = []
    def failed_query(args, **kwargs):
        calls.append((args, kwargs))
        return response
    monkeypatch.setattr(repo_mod, "_git", failed_query)
    expected = (_EFFECTIVE_ARGS, {"cwd": repo, "timeout": repo_mod.QUERY_TIMEOUT})
    with pytest.raises(SecretsError, match="effective pre-commit"):
        guard.require_guard(repo)
    assert calls == [expected]
    assert guard.is_guarded(repo) is False
    assert calls == [expected] * 2
    with pytest.raises(SecretsError, match="effective pre-commit"):
        guard.install(repo, force=True)
    assert calls == [expected] * 3
    assert target.read_bytes() == before


@pytest.mark.parametrize("command", ["init", "add", "add-update", "remove", "rotate-identity"])
def test_inline_authoring_refuses_effective_target_before_named_effects(repo, tmp_path, monkeypatch, capsys, command):
    cli_path = Path(__file__).resolve().parents[2] / "plugins" / "secrets-kit" / "scripts" / "secrets_kit_cli.py"
    # The real CLI inserts its lib path; keep that change local to this test.
    monkeypatch.setattr(sys, "path", sys.path.copy())
    spec = importlib.util.spec_from_file_location("guard_test_inline_cli", cli_path)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    data = tmp_path / "inline-data"
    data.mkdir()
    clone = copy_git_tree(repo, data / "repo")
    alternate = tmp_path / "alternate-empty"
    alternate.mkdir()
    _git(clone, "config", "core.hooksPath", str(alternate))
    config = tmp_path / "inline-secrets.json"
    config.write_text(json.dumps({"repo": str(tmp_path / "unused-remote.git"), "machines": {"testbox": {"profiles": []}}}), encoding="utf-8")
    monkeypatch.setattr(cli, "CONFIG_PATH", config)
    monkeypatch.setattr(cli, "DATA_DIR", data)
    monkeypatch.setattr("secrets_kit.manifest.resolve_host", lambda: ["testbox"])
    monkeypatch.setattr(repo_mod, "sync", lambda clone: None)
    monkeypatch.setattr(repo_mod, "remote_has", lambda clone, name: False)
    destination = tmp_path / "plain-destination"
    destination.mkdir()
    manifest = clone / "manifest.json"
    manifest.write_text(json.dumps({"version": 1, "recipient": "age1dummy", "profiles": {"home": ["existing"]}, "entries": {"existing": {"blob": "blobs/existing.age", "dest": str(destination / "existing"), "mode": "0600"}}}), encoding="utf-8")
    blob = clone / "blobs" / "existing.age"
    blob.write_text(_ARMORED, encoding="utf-8")
    identity = data / "identity.txt"
    identity.write_text("dummy cached identity", encoding="utf-8")
    source = tmp_path / "new-token.txt"
    source.write_text("dummy input", encoding="utf-8")
    named = [manifest, blob, identity, clone / "identity.age", clone / "blobs" / "new-token.txt.age", clone / ".gitignore"]
    preimages = {path: path.read_bytes() if path.exists() else None for path in named}
    effects = []
    def effect(name):
        def fail(*args, **kwargs):
            effects.append(name)
            raise SecretsError(f"EFFECT BOUNDARY: {name}")
        return fail
    for name in ["keygen", "wrap_identity", "encrypt_to_recipient", "decrypt_with_identity"]:
        monkeypatch.setattr(cli.agefile, name, effect(name))
    monkeypatch.setattr(repo_mod, "commit_and_push", effect("commit_and_push"))
    argv = [command]
    if command in ["add", "add-update"]:
        argv = ["add", "existing" if command == "add-update" else "new", "--file", str(source), "--dest", str(destination / "new")]
        if command == "add-update":
            argv.append("--update")
    elif command == "remove":
        argv.append("existing")
    code = cli.main(argv)
    output = capsys.readouterr()
    assert code == 1
    assert "effective pre-commit" in output.err, f"missing guard refusal; effects={effects}; diagnostic={output.err}"
    assert effects == []
    assert {path: path.read_bytes() if path.exists() else None for path in named} == preimages
    assert list(alternate.iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="native Windows link behavior remains unverified")
@pytest.mark.parametrize("form", ["leaf-current", "leaf-old-force", "dangling-leaf", "hooks-empty", "hooks-current", "hooks-foreign", "git-directory", "hard-link"])
def test_static_redirected_hook_slot_refuses_without_changing_outside_preimages(repo, tmp_path, monkeypatch, form):
    hooks = repo / ".git" / "hooks"
    target = hooks / "pre-commit"
    outside = tmp_path / "outside"
    outside.mkdir()
    external = outside / "pre-commit"
    external.write_text("#!/bin/sh\n# secrets-kit-guard-version: 2\nexit 0\n", encoding="utf-8")
    external.chmod(0o600)
    if form == "leaf-old-force":
        external.write_text("#!/bin/sh\n# secrets-kit-guard-version: 1\nexit 0\n", encoding="utf-8")
    elif form == "hooks-foreign":
        external.write_text("#!/bin/sh\necho foreign\n", encoding="utf-8")
    if form.startswith("leaf") or form == "dangling-leaf":
        target.unlink()
        target.symlink_to(external if form != "dangling-leaf" else outside / "missing")
    elif form.startswith("hooks"):
        shutil.rmtree(hooks)
        if form == "hooks-empty":
            external.unlink()
        hooks.symlink_to(outside, target_is_directory=True)
    elif form == "git-directory":
        relocated = outside / "git-dir"
        shutil.move(repo / ".git", relocated)
        (repo / ".git").symlink_to(relocated, target_is_directory=True)
    else:
        target.unlink()
        os.link(external, target)
    def snapshot():
        return {str(path.relative_to(outside)): (path.read_bytes(), stat.S_IMODE(path.stat().st_mode)) for path in outside.rglob("*") if path.is_file()}
    before = snapshot()
    real = repo_mod._git
    calls = []
    def record(args, **kwargs):
        calls.append((args, kwargs))
        return real(args, **kwargs)
    monkeypatch.setattr(repo_mod, "_git", record)
    with pytest.raises(SecretsError, match="effective pre-commit.*ownership"):
        if form == "leaf-old-force":
            guard.install(repo, force=True)
        else:
            guard.require_guard(repo)
    assert snapshot() == before
    assert guard.is_guarded(repo) is False
    assert calls == []
    if form.startswith("leaf") or form == "dangling-leaf":
        assert target.is_symlink()
    elif form.startswith("hooks"):
        assert hooks.is_symlink()
    elif form == "git-directory":
        assert (repo / ".git").is_symlink()
    else:
        assert target.stat().st_ino == external.stat().st_ino


@pytest.mark.skipif(os.name == "nt", reason="native Windows symlink behavior remains unverified")
def test_clone_root_alias_and_equivalent_local_hook_config_remain_supported(repo, tmp_path):
    alias = tmp_path / "clone-alias"
    alias.symlink_to(repo, target_is_directory=True)
    _git(repo, "config", "core.hooksPath", str(repo / ".git" / "hooks" / "."))
    assert guard.require_guard(alias) == ""
    assert guard.is_guarded(alias)
    (repo / "blobs" / "alias.age").write_text(_ARMORED, encoding="utf-8")
    proc = _commit(repo, "blobs/alias.age")
    assert proc.returncode == 0, proc.stdout
