"""Tests for bootstrap_lib.project_links_check and its engine wiring (Step 3d4).

Covers every check status, the fixer's refusals (wrong target and non-link are
never replaced; a path git does not ignore is never linked), project-layer
provenance, parent-symlink escape, duplicate rejection, and the engine step
run against a real ``git worktree add`` checkout.
"""

import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from bootstrap_lib import agent_skills_check as asc
from bootstrap_lib import engine
from bootstrap_lib import project_links_check as plc
from bootstrap_lib.codex_detection import CodexDetection

from .link_compat import CAN_SYMLINK

pytestmark = pytest.mark.skipif(
    not CAN_SYMLINK, reason="symlink privilege unavailable")

_GIT_ID = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]


def _git(*args, cwd):
    subprocess.run(["git"] + _GIT_ID + list(args), cwd=cwd, check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _write_manifest(project, links):
    claude = os.path.join(project, ".claude")
    os.makedirs(claude, exist_ok=True)
    with open(os.path.join(claude, "bootstrap.json"), "w") as f:
        json.dump({"links": links}, f)


def _repo(tmp_path, ignore="dev/tasks\n", links=None, name="repo"):
    repo = str(tmp_path / name)
    os.makedirs(repo)
    _git("init", "-q", cwd=repo)
    with open(os.path.join(repo, ".gitignore"), "w") as f:
        f.write(ignore)
    if links is not None:
        _write_manifest(repo, links)
    return repo


@pytest.fixture
def target(tmp_path):
    t = tmp_path / "store" / "tasks"
    t.mkdir(parents=True)
    return str(t)


def _decl(path="dev/tasks", tgt="/abs/target"):
    return plc.LinkDeclaration(path, tgt)


class TestStatuses:
    def test_fixable_when_absent_ignored_and_target_exists(self, tmp_path, target):
        repo = _repo(tmp_path)
        out = plc.check_project_link(repo, _decl(tgt=target))
        assert out.status == "fixable"

    def test_ok_same_after_fix(self, tmp_path, target):
        repo = _repo(tmp_path)
        fix = plc.create_project_link(repo, _decl(tgt=target))
        assert fix.ok and fix.status == "created"
        assert os.path.islink(os.path.join(repo, "dev", "tasks"))
        assert plc.check_project_link(repo, _decl(tgt=target)).status == "ok_same"

    def test_wrong_target_is_reported_and_never_replaced(self, tmp_path, target):
        repo = _repo(tmp_path)
        other = tmp_path / "other"
        other.mkdir()
        os.makedirs(os.path.join(repo, "dev"))
        link = os.path.join(repo, "dev", "tasks")
        os.symlink(str(other), link)
        assert plc.check_project_link(repo, _decl(tgt=target)).status == "wrong_target"
        fix = plc.create_project_link(repo, _decl(tgt=target))
        assert not fix.ok and fix.status == "not_fixable"
        assert os.path.realpath(link) == os.path.realpath(str(other))

    def test_not_link_is_reported_and_content_preserved(self, tmp_path, target):
        repo = _repo(tmp_path)
        real = os.path.join(repo, "dev", "tasks")
        os.makedirs(real)
        keep = os.path.join(real, "keep.txt")
        open(keep, "w").write("mine")
        assert plc.check_project_link(repo, _decl(tgt=target)).status == "not_link"
        assert not plc.create_project_link(repo, _decl(tgt=target)).ok
        assert open(keep).read() == "mine" and not os.path.islink(real)

    def test_not_link_for_a_regular_file(self, tmp_path, target):
        repo = _repo(tmp_path)
        os.makedirs(os.path.join(repo, "dev"))
        open(os.path.join(repo, "dev", "tasks"), "w").write("x")
        assert plc.check_project_link(repo, _decl(tgt=target)).status == "not_link"

    def test_target_missing(self, tmp_path):
        repo = _repo(tmp_path)
        out = plc.check_project_link(repo, _decl(tgt=str(tmp_path / "nope")))
        assert out.status == "target_missing"

    def test_target_that_is_a_file_is_missing(self, tmp_path):
        repo = _repo(tmp_path)
        f = tmp_path / "afile"
        f.write_text("x")
        assert plc.check_project_link(repo, _decl(tgt=str(f))).status == "target_missing"

    def test_not_ignored_is_refused_and_nothing_is_created(self, tmp_path, target):
        repo = _repo(tmp_path, ignore="something-else\n")
        assert plc.check_project_link(repo, _decl(tgt=target)).status == "not_ignored"
        fix = plc.create_project_link(repo, _decl(tgt=target))
        assert not fix.ok and fix.status == "not_fixable"
        assert not os.path.lexists(os.path.join(repo, "dev"))

    def test_not_a_git_repo_counts_as_not_ignored(self, tmp_path, target):
        plain = tmp_path / "plain"
        plain.mkdir()
        assert plc.check_project_link(str(plain), _decl(tgt=target)).status == "not_ignored"

    def test_target_inside_project_is_invalid(self, tmp_path):
        repo = _repo(tmp_path)
        inner = os.path.join(repo, "inner")
        os.makedirs(inner)
        out = plc.check_project_link(repo, _decl(tgt=inner))
        assert out.status == "invalid" and "outside the project" in out.detail


class TestParentSafety:
    def test_parent_symlink_escape_is_refused(self, tmp_path, target):
        repo = _repo(tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir()
        os.symlink(str(outside), os.path.join(repo, "dev"))
        out = plc.check_project_link(repo, _decl(tgt=target))
        assert out.status == "invalid" and "is a link" in out.detail
        assert not plc.create_project_link(repo, _decl(tgt=target)).ok
        assert os.listdir(str(outside)) == []

    def test_parent_that_is_a_file_is_invalid(self, tmp_path, target):
        repo = _repo(tmp_path)
        open(os.path.join(repo, "dev"), "w").write("x")
        assert plc.check_project_link(repo, _decl(tgt=target)).status == "invalid"

    def test_missing_parents_are_created_and_rolled_back_on_failure(
            self, tmp_path, target, monkeypatch):
        repo = _repo(tmp_path, ignore="a/b/c\n")
        monkeypatch.setattr(plc, "_create_link", lambda t, p: (False, "", "boom"))
        fix = plc.create_project_link(repo, _decl("a/b/c", target))
        assert fix.status == "link_failed"
        assert not os.path.lexists(os.path.join(repo, "a"))


class TestDeclarationValidation:
    @pytest.mark.parametrize("bad", [
        "/abs/x", "../x", "a/../x", "a//b", "./a", "a/./b", "a/", "",
        "a\\b", "C:/x", ".git/hooks", "a/.GIT/x",
    ])
    def test_bad_paths_are_invalid(self, tmp_path, target, bad):
        repo = _repo(tmp_path, links=[{"path": bad, "target": target}])
        read = plc.read_project_links(repo)
        assert read.declarations == ()
        assert len(read.invalid) == 1 and read.invalid[0].status == "invalid"

    def test_unresolved_variable_in_target_fails_loudly(self, tmp_path, monkeypatch):
        monkeypatch.delenv("PLC_NOT_SET", raising=False)
        repo = _repo(tmp_path, links=[{"path": "dev/tasks", "target": "$PLC_NOT_SET/t"}])
        read = plc.read_project_links(repo)
        assert read.declarations == ()
        assert "unresolved variable" in read.invalid[0].detail

    def test_target_is_expanded_with_env(self, tmp_path, target, monkeypatch):
        monkeypatch.setenv("PLC_ROOT", os.path.dirname(target))
        repo = _repo(tmp_path, links=[{"path": "dev/tasks", "target": "$PLC_ROOT/tasks"}])
        read = plc.read_project_links(repo)
        assert [d.target for d in read.declarations] == [os.path.normpath(target)]

    def test_relative_target_is_invalid(self, tmp_path):
        repo = _repo(tmp_path, links=[{"path": "dev/tasks", "target": "rel/dir"}])
        assert plc.read_project_links(repo).invalid[0].status == "invalid"

    def test_unknown_key_and_non_object_entries_are_invalid(self, tmp_path, target):
        repo = _repo(tmp_path, links=[
            {"path": "a", "target": target, "mode": "x"}, "dev/tasks", 3])
        read = plc.read_project_links(repo)
        assert read.declarations == () and len(read.invalid) == 3

    def test_links_not_a_list_is_invalid(self, tmp_path):
        repo = _repo(tmp_path)
        _write_manifest(repo, {"path": "x"})
        read = plc.read_project_links(repo)
        assert read.invalid[0].status == "invalid"

    def test_duplicate_paths_reject_every_entry_involved(self, tmp_path, target):
        repo = _repo(tmp_path, links=[
            {"path": "dev/tasks", "target": target},
            {"path": "Dev/Tasks", "target": target},
            {"path": "other", "target": target},
        ])
        read = plc.read_project_links(repo)
        assert [d.path for d in read.declarations] == ["other"]
        assert len(read.invalid) == 2
        assert all("duplicate" in c.detail for c in read.invalid)

    def test_nested_paths_are_rejected(self, tmp_path, target):
        repo = _repo(tmp_path, links=[
            {"path": "dev", "target": target},
            {"path": "dev/tasks", "target": target},
        ])
        read = plc.read_project_links(repo)
        assert read.declarations == () and len(read.invalid) == 2


class TestProvenance:
    def test_only_the_project_layer_file_is_read(self, tmp_path, target, monkeypatch):
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / "bootstrap.json").write_text(json.dumps(
            {"links": [{"path": "dev/tasks", "target": target}]}))
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        repo = _repo(tmp_path)
        assert plc.read_project_links(repo).status == "absent"

    def test_local_layer_links_are_not_read(self, tmp_path, target):
        repo = _repo(tmp_path)
        os.makedirs(os.path.join(repo, ".claude"))
        with open(os.path.join(repo, ".claude", "bootstrap.local.json"), "w") as f:
            json.dump({"links": [{"path": "dev/tasks", "target": target}]}, f)
        assert plc.read_project_links(repo).status == "absent"

    def test_engine_ignores_and_reports_a_user_layer_links(
            self, tmp_path, target, monkeypatch):
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / "bootstrap.json").write_text(json.dumps(
            {"links": [{"path": "dev/tasks", "target": target}]}))
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        repo = _repo(tmp_path)
        merged, errors = engine._load_layered_manifests(repo, None)
        assert errors == [] and merged.get("links")
        actions, oks, failures = engine._run_project_links_check(
            repo, merged.get("links"))
        assert failures == []
        assert any("ignored `links` declared outside" in a for a in actions)
        assert not os.path.lexists(os.path.join(repo, "dev"))


class TestEngineMessages:
    def test_no_project_dir(self):
        actions, oks, failures = engine._run_project_links_check(None, None)
        assert (actions, failures) == ([], [])
        assert oks == ["project links: skipped (no project directory)"]

    def test_no_manifest_and_no_links_each_log_one_ok(self, tmp_path):
        repo = _repo(tmp_path)
        actions, oks, failures = engine._run_project_links_check(repo, None)
        assert actions == [] and failures == [] and len(oks) == 1
        os.makedirs(os.path.join(repo, ".claude"))
        open(os.path.join(repo, ".claude", "bootstrap.json"), "w").write("{}")
        actions, oks, failures = engine._run_project_links_check(repo, None)
        assert actions == [] and failures == [] and len(oks) == 1

    def test_empty_links_list_logs_one_ok(self, tmp_path):
        repo = _repo(tmp_path, links=[])
        actions, oks, failures = engine._run_project_links_check(repo, [])
        assert actions == [] and failures == []
        assert len(oks) == 1 and "links list is empty" in oks[0]

    def test_unparseable_manifest_logs_one_ok(self, tmp_path):
        repo = _repo(tmp_path)
        os.makedirs(os.path.join(repo, ".claude"))
        open(os.path.join(repo, ".claude", "bootstrap.json"), "w").write("{nope")
        actions, oks, failures = engine._run_project_links_check(repo, None)
        assert failures == [] and len(oks) == 1 and "unreadable" in oks[0]

    def test_created_then_already_linked(self, tmp_path, target):
        repo = _repo(tmp_path, links=[{"path": "dev/tasks", "target": target}])
        merged, _ = engine._load_layered_manifests(repo, None)
        actions, oks, failures = engine._run_project_links_check(repo, merged.get("links"))
        assert failures == [] and oks == [] and len(actions) == 1
        assert "created dev/tasks ->" in actions[0]
        actions, oks, failures = engine._run_project_links_check(repo, merged.get("links"))
        assert failures == [] and actions == [] and len(oks) == 1
        assert "already linked" in oks[0]

    @pytest.mark.parametrize("make,expected", [
        ("wrong", "wrong_target"), ("plain_dir", "not_link"),
        ("ignore_off", "not_ignored"), ("no_target", "target_missing"),
    ])
    def test_each_refusal_is_one_persistent_failure(
            self, tmp_path, target, make, expected):
        tgt = target
        repo = _repo(tmp_path)
        if make == "wrong":
            os.makedirs(os.path.join(repo, "dev"))
            os.symlink(str(tmp_path), os.path.join(repo, "dev", "tasks"))
        elif make == "plain_dir":
            os.makedirs(os.path.join(repo, "dev", "tasks"))
        elif make == "ignore_off":
            open(os.path.join(repo, ".gitignore"), "w").write("")
        elif make == "no_target":
            tgt = str(tmp_path / "missing")
        _write_manifest(repo, [{"path": "dev/tasks", "target": tgt}])
        merged, _ = engine._load_layered_manifests(repo, None)
        actions, oks, failures = engine._run_project_links_check(repo, merged.get("links"))
        assert oks == [] and len(actions) == 1 and len(failures) == 1
        assert expected in actions[0]
        assert failures[0]["type"] == "project_links"
        assert failures[0]["persist_across_sessions"] is True

    def test_invalid_declaration_is_a_failure(self, tmp_path, target):
        repo = _repo(tmp_path, links=[{"path": "../x", "target": target}])
        actions, oks, failures = engine._run_project_links_check(repo, [{"path": "../x", "target": target}])
        assert len(failures) == 1 and "invalid declaration" in actions[0]


class TestWorktreePass:
    def test_worktree_gets_the_link_after_the_pass_step(
            self, tmp_path, target, monkeypatch):
        monkeypatch.setattr(
            asc, "detect_codex",
            lambda: CodexDetection(available=False, reason="test"))
        repo = _repo(tmp_path, links=[{"path": "dev/tasks", "target": target}])
        _git("add", ".gitignore", ".claude/bootstrap.json", cwd=repo)
        _git("commit", "-q", "-m", "init", cwd=repo)
        wt = str(tmp_path / "wt")
        _git("worktree", "add", "-q", "-b", "unit", wt, cwd=repo)
        assert not os.path.lexists(os.path.join(wt, "dev"))

        lm, errors = engine._load_layered_manifests(wt, None)
        assert errors == []
        st = SimpleNamespace(
            args=SimpleNamespace(project_dir=wt), layered_manifest=lm,
            boot_action=[], boot_ok=[], boot_quiet=[], all_failures=[])
        engine._pass_project_npm_and_links(st)

        link = os.path.join(wt, "dev", "tasks")
        assert os.path.islink(link)
        assert os.path.realpath(link) == os.path.realpath(target)
        assert st.all_failures == []
        assert any("created dev/tasks ->" in a for a in st.boot_action)
        # Git sees nothing new: the link is ignored.
        status = subprocess.run(["git", "status", "--porcelain"], cwd=wt,
                                stdout=subprocess.PIPE, text=True, check=True)
        assert status.stdout.strip() == ""
