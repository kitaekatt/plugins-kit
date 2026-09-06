"""Tests for git_dep_check.py — git dependency validation."""

import os
import subprocess

import pytest

from bootstrap_lib.git_dep_check import (
    check_git_dep,
    ensure_git_dep,
    _extract_repo_name,
    _build_clone_cmd,
)


class TestExtractRepoName:
    def test_https_url(self):
        assert _extract_repo_name("https://github.com/octocat/Hello-World") == "Hello-World"

    def test_url_with_git_suffix(self):
        assert _extract_repo_name("https://github.com/octocat/Hello-World.git") == "Hello-World"

    def test_trailing_slash(self):
        assert _extract_repo_name("https://github.com/octocat/Hello-World/") == "Hello-World"


class TestCheckGitDep:
    def test_missing_directory(self, tmp_path):
        """Returns failure when clone directory doesn't exist."""
        result = check_git_dep(
            str(tmp_path), "https://github.com/octocat/Hello-World", "master",
        )

        assert not result.passed
        assert "not cloned" in result.message
        assert result.subject == "Hello-World"
        assert "git clone" in result.remediation_cmd

    def test_directory_not_git_repo(self, tmp_path):
        """Returns failure when directory exists but is not a git repo."""
        target = tmp_path / "github" / "Hello-World"
        target.mkdir(parents=True)

        result = check_git_dep(
            str(tmp_path), "https://github.com/octocat/Hello-World", "master",
        )

        assert not result.passed
        assert "not a git repo" in result.message

    def test_correct_clone(self, tmp_path):
        """Passes when directory is a git repo on the correct branch."""
        target = tmp_path / "github" / "my-repo"
        target.mkdir(parents=True)

        # Init a real git repo on the expected branch
        subprocess.run(["git", "init", str(target)], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(target), "checkout", "-b", "main"],
            capture_output=True, check=True,
        )
        # Need at least one commit for branch to exist
        subprocess.run(
            ["git", "-C", str(target), "commit", "--allow-empty", "-m", "init"],
            capture_output=True, check=True,
            env={**os.environ, "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@test.com",
                 "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@test.com"},
        )
        subprocess.run(
            ["git", "-C", str(target), "remote", "add", "origin",
             "https://github.com/example/my-repo.git"],
            capture_output=True, check=True,
        )

        result = check_git_dep(
            str(tmp_path), "https://github.com/example/my-repo.git", "main",
        )

        assert result.passed
        assert "cloned on main" in result.message
        assert result.remediation_cmd is None

    def test_changed_origin_fails(self, tmp_path):
        target = tmp_path / "github" / "my-repo"
        target.mkdir(parents=True)
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(target), "commit", "--allow-empty", "-m", "init"],
            capture_output=True, check=True,
            env={**os.environ, "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@test.com",
                 "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@test.com"},
        )
        subprocess.run(
            ["git", "-C", str(target), "remote", "add", "origin", "https://example.com/moved.git"],
            capture_output=True, check=True,
        )

        result = check_git_dep(
            str(tmp_path), "https://github.com/example/my-repo", "main",
        )

        assert not result.passed
        assert "origin" in result.message

    def test_corrupt_clone_reports_could_not_check(self, tmp_path, monkeypatch):
        target = tmp_path / "github" / "my-repo"
        (target / ".git").mkdir(parents=True)

        class _Proc:
            returncode = 1
            stdout = ""
            stderr = "fatal: corrupt repository"

        monkeypatch.setattr("bootstrap_lib.git_dep_check.subprocess.run", lambda *a, **k: _Proc())
        result = check_git_dep(
            str(tmp_path), "https://github.com/example/my-repo", "main",
        )

        assert not result.passed
        assert "could not check" in result.message

    def test_wrong_branch(self, tmp_path):
        """Returns failure when repo is on wrong branch."""
        target = tmp_path / "github" / "my-repo"
        target.mkdir(parents=True)

        env = {**os.environ, "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@test.com",
               "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@test.com"}
        subprocess.run(["git", "init", str(target)], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(target), "checkout", "-b", "develop"],
            capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "-C", str(target), "commit", "--allow-empty", "-m", "init"],
            capture_output=True, check=True, env=env,
        )

        result = check_git_dep(
            str(tmp_path), "https://github.com/example/my-repo", "main",
        )

        assert not result.passed
        assert "develop" in result.message
        assert "expected main" in result.message
        assert "checkout main" in result.remediation_cmd

    def test_sparse_checkout_remediation(self, tmp_path):
        """Remediation includes sparse-checkout when sparse_paths specified."""
        result = check_git_dep(
            str(tmp_path), "https://github.com/octocat/Hello-World", "master",
            sparse_paths=["README", "docs"],
        )

        assert not result.passed
        assert "sparse-checkout" in result.remediation_cmd
        assert "README" in result.remediation_cmd
        assert "docs" in result.remediation_cmd

    def test_target_path_uses_github_subdir(self, tmp_path):
        """Clone target is always <data_dir>/github/<repo_name>/."""
        result = check_git_dep(
            str(tmp_path), "https://github.com/octocat/Hello-World", "master",
        )

        expected = os.path.join(str(tmp_path), "github", "Hello-World")
        assert result.target_path == expected


class TestCommitPinning:
    """Tests for commit SHA pinning in git dependencies."""

    @staticmethod
    def _git_env():
        return {
            **os.environ,
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@test.com",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@test.com",
        }

    def _init_repo_with_commits(self, target, branch="main"):
        """Create a git repo with two commits, return (first_sha, second_sha)."""
        env = self._git_env()
        subprocess.run(["git", "init", str(target)], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(target), "checkout", "-b", branch],
            capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "-C", str(target), "commit", "--allow-empty", "-m", "first"],
            capture_output=True, check=True, env=env,
        )
        r1 = subprocess.run(
            ["git", "-C", str(target), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        )
        first_sha = r1.stdout.strip()

        subprocess.run(
            ["git", "-C", str(target), "commit", "--allow-empty", "-m", "second"],
            capture_output=True, check=True, env=env,
        )
        subprocess.run(
            ["git", "-C", str(target), "remote", "add", "origin",
             "https://github.com/example/my-repo"],
            capture_output=True, check=True,
        )
        r2 = subprocess.run(
            ["git", "-C", str(target), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        )
        second_sha = r2.stdout.strip()
        return first_sha, second_sha

    def test_commit_pinning_matches(self, tmp_path):
        """Passes when HEAD matches pinned commit."""
        target = tmp_path / "github" / "my-repo"
        target.mkdir(parents=True)
        first_sha, second_sha = self._init_repo_with_commits(target)

        # Check out the first commit
        subprocess.run(
            ["git", "-C", str(target), "checkout", first_sha],
            capture_output=True, check=True,
        )

        result = check_git_dep(
            str(tmp_path), "https://github.com/example/my-repo", "main",
            commit=first_sha,
        )

        assert result.passed
        assert first_sha[:7] in result.message

    def test_commit_mismatch(self, tmp_path):
        """Fails when HEAD differs from pinned commit."""
        target = tmp_path / "github" / "my-repo"
        target.mkdir(parents=True)
        first_sha, second_sha = self._init_repo_with_commits(target)

        # HEAD is at second_sha, but we pin to first_sha
        result = check_git_dep(
            str(tmp_path), "https://github.com/example/my-repo", "main",
            commit=first_sha,
        )

        assert not result.passed
        assert first_sha[:7] in result.message
        assert "fetch" in result.remediation_cmd
        assert "checkout" in result.remediation_cmd

    def test_full_sha_pin_does_not_accept_same_prefix(self, tmp_path, monkeypatch):
        target = tmp_path / "github" / "my-repo"
        target.mkdir(parents=True)
        first_sha, _second_sha = self._init_repo_with_commits(target)
        fake_head = first_sha[:7] + "1" * 33
        pin = first_sha[:7] + "2" * 33
        real_run = subprocess.run

        def _fake_run(cmd, **kwargs):
            if cmd[-2:] == ["rev-parse", "HEAD"]:
                return type("_Proc", (), {"returncode": 0, "stdout": fake_head + "\n", "stderr": ""})()
            return real_run(cmd, **kwargs)

        monkeypatch.setattr("bootstrap_lib.git_dep_check.subprocess.run", _fake_run)
        result = check_git_dep(
            str(tmp_path), "https://github.com/example/my-repo", "main", commit=pin,
        )

        assert not result.passed
        assert "expected" in result.message

    def test_sparse_checkout_drift_fails(self, tmp_path):
        target = tmp_path / "github" / "my-repo"
        target.mkdir(parents=True)
        env = self._git_env()
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        (target / "README").write_text("readme\n")
        (target / "docs").mkdir()
        (target / "docs" / "guide").write_text("guide\n")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True, env=env)
        subprocess.run(
            ["git", "-C", str(target), "remote", "add", "origin", "https://github.com/example/my-repo"],
            capture_output=True, check=True,
        )
        subprocess.run(["git", "-C", str(target), "sparse-checkout", "set", "docs"], capture_output=True, check=True)

        result = check_git_dep(
            str(tmp_path), "https://github.com/example/my-repo", "main",
            sparse_paths=["README"],
        )

        assert not result.passed
        assert "sparse" in result.message

    def test_commit_not_cloned(self, tmp_path):
        """Not-cloned failure includes checkout step in remediation."""
        result = check_git_dep(
            str(tmp_path), "https://github.com/example/my-repo", "main",
            commit="abc1234567890",
        )

        assert not result.passed
        assert "not cloned" in result.message
        assert "checkout abc1234567890" in result.remediation_cmd

    def test_build_clone_cmd_with_commit(self):
        """Clone command includes checkout step when commit is specified."""
        cmd = _build_clone_cmd(
            "https://github.com/example/repo.git",
            "main",
            "/tmp/test",
            commit="abc123",
        )
        assert "git clone --branch main" in cmd
        assert "&& git -C /tmp/test checkout abc123" in cmd


class TestEnsureGitDep:
    @staticmethod
    def _git_env():
        return {
            **os.environ,
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@test.com",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@test.com",
        }

    def test_wrong_branch_is_fixed_and_authoritatively_rechecked(self, tmp_path):
        upstream = tmp_path / "upstream"
        subprocess.run(
            ["git", "init", "-b", "main", str(upstream)],
            capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "-C", str(upstream), "commit", "--allow-empty", "-m", "init"],
            capture_output=True, check=True, env=self._git_env(),
        )
        target = tmp_path / "data" / "github" / "upstream"
        target.parent.mkdir(parents=True)
        subprocess.run(
            ["git", "clone", str(upstream), str(target)],
            capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "-C", str(target), "checkout", "-b", "develop"],
            capture_output=True, check=True,
        )

        result, entries = ensure_git_dep(
            {"url": str(upstream), "branch": "main"}, str(tmp_path / "data")
        )

        assert result.passed
        assert subprocess.run(
            ["git", "-C", str(target), "branch", "--show-current"],
            capture_output=True, text=True, check=True,
        ).stdout.strip() == "main"
        assert any("checked out" in entry for entry in entries)
        assert not any("pulled" in entry for entry in entries)

    def test_engine_phase_uses_converged_result(self, tmp_path):
        from bootstrap_lib import engine

        upstream = tmp_path / "upstream"
        subprocess.run(
            ["git", "init", "-b", "main", str(upstream)],
            capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "-C", str(upstream), "commit", "--allow-empty", "-m", "init"],
            capture_output=True, check=True, env=self._git_env(),
        )
        target = tmp_path / "data" / "github" / "upstream"
        target.parent.mkdir(parents=True)
        subprocess.run(["git", "clone", str(upstream), str(target)], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(target), "checkout", "-b", "develop"],
            capture_output=True, check=True,
        )

        class _Ctx:
            manifest = {"git_deps": [{"url": str(upstream), "branch": "main"}]}
            data_dir = str(tmp_path / "data")
            action_entries = []
            ok_entries = []
            failures = []

            def action(self, message):
                self.action_entries.append(message)

            def ok(self, message):
                self.ok_entries.append(message)

            def fail(self, entry, **failure):
                self.action_entries.append(entry)
                self.failures.append(failure)

        ctx = _Ctx()
        engine._phase_git_deps(ctx)

        assert check_git_dep(str(tmp_path / "data"), str(upstream), "main").passed
        assert ctx.failures == []
        assert any("checked out" in entry for entry in ctx.action_entries)

    def test_failed_recheck_does_not_report_clone_success(self, tmp_path, monkeypatch):
        import bootstrap_lib.git_dep_check as module
        from bootstrap_lib.result import Result

        target = str(tmp_path / "data" / "github" / "repo")
        failed = Result(
            passed=False,
            subject="repo",
            message="repo still not ready",
            remediation_cmd="git clone ...",
            extras={"target_path": target},
        )
        monkeypatch.setattr(module, "check_git_dep", lambda *args, **kwargs: failed)
        monkeypatch.setattr(module, "clone_git_dep", lambda *args, **kwargs: (True, "cloned"))

        result, entries = ensure_git_dep(
            {"url": "https://example.invalid/repo", "branch": "main"},
            str(tmp_path / "data"),
        )

        assert not result.passed
        assert entries
        assert not any(word in entry for entry in entries for word in ("pulled", "cloned"))


def test_failed_checks_carry_a_structured_reason(tmp_path, monkeypatch):
    """ensure_git_dep dispatches on result.reason, never on message text."""
    from bootstrap_lib.git_dep_check import check_git_dep

    missing = check_git_dep(str(tmp_path), "https://x/y/repo.git", "main")
    assert missing.reason == "missing"
    not_git = tmp_path / "github" / "repo"
    not_git.mkdir(parents=True)
    assert check_git_dep(str(tmp_path), "https://x/y/repo.git", "main").reason == "not-git"


def test_pin_prefixes_of_any_length_and_case_match(tmp_path, monkeypatch):
    """A pin is a case-insensitive prefix of HEAD: 5, 12 or 40 chars all work."""
    import subprocess
    from bootstrap_lib import git_dep_check

    head = "0123456789abcdef0123456789abcdef01234567"
    repo = tmp_path / "github" / "repo"
    (repo / ".git").mkdir(parents=True)

    def fake_run(argv, **kwargs):
        class _P:
            returncode = 0
            stderr = ""
        p = _P()
        if "rev-parse" in argv and "HEAD" in argv and "--abbrev-ref" not in argv:
            p.stdout = head + "\n"
        elif "--abbrev-ref" in argv:
            p.stdout = "main\n"
        elif "get-url" in argv:
            p.stdout = "https://x/y/repo.git\n"
        else:
            p.stdout = ""
        return p

    monkeypatch.setattr(git_dep_check.subprocess, "run", fake_run)
    for pin in (head[:5], head[:12], head, head.upper()):
        result = git_dep_check.check_git_dep(str(tmp_path), "https://x/y/repo.git", "main", None, pin)
        assert result.passed, (pin, result.message)
    assert not git_dep_check.check_git_dep(
        str(tmp_path), "https://x/y/repo.git", "main", None, "ffff"
    ).passed
