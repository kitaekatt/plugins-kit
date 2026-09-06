"""Tests for the VCS half of bootstrap_lib/agent_skills_check.py.

Git coverage uses a REAL disposable git repository, never a mocked
subprocess -- git's own quoting/pathspec/ignore-matching behavior is exactly
what D3/D4/D5 depend on. Perforce coverage mocks `p4` (no server is
available in this environment; see carryover-findings.md) but exercises the
module's OWN command-construction and output-parsing logic against recorded
real-world outputs from the design's read-only inspection transcript.
"""

import os
import subprocess

import pytest

from bootstrap_lib import agent_skills_check as asc


def _git(*args, cwd):
    subprocess.run(["git"] + list(args), cwd=cwd, check=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _init_repo(path):
    os.makedirs(path, exist_ok=True)
    _git("init", "-q", cwd=path)
    return path


# ---------------------------------------------------------------------------
# Git: info/exclude, not .gitignore; D3 anchored rule; D5 literal pathspecs
# ---------------------------------------------------------------------------


class TestGitExclusion:
    def test_adds_anchored_rule_to_info_exclude(self, tmp_path):
        repo = _init_repo(str(tmp_path / "repo"))
        status, detail = asc._apply_git_exclusion(repo)
        assert status == "added"

        exclude_path = os.path.join(repo, ".git", "info", "exclude")
        assert os.path.isfile(exclude_path)
        assert "Git exclusion added to" in detail
        assert detail.endswith(("exclude", "exclude\n")) or "info" in detail
        with open(exclude_path) as f:
            content = f.read()
        # D3: anchors the skills CHILD, not the whole of .agents/.
        assert "/.agents/skills/\n" in content
        assert "# plugins-kit bootstrap: generated agent skills link" in content
        assert "/.agents/\n" not in content.replace("/.agents/skills/\n", "")

    def test_gitignore_is_never_touched(self, tmp_path):
        repo = _init_repo(str(tmp_path / "repo"))
        gitignore = os.path.join(repo, ".gitignore")
        with open(gitignore, "w") as f:
            f.write("*.log\n")
        asc._apply_git_exclusion(repo)
        with open(gitignore) as f:
            assert f.read() == "*.log\n"
        assert not os.path.exists(os.path.join(repo, ".claude"))

    def test_already_effective_writes_nothing(self, tmp_path):
        repo = _init_repo(str(tmp_path / "repo"))
        first_status, _ = asc._apply_git_exclusion(repo)
        assert first_status == "added"
        exclude_path = os.path.join(repo, ".git", "info", "exclude")
        before = os.path.getmtime(exclude_path)

        second_status, second_detail = asc._apply_git_exclusion(repo)
        assert second_status == "effective"
        assert second_detail == "Git exclusion already effective"
        assert os.path.getmtime(exclude_path) == before

    def test_no_git_repository_is_a_noop(self, tmp_path):
        status, detail = asc._apply_git_exclusion(str(tmp_path))
        assert status == "none"
        assert detail == ""

    def test_literal_pathspec_used_for_tracking_query(self, tmp_path, monkeypatch):
        """D5: git ls-files must use --literal-pathspecs so a root path
        containing *, ?, or [ is never misread as a glob."""
        repo = _init_repo(str(tmp_path / "repo"))
        seen_args = []
        real_run_git = asc._run_git

        def _spy(root, args):
            seen_args.append(list(args))
            return real_run_git(root, args)

        monkeypatch.setattr(asc, "_run_git", _spy)
        asc._apply_git_exclusion(repo)

        ls_files_calls = [a for a in seen_args if "ls-files" in a]
        assert ls_files_calls, "expected a git ls-files call"
        assert "--literal-pathspecs" in ls_files_calls[0]

    def test_tracked_path_refuses_to_write(self, tmp_path):
        repo = _init_repo(str(tmp_path / "repo"))
        agents_skills = os.path.join(repo, ".agents", "skills")
        os.makedirs(agents_skills)
        with open(os.path.join(agents_skills, "placeholder.txt"), "w") as f:
            f.write("tracked on purpose for this test")
        _git("add", "-A", cwd=repo)

        status, detail = asc._apply_git_exclusion(repo)
        assert status == "error"
        assert "tracked by git" in detail

        exclude_path = os.path.join(repo, ".git", "info", "exclude")
        assert not os.path.isfile(exclude_path) or "/.agents/skills/" not in open(exclude_path).read()


# ---------------------------------------------------------------------------
# D4: the trailing-slash check-ignore probe, reproduced against real git
# ---------------------------------------------------------------------------


class TestD4Probe:
    def test_probe_without_trailing_slash_misreports_an_absent_directory(self, tmp_path):
        repo = _init_repo(str(tmp_path / "repo"))
        exclude_path = os.path.join(repo, ".git", "info", "exclude")
        os.makedirs(os.path.dirname(exclude_path), exist_ok=True)
        with open(exclude_path, "w") as f:
            f.write("/.agents/skills/\n")

        assert not os.path.exists(os.path.join(repo, ".agents", "skills"))
        assert asc._git_check_ignore(repo, ".agents/skills") is False
        assert asc._git_check_ignore(repo, ".agents/skills/") is True


# ---------------------------------------------------------------------------
# Perforce: command parsing over recorded/synthetic outputs (no live server)
# ---------------------------------------------------------------------------


class _Proc:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class TestP4CommandParsing:
    def test_ignores_parses_ignored(self, monkeypatch):
        monkeypatch.setattr(
            asc, "_run_p4",
            lambda args, timeout=None: _Proc(
                stdout="D:/x/.agents/skills ignored by D:/x\\.p4ignore:1:.agents/skills\n"
            ),
        )
        assert asc._p4_ignores("D:/x", "D:/x/.agents/skills") is True

    def test_ignores_parses_not_ignored(self, monkeypatch):
        monkeypatch.setattr(
            asc, "_run_p4",
            lambda args, timeout=None: _Proc(stdout="D:/x/.agents/skills not ignored\n"),
        )
        assert asc._p4_ignores("D:/x", "D:/x/.agents/skills") is False

    def test_where_parses_unmapped(self, monkeypatch):
        monkeypatch.setattr(
            asc, "_run_p4",
            lambda args, timeout=None: _Proc(
                stdout="", stderr="D:/x/.agents/skills/... is not in client view.\n",
                returncode=1,
            ),
        )
        assert asc._p4_where_mapped("D:/x", "D:/x/.agents/skills") is False

    def test_where_unreachable_server_is_none(self, monkeypatch):
        monkeypatch.setattr(
            asc, "_run_p4",
            lambda args, timeout=None: _Proc(
                stdout="",
                stderr="Perforce client error:\n    TCP connect to perforce:1666 failed.\n",
                returncode=1,
            ),
        )
        assert asc._p4_where_mapped("D:/x", "D:/x/.agents/skills") is None

    def test_no_p4_and_no_local_evidence_is_noop(self, tmp_path, monkeypatch):
        monkeypatch.setattr(asc.shutil, "which", lambda name: None)
        status, detail = asc._apply_p4_exclusion(str(tmp_path))
        assert status == "none"
        assert detail == ""

    def test_p4_unavailable_with_local_evidence_fails(self, tmp_path, monkeypatch):
        with open(os.path.join(tmp_path, ".p4ignore"), "w") as f:
            f.write(".claude/settings.local.json\n")
        monkeypatch.setattr(asc.shutil, "which", lambda name: None)
        status, detail = asc._apply_p4_exclusion(str(tmp_path))
        assert status == "error"
        assert "p4 CLI is unavailable" in detail

    def test_unset_p4ignore_creates_p4ignore_txt_with_both_rules(self, tmp_path, monkeypatch):
        root = str(tmp_path)
        monkeypatch.setattr(asc.shutil, "which", lambda name: "p4" if name == "p4" else None)
        monkeypatch.setattr(asc, "_p4_where_mapped", lambda r, t: True)

        calls = {"n": 0}

        def _fake_ignores(r, path):
            # First call: pre-write check on .agents/skills -> not ignored.
            # Post-write calls (both paths) -> ignored.
            calls["n"] += 1
            if calls["n"] == 1:
                return False
            return True

        monkeypatch.setattr(asc, "_p4_ignores", _fake_ignores)
        monkeypatch.setattr(asc, "_p4_set_p4ignore", lambda r: None)

        status, detail = asc._apply_p4_exclusion(root)
        assert status == "added"

        p4ignore_txt = os.path.join(root, "p4ignore.txt")
        assert os.path.isfile(p4ignore_txt)
        with open(p4ignore_txt) as f:
            content = f.read()
        assert "/.agents/skills/" in content
        assert "/p4ignore.txt" in content

    def test_explicit_p4ignore_honored_and_external_files_never_touched(self, tmp_path, monkeypatch):
        root = str(tmp_path)
        monkeypatch.setattr(asc.shutil, "which", lambda name: "p4" if name == "p4" else None)
        monkeypatch.setattr(asc, "_p4_where_mapped", lambda r, t: True)
        monkeypatch.setattr(asc, "_p4_ignores", lambda r, path: False)
        monkeypatch.setattr(asc, "_p4_set_p4ignore", lambda r: ["custom.p4ignore"])

        # An external/global ignore file elsewhere must never be touched --
        # only a MISSING root-local file named by P4IGNORE is written.
        outside = tmp_path.parent / "global.p4ignore"
        outside.write_text("unrelated\n")

        # _p4_ignores is stubbed to always report False, so the post-write
        # authoritative check also reports False -> the write is judged to
        # have failed verification. That is fine for this test: what matters
        # is WHICH file got created.
        asc._apply_p4_exclusion(root)

        created = os.path.join(root, "custom.p4ignore")
        assert os.path.isfile(created)
        with open(created) as f:
            content = f.read()
        assert "/.agents/skills/" in content
        assert "/custom.p4ignore" in content
        assert outside.read_text() == "unrelated\n"

    def test_absolute_missing_p4ignore_is_rejected(self, tmp_path, monkeypatch):
        root = str(tmp_path / "project")
        os.makedirs(root)
        outside = tmp_path / "outside.p4ignore"
        monkeypatch.setattr(asc.shutil, "which", lambda name: "p4" if name == "p4" else None)
        monkeypatch.setattr(asc, "_p4_where_mapped", lambda r, t: True)
        monkeypatch.setattr(asc, "_p4_ignores", lambda r, path: False)
        monkeypatch.setattr(asc, "_p4_set_p4ignore", lambda r: [str(outside)])

        status, detail = asc._apply_p4_exclusion(root)

        assert status == "error"
        assert "outside the project" in detail
        assert not outside.exists()

    def test_p4_ignore_file_is_excluded_from_git(self, tmp_path, monkeypatch):
        root = _init_repo(str(tmp_path / "project"))
        monkeypatch.setattr(asc.shutil, "which", lambda name: "p4" if name == "p4" else None)
        monkeypatch.setattr(asc, "_p4_where_mapped", lambda r, t: True)
        calls = {"count": 0}

        def _fake_ignores(r, path):
            calls["count"] += 1
            return calls["count"] > 1

        monkeypatch.setattr(asc, "_p4_ignores", _fake_ignores)
        monkeypatch.setattr(asc, "_p4_set_p4ignore", lambda r: None)

        status, _detail = asc._apply_p4_exclusion(root)

        assert status == "added"
        clean = subprocess.run(
            ["git", "-C", root, "status", "--porcelain"],
            capture_output=True, text=True, check=True,
        )
        assert clean.stdout == ""

    def test_home_p4ignore_does_not_affect_git_project(self, tmp_path, monkeypatch):
        root = _init_repo(str(tmp_path / "project"))
        (tmp_path / ".p4ignore").write_text("outside\n")
        monkeypatch.setattr(asc.shutil, "which", lambda name: None)

        status, detail = asc._apply_p4_exclusion(root)

        assert status == "none"
        assert detail == ""

    def test_already_ignored_writes_nothing(self, tmp_path, monkeypatch):
        root = str(tmp_path)
        monkeypatch.setattr(asc.shutil, "which", lambda name: "p4" if name == "p4" else None)
        monkeypatch.setattr(asc, "_p4_where_mapped", lambda r, t: True)
        monkeypatch.setattr(asc, "_p4_ignores", lambda r, path: True)

        status, detail = asc._apply_p4_exclusion(root)
        assert status == "effective"
        assert detail == "P4 exclusion already effective"
        assert not os.path.exists(os.path.join(root, "p4ignore.txt"))

    def test_no_p4_binary_no_workspace_is_noop(self, tmp_path, monkeypatch):
        monkeypatch.setattr(asc.shutil, "which", lambda name: None)
        status, detail = asc._apply_p4_exclusion(str(tmp_path))
        assert status == "none"
