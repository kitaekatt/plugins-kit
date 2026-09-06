"""Tests for bootstrap_lib/agent_skills_check.py -- the agent_skills_link check.

Proportionate to the design's stated risk surface: quick-exit correctness
(the .agents/skills escape hatch must win before every other seam runs), D2's
git-toplevel scoping, strict boolean validation, the D4 absent-directory
regression, real link creation/verification, and the no-copy /
bounded-cleanup safety guarantees. VCS behavior itself is covered by
test_agent_skills_vcs.py.
"""

import os
import subprocess
import sys

import pytest

from bootstrap.link_compat import CAN_SYMLINK, requires_symlinks
from bootstrap_lib import agent_skills_check as asc
from bootstrap_lib.codex import CodexDetection


def _git(*args, cwd):
    subprocess.run(["git"] + list(args), cwd=cwd, check=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _init_repo(path):
    os.makedirs(path, exist_ok=True)
    _git("init", "-q", cwd=path)
    return path


def _make_source(root, name="demo"):
    skill_dir = os.path.join(root, ".claude", "skills", name)
    os.makedirs(skill_dir, exist_ok=True)
    with open(os.path.join(skill_dir, "SKILL.md"), "w") as f:
        f.write("---\nname: demo\ndescription: demo\n---\n")


@pytest.fixture(autouse=True)
def _no_real_codex(monkeypatch):
    """Never let a test accidentally spawn the real codex binary."""
    monkeypatch.setattr(
        asc, "detect_codex",
        lambda: CodexDetection(available=False, reason="not probed in this test"),
    )


def _forbid_subprocess(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("subprocess.run must not be called past the quick exit")
    monkeypatch.setattr(asc.subprocess, "run", _boom)


def _forbid_codex(monkeypatch):
    def _boom():
        raise AssertionError("detect_codex must not be called past the quick exit")
    monkeypatch.setattr(asc, "detect_codex", _boom)


# ---------------------------------------------------------------------------
# .agents/skills quick exit -- must win before every other seam
# ---------------------------------------------------------------------------


def _skills(root):
    return os.path.join(root, ".agents", "skills")


class TestQuickExit:
    """The sentinel is the CHILD. `.agents/` is shared with Codex's own
    `.agents/plugins/` config, so its mere existence says nothing about
    whether the skills link is wanted -- see the module docstring."""

    def test_existing_directory(self, tmp_path, monkeypatch):
        root = str(tmp_path)
        os.makedirs(_skills(root))
        _forbid_subprocess(monkeypatch)
        _forbid_codex(monkeypatch)
        result = asc.check_project_agent_skills_link(root, None)
        assert result.status == "existing"

    def test_existing_file(self, tmp_path, monkeypatch):
        root = str(tmp_path)
        os.makedirs(os.path.join(root, ".agents"))
        with open(_skills(root), "w") as f:
            f.write("not a directory")
        _forbid_subprocess(monkeypatch)
        _forbid_codex(monkeypatch)
        result = asc.check_project_agent_skills_link(root, None)
        assert result.status == "existing"

    @requires_symlinks
    def test_existing_symlink(self, tmp_path, monkeypatch):
        root = str(tmp_path)
        target = os.path.join(root, "elsewhere")
        os.makedirs(target)
        os.makedirs(os.path.join(root, ".agents"))
        os.symlink(target, _skills(root), target_is_directory=True)
        _forbid_subprocess(monkeypatch)
        _forbid_codex(monkeypatch)
        result = asc.check_project_agent_skills_link(root, None)
        assert result.status == "existing"

    @requires_symlinks
    def test_existing_dangling_symlink(self, tmp_path, monkeypatch):
        """lstat, not stat: a dangling link is still somebody's link and is
        not ours to repair."""
        root = str(tmp_path)
        os.makedirs(os.path.join(root, ".agents"))
        os.symlink(os.path.join(root, "nonexistent"), _skills(root))
        _forbid_subprocess(monkeypatch)
        _forbid_codex(monkeypatch)
        result = asc.check_project_agent_skills_link(root, None)
        assert result.status == "existing"

    def test_bare_agents_directory_does_not_quick_exit(self, tmp_path, monkeypatch):
        """The regression. A project that adopts `.agents/plugins/` for
        repo-level Codex config used to be skipped forever, and the only
        documented recovery -- "delete .agents to rebuild" -- meant deleting
        that config. The parent is not the sentinel."""
        repo = _init_repo(str(tmp_path / "repo"))
        _make_source(repo)
        os.makedirs(os.path.join(repo, ".agents", "plugins"))
        with open(os.path.join(repo, ".agents", "plugins",
                               "marketplace.json"), "w") as f:
            f.write("{}\n")
        monkeypatch.setattr(
            asc, "detect_codex",
            lambda: CodexDetection(available=True, reason="fake"))

        result = asc.check_project_agent_skills_link(repo, None)

        assert result.status == "fixable"

    def test_agents_as_a_file_reports_the_os_error(self, tmp_path, monkeypatch):
        """ENOTDIR on the child. Unusual enough that the OS message beats a
        status of its own -- but it must be a failure, not a silent skip."""
        root = str(tmp_path)
        with open(os.path.join(root, ".agents"), "w") as f:
            f.write("not a directory")
        _forbid_subprocess(monkeypatch)
        _forbid_codex(monkeypatch)
        result = asc.check_project_agent_skills_link(root, None)
        assert result.status == "lstat_error"
        assert result.detail


# ---------------------------------------------------------------------------
# D2: link only when project_dir IS the git repository root
# ---------------------------------------------------------------------------


class TestRootScoping:
    def test_no_git_repository_skips(self, tmp_path, monkeypatch):
        _forbid_codex(monkeypatch)
        result = asc.check_project_agent_skills_link(str(tmp_path), None)
        assert result.status == "not_worktree"

    def test_nested_directory_skips(self, tmp_path, monkeypatch):
        repo = _init_repo(str(tmp_path / "repo"))
        nested = os.path.join(repo, "sub")
        os.makedirs(nested)
        _forbid_codex(monkeypatch)
        result = asc.check_project_agent_skills_link(nested, None)
        assert result.status == "not_toplevel"
        assert os.path.normcase(os.path.normpath(result.toplevel)) == \
            os.path.normcase(os.path.normpath(repo))

    def test_repository_root_proceeds_past_root_check(self, tmp_path):
        repo = _init_repo(str(tmp_path / "repo"))
        # The autouse fixture reports Codex unavailable -- reaching that
        # status (rather than not_worktree/not_toplevel) proves the root
        # check passed and D2 scoping did not stop us early.
        result = asc.check_project_agent_skills_link(repo, None)
        assert result.status == "codex_unavailable"


# ---------------------------------------------------------------------------
# Strict boolean validation
# ---------------------------------------------------------------------------


class TestOptionValidation:
    def _repo_with_source(self, tmp_path):
        repo = _init_repo(str(tmp_path / "repo"))
        _make_source(repo)
        return repo

    @pytest.mark.parametrize("value", ["true", 0, 1, 1.0, [], {}])
    def test_non_bool_is_invalid(self, tmp_path, value):
        repo = self._repo_with_source(tmp_path)
        result = asc.check_project_agent_skills_link(repo, value)
        assert result.status == "invalid_option"

    def test_false_is_opt_out(self, tmp_path):
        repo = self._repo_with_source(tmp_path)
        result = asc.check_project_agent_skills_link(repo, False)
        assert result.status == "opt_out"

    def test_absent_is_enabled(self, tmp_path, monkeypatch):
        repo = self._repo_with_source(tmp_path)
        monkeypatch.setattr(
            asc, "detect_codex",
            lambda: CodexDetection(available=True, reason="fake"),
        )
        result = asc.check_project_agent_skills_link(repo, None)
        assert result.status == "fixable"

    def test_true_is_enabled(self, tmp_path, monkeypatch):
        repo = self._repo_with_source(tmp_path)
        monkeypatch.setattr(
            asc, "detect_codex",
            lambda: CodexDetection(available=True, reason="fake"),
        )
        result = asc.check_project_agent_skills_link(repo, True)
        assert result.status == "fixable"


# ---------------------------------------------------------------------------
# Codex availability and source inspection
# ---------------------------------------------------------------------------


class TestCodexAndSource:
    def test_codex_unavailable_skips(self, tmp_path, monkeypatch):
        repo = _init_repo(str(tmp_path / "repo"))
        _make_source(repo)
        monkeypatch.setattr(
            asc, "detect_codex",
            lambda: CodexDetection(available=False, reason="not found"),
        )
        result = asc.check_project_agent_skills_link(repo, None)
        assert result.status == "codex_unavailable"
        assert result.detail == "not found"

    def test_missing_source_skips(self, tmp_path, monkeypatch):
        repo = _init_repo(str(tmp_path / "repo"))
        monkeypatch.setattr(
            asc, "detect_codex",
            lambda: CodexDetection(available=True, reason="fake"),
        )
        result = asc.check_project_agent_skills_link(repo, None)
        assert result.status == "source_missing"

    def test_empty_source_skips(self, tmp_path, monkeypatch):
        repo = _init_repo(str(tmp_path / "repo"))
        os.makedirs(os.path.join(repo, ".claude", "skills"))
        monkeypatch.setattr(
            asc, "detect_codex",
            lambda: CodexDetection(available=True, reason="fake"),
        )
        result = asc.check_project_agent_skills_link(repo, None)
        assert result.status == "source_empty"

    def test_nonempty_source_is_fixable(self, tmp_path, monkeypatch):
        repo = _init_repo(str(tmp_path / "repo"))
        _make_source(repo)
        monkeypatch.setattr(
            asc, "detect_codex",
            lambda: CodexDetection(available=True, reason="fake"),
        )
        result = asc.check_project_agent_skills_link(repo, None)
        assert result.status == "fixable"


# ---------------------------------------------------------------------------
# D4: the absent-directory regression the naive check-ignore probe misses
# ---------------------------------------------------------------------------


class TestD4TrailingSlashRegression:
    def test_naive_probe_without_slash_fails_on_a_clean_repo(self, tmp_path):
        """Reproduces the D4 defect directly against real git: querying the
        absent .agents/skills path WITHOUT a trailing slash reports "not
        ignored" even though a matching directory rule exists -- because git
        cannot know an absent path is a directory. This is why
        _git_check_ignore must always be called with a trailing-slash probe
        (as _apply_git_exclusion does)."""
        repo = _init_repo(str(tmp_path / "repo"))
        exclude_path = os.path.join(repo, ".git", "info", "exclude")
        os.makedirs(os.path.dirname(exclude_path), exist_ok=True)
        with open(exclude_path, "a") as f:
            f.write("/.agents/skills/\n")

        # Confirm .agents/skills genuinely does not exist yet.
        assert not os.path.exists(os.path.join(repo, ".agents"))

        naive = asc._git_check_ignore(repo, ".agents/skills")
        correct = asc._git_check_ignore(repo, ".agents/skills/")

        assert naive is False, (
            "expected the naive (no trailing slash) probe to report "
            "'not ignored' on an absent path -- if this now fails, git's "
            "behavior has changed and the D4 workaround may be obsolete"
        )
        assert correct is True

    def test_apply_git_exclusion_converges_on_a_clean_repo(self, tmp_path):
        """The real regression test: a fresh repo with no .agents/skills on
        disk must still converge on the FIRST attempt."""
        repo = _init_repo(str(tmp_path / "repo"))
        status, detail = asc._apply_git_exclusion(repo)
        assert status == "added"
        assert "Git exclusion added to" in detail

        # Re-running now finds it already effective.
        status2, detail2 = asc._apply_git_exclusion(repo)
        assert status2 == "effective"


# ---------------------------------------------------------------------------
# Link creation: real symlink / real junction, no-copy, bounded cleanup
# ---------------------------------------------------------------------------


class TestAdoptsExistingAgentsDir:
    """`.agents` is created only when absent, adopted when present, and never
    removed unless THIS attempt made it."""

    @requires_symlinks
    def test_links_into_an_existing_agents_directory(self, tmp_path):
        repo = _init_repo(str(tmp_path / "repo"))
        _make_source(repo)
        config = os.path.join(repo, ".agents", "plugins", "marketplace.json")
        os.makedirs(os.path.dirname(config))
        with open(config, "w") as f:
            f.write("{}\n")

        result = asc.create_agent_skills_link(repo)

        assert result.ok, result.detail
        assert os.path.islink(os.path.join(repo, ".agents", "skills"))
        assert os.path.isfile(config), "adoption must not disturb existing content"

    def test_a_failed_link_leaves_an_adopted_agents_alone(self, tmp_path, monkeypatch):
        """The half that adoption makes dangerous: bounded cleanup removes
        the `.agents` it created, and an adopted one is not that."""
        repo = _init_repo(str(tmp_path / "repo"))
        _make_source(repo)
        config = os.path.join(repo, ".agents", "plugins", "marketplace.json")
        os.makedirs(os.path.dirname(config))
        with open(config, "w") as f:
            f.write("{}\n")

        def _fake_symlink(*a, **k):
            err = OSError("access denied")
            err.winerror = 5  # NOT the privilege signal -- no junction fallback
            raise err

        monkeypatch.setattr(os, "symlink", _fake_symlink)
        result = asc.create_agent_skills_link(repo)

        assert not result.ok
        assert not os.path.lexists(os.path.join(repo, ".agents", "skills"))
        assert os.path.isfile(config), (
            "cleanup must never remove an .agents this attempt did not create")

    def test_agents_as_a_file_is_a_mkdir_failure(self, tmp_path):
        """The check half reports ENOTDIR and never reaches the fixer, so
        this is the race/direct-call guard."""
        repo = _init_repo(str(tmp_path / "repo"))
        _make_source(repo)
        with open(os.path.join(repo, ".agents"), "w") as f:
            f.write("not a directory")

        result = asc.create_agent_skills_link(repo)

        assert not result.ok
        assert result.status == "mkdir_failed"
        assert "not a directory" in result.detail

    @requires_symlinks
    def test_a_link_appearing_first_is_reported_as_a_race(self, tmp_path):
        """Adoption moves the race window from `.agents` down to the child,
        which is the thing that actually has one owner."""
        repo = _init_repo(str(tmp_path / "repo"))
        _make_source(repo)
        os.makedirs(os.path.join(repo, ".agents"))
        os.symlink(os.path.join(repo, ".claude", "skills"),
                   os.path.join(repo, ".agents", "skills"),
                   target_is_directory=True)

        result = asc.create_agent_skills_link(repo)

        assert not result.ok
        assert result.status == "race_existing"
        assert os.path.islink(os.path.join(repo, ".agents", "skills")), (
            "the loser of a race must not delete the winner's link")


class TestLinkCreation:
    @requires_symlinks
    def test_real_symlink_created_and_verified(self, tmp_path):
        repo = _init_repo(str(tmp_path / "repo"))
        _make_source(repo)
        result = asc.create_agent_skills_link(repo)
        assert result.ok, result.detail
        assert result.mechanism == "directory symlink"
        link_path = os.path.join(repo, ".agents", "skills")
        assert os.path.islink(link_path)
        assert os.path.samefile(link_path, os.path.join(repo, ".claude", "skills"))

    @pytest.mark.skipif(sys.platform != "win32", reason="junction fallback is Windows-only")
    def test_windows_junction_fallback(self, tmp_path, monkeypatch):
        """Forces the symlink attempt to fail with the privilege signal
        (WinError 1314) so the real _winapi.CreateJunction fallback runs,
        deterministically, regardless of whether this machine actually has
        symlink privilege."""
        repo = _init_repo(str(tmp_path / "repo"))
        _make_source(repo)

        real_symlink = os.symlink

        def _fake_symlink(*a, **k):
            err = OSError("privilege not held")
            err.winerror = 1314
            raise err

        monkeypatch.setattr(os, "symlink", _fake_symlink)
        result = asc.create_agent_skills_link(repo)
        monkeypatch.setattr(os, "symlink", real_symlink)

        assert result.ok, result.detail
        assert result.mechanism == "NTFS junction"
        link_path = os.path.join(repo, ".agents", "skills")
        from pathlib import Path
        assert Path(link_path).is_junction()
        assert os.path.samefile(link_path, os.path.join(repo, ".claude", "skills"))

    def test_both_mechanisms_fail_no_copy_created(self, tmp_path, monkeypatch):
        repo = _init_repo(str(tmp_path / "repo"))
        _make_source(repo)

        def _fake_symlink(*a, **k):
            err = OSError("access denied")
            err.winerror = 5  # NOT the privilege signal -- must not fall back
            raise err

        monkeypatch.setattr(os, "symlink", _fake_symlink)
        result = asc.create_agent_skills_link(repo)

        assert not result.ok
        link_path = os.path.join(repo, ".agents", "skills")
        assert not os.path.lexists(link_path), "no copy fallback must ever be created"
        # Bounded cleanup: the freshly created empty .agents is removed too.
        assert not os.path.lexists(os.path.join(repo, ".agents")), (
            "a failed creation must remove the .agents directory it made"
        )

    def test_failed_creation_removes_only_what_it_made(self, tmp_path, monkeypatch):
        """A pre-existing sibling of .agents in the project root, and any
        content the VCS phase legitimately wrote outside .agents, must
        survive a link-creation failure untouched."""
        repo = _init_repo(str(tmp_path / "repo"))
        _make_source(repo)
        sentinel = os.path.join(repo, "keep-me.txt")
        with open(sentinel, "w") as f:
            f.write("do not touch")

        def _fake_symlink(*a, **k):
            err = OSError("access denied")
            err.winerror = 5
            raise err

        monkeypatch.setattr(os, "symlink", _fake_symlink)
        result = asc.create_agent_skills_link(repo)

        assert not result.ok
        assert os.path.isfile(sentinel)
        with open(sentinel) as f:
            assert f.read() == "do not touch"
        # The git exclusion this attempt wrote (info/exclude, outside
        # .agents) is allowed to remain -- it is harmless and makes the next
        # attempt cheaper.
        exclude_path = os.path.join(repo, ".git", "info", "exclude")
        with open(exclude_path) as f:
            assert "/.agents/skills/" in f.read()

    def test_vcs_failure_removes_only_the_empty_agents_dir(self, tmp_path, monkeypatch):
        repo = _init_repo(str(tmp_path / "repo"))
        _make_source(repo)
        monkeypatch.setattr(asc, "_apply_vcs_exclusions", lambda root: (False, "boom"))
        result = asc.create_agent_skills_link(repo)
        assert not result.ok
        assert result.status == "vcs_failed"
        assert not os.path.lexists(os.path.join(repo, ".agents"))

    @requires_symlinks
    def test_verification_failure_removes_new_link_and_is_retryable(self, tmp_path, monkeypatch):
        repo = _init_repo(str(tmp_path / "repo"))
        _make_source(repo)
        monkeypatch.setattr(
            asc, "detect_codex",
            lambda: CodexDetection(available=True, reason="fake"),
        )
        monkeypatch.setattr(asc, "_verify_link", lambda link, source: "forced verification failure")

        result = asc.create_agent_skills_link(repo)

        assert not result.ok
        assert result.status == "verify_error"
        assert not os.path.lexists(_skills(repo))
        assert asc.check_project_agent_skills_link(repo, None).status == "fixable"
