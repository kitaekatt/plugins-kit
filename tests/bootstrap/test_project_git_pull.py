"""Tests for project_git_pull.py -- the safe fast-forward of the project checkout.

Every test drives real git against a bare "origin" and a clone of it, because
the safety claims are claims about what git does to a working tree.
"""

import os
import subprocess

import pytest

from bootstrap_lib import project_git_pull as pgp
from bootstrap_lib.project_git_pull import PullConfig, parse_config, pull_project

_ENV = {
    "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
}
PLAIN = PullConfig(enabled=True)


def git(cwd, *args, check=True):
    result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                            env={**os.environ, **_ENV})
    if check:
        assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, newline="\n")


def commit_all(repo, message):
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repos(tmp_path):
    """(work, upstream_editor): a clone tracking origin/main, and a second clone to push from."""
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    seed = tmp_path / "seed"
    git(tmp_path, "clone", "-q", "-c", "core.autocrlf=false", str(origin), str(seed))
    write(seed / "a.txt", "a\n")
    write(seed / "b.txt", "b\n")
    commit_all(seed, "seed")
    git(seed, "push", "-q", "origin", "HEAD:main")
    work = tmp_path / "work"
    git(tmp_path, "clone", "-q", "-c", "core.autocrlf=false", str(origin), str(work))
    return work, seed


def push_change(editor, path, text, message="upstream change"):
    write(editor / path, text)
    sha = commit_all(editor, message)
    git(editor, "push", "-q", "origin", "HEAD:main")
    return sha


class TestParseConfig:
    def test_true_enables_a_plain_pull(self):
        assert parse_config(True) == (PullConfig(enabled=True), None)

    def test_object_carries_gate_and_timeout(self):
        config, error = parse_config({"gate": "node gate.mjs", "gate_timeout": 30})
        assert error is None
        assert config == PullConfig(enabled=True, gate="node gate.mjs", gate_timeout=30)

    def test_enabled_false_disables(self):
        assert parse_config({"enabled": False})[0].enabled is False

    @pytest.mark.parametrize("value", ["yes", 1, {"enabled": "no"}, {"gate": ""},
                                       {"gate_timeout": 0}, {"gate_timeout": True}])
    def test_malformed_values_are_rejected_with_a_reason(self, value):
        config, error = parse_config(value)
        assert config is None and error


class TestUpdates:
    def test_behind_clean_checkout_fast_forwards(self, repos):
        work, editor = repos
        target = push_change(editor, "a.txt", "a2\n")
        result = pull_project(str(work), PLAIN)
        assert result.outcome == pgp.UPDATED
        assert result.moved
        assert git(work, "rev-parse", "HEAD") == target
        assert result.notice == f"project updated: main fast-forwarded 1 commit(s) to {target[:7]}"

    def test_dirty_files_the_update_does_not_touch_survive_it(self, repos):
        work, editor = repos
        target = push_change(editor, "a.txt", "a2\n")
        write(work / "b.txt", "local edit\n")
        write(work / "staged.txt", "staged\n")
        git(work, "add", "staged.txt")
        write(work / "untracked.txt", "untracked\n")
        result = pull_project(str(work), PLAIN)
        assert result.outcome == pgp.UPDATED
        assert git(work, "rev-parse", "HEAD") == target
        assert (work / "b.txt").read_text() == "local edit\n"
        assert (work / "untracked.txt").read_text() == "untracked\n"
        assert "A  staged.txt" in git(work, "status", "--porcelain")

    def test_up_to_date_is_log_only(self, repos):
        work, _ = repos
        result = pull_project(str(work), PLAIN)
        assert result.outcome == pgp.CURRENT
        assert result.notice is None
        assert not result.moved

    def test_local_commits_with_nothing_incoming_are_current(self, repos):
        work, _ = repos
        write(work / "a.txt", "mine\n")
        commit_all(work, "local")
        result = pull_project(str(work), PLAIN)
        assert result.outcome == pgp.CURRENT
        assert "1 local commit(s)" in result.summary

    def test_a_failing_post_merge_hook_does_not_hide_the_update(self, repos):
        work, editor = repos
        target = push_change(editor, "a.txt", "a2\n")
        hook = work / ".git" / "hooks" / "post-merge"
        write(hook, "#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        result = pull_project(str(work), PLAIN)
        assert result.outcome == pgp.UPDATED
        assert git(work, "rev-parse", "HEAD") == target


class TestNotApplicable:
    def test_outside_a_checkout(self, tmp_path):
        result = pull_project(str(tmp_path), PLAIN)
        assert result.outcome == pgp.NOT_A_REPOSITORY
        assert result.notice is None

    def test_detached_head(self, repos):
        work, editor = repos
        push_change(editor, "a.txt", "a2\n")
        git(work, "checkout", "-q", "--detach")
        result = pull_project(str(work), PLAIN)
        assert result.outcome == pgp.DETACHED_HEAD
        assert result.notice is None

    def test_branch_without_upstream(self, repos):
        work, _ = repos
        git(work, "checkout", "-q", "-b", "feature")
        result = pull_project(str(work), PLAIN)
        assert result.outcome == pgp.NO_UPSTREAM
        assert result.notice is None


class TestBlocked:
    def _assert_untouched(self, work, head):
        assert git(work, "rev-parse", "HEAD") == head

    def test_diverged_history_is_left_alone(self, repos):
        work, editor = repos
        push_change(editor, "a.txt", "theirs\n")
        write(work / "b.txt", "mine\n")
        head = commit_all(work, "local")
        result = pull_project(str(work), PLAIN)
        assert result.outcome == pgp.DIVERGED
        assert "1 local commit(s)" in result.summary and "1 new commit(s)" in result.summary
        assert result.notice.startswith("project not updated [diverged]: ")
        self._assert_untouched(work, head)

    def test_a_local_edit_on_an_incoming_path_blocks_and_survives(self, repos):
        work, editor = repos
        head = git(work, "rev-parse", "HEAD")
        push_change(editor, "a.txt", "theirs\n")
        write(work / "a.txt", "mine\n")
        result = pull_project(str(work), PLAIN)
        assert result.outcome == pgp.LOCAL_CHANGES_OVERLAP
        assert "a.txt" in result.summary
        assert (work / "a.txt").read_text() == "mine\n"
        self._assert_untouched(work, head)

    @pytest.mark.parametrize("ignored", [False, True])
    def test_a_file_already_where_the_update_adds_one_blocks(self, repos, ignored):
        work, editor = repos
        head = git(work, "rev-parse", "HEAD")
        push_change(editor, "new.txt", "theirs\n")
        if ignored:
            write(work / ".git" / "info" / "exclude", "new.txt\n")
        write(work / "new.txt", "mine\n")
        result = pull_project(str(work), PLAIN)
        assert result.outcome == pgp.LOCAL_CHANGES_OVERLAP
        assert (work / "new.txt").read_text() == "mine\n"
        self._assert_untouched(work, head)

    def test_a_file_where_the_update_needs_a_directory_blocks(self, repos):
        work, editor = repos
        head = git(work, "rev-parse", "HEAD")
        push_change(editor, "dir/inner.txt", "theirs\n")
        write(work / "dir", "a file, not a directory\n")
        result = pull_project(str(work), PLAIN)
        assert result.outcome == pgp.LOCAL_CHANGES_OVERLAP
        self._assert_untouched(work, head)

    def test_merge_in_progress(self, repos):
        work, editor = repos
        git(work, "checkout", "-q", "-b", "side")
        write(work / "a.txt", "side\n")
        commit_all(work, "side")
        git(work, "checkout", "-q", "main")
        write(work / "a.txt", "main\n")
        commit_all(work, "main")
        git(work, "merge", "side", check=False)
        result = pull_project(str(work), PLAIN)
        assert result.outcome == pgp.OPERATION_IN_PROGRESS
        assert result.summary == "merge in progress"

    def test_a_stopped_rebase_reports_the_rebase_not_a_detached_head(self, repos):
        work, editor = repos
        push_change(editor, "a.txt", "theirs\n")
        write(work / "a.txt", "mine\n")
        commit_all(work, "local")
        git(work, "fetch", "-q")
        git(work, "rebase", "origin/main", check=False)
        result = pull_project(str(work), PLAIN)
        assert result.outcome == pgp.OPERATION_IN_PROGRESS
        assert result.summary == "rebase in progress"

    def test_unreachable_remote(self, repos, tmp_path):
        work, _ = repos
        head = git(work, "rev-parse", "HEAD")
        git(work, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
        result = pull_project(str(work), PLAIN)
        assert result.outcome == pgp.FETCH_FAILED
        assert result.notice.startswith("project not updated [fetch-failed]: fetching origin failed (")
        self._assert_untouched(work, head)


class TestFilters:
    def _filtered_update(self, work, editor, driver):
        write(editor / ".gitattributes", f"*.bin filter={driver}\n")
        write(editor / "asset.bin", "v1\n")
        commit_all(editor, "filtered asset")
        git(editor, "push", "-q", "origin", "HEAD:main")
        git(work, "pull", "-q", "--ff-only")
        return push_change(editor, "asset.bin", "v2\n")

    def test_an_unverifiable_filter_blocks_before_the_tree_is_touched(self, repos):
        work, editor = repos
        self._filtered_update(work, editor, "custom")
        head = git(work, "rev-parse", "HEAD")
        git(work, "config", "filter.custom.clean", "cat")
        git(work, "config", "filter.custom.smudge", "false")
        git(work, "config", "filter.custom.required", "true")
        result = pull_project(str(work), PLAIN)
        assert result.outcome == pgp.UNSUPPORTED_FILTER
        assert "(custom): asset.bin" in result.summary
        assert git(work, "rev-parse", "HEAD") == head
        assert git(work, "status", "--porcelain") == ""

    def test_a_refusal_names_the_files_the_attempt_left_changed(self, repos, monkeypatch):
        """Why the filter check exists: with it bypassed, a failing smudge
        aborts the merge with HEAD unmoved but the tracked file deleted -- and
        the report must say so rather than imply an untouched tree."""
        work, editor = repos
        self._filtered_update(work, editor, "custom")
        head = git(work, "rev-parse", "HEAD")
        git(work, "config", "filter.custom.clean", "cat")
        git(work, "config", "filter.custom.smudge", "false")
        git(work, "config", "filter.custom.required", "true")
        monkeypatch.setattr(pgp, "_filters", lambda *a: {})
        result = pull_project(str(work), PLAIN)
        assert result.outcome == pgp.FF_REFUSED
        assert git(work, "rev-parse", "HEAD") == head
        assert "the attempt left 1 file(s) changed: asset.bin" in result.summary

    def test_lfs_files_need_git_lfs(self, repos, monkeypatch):
        work, editor = repos
        self._filtered_update(work, editor, "lfs")
        head = git(work, "rev-parse", "HEAD")
        real = pgp._git
        monkeypatch.setattr(pgp, "_git", lambda repo, *args, **kw: (
            (1, "", "git: 'lfs' is not a git command") if args[:1] == ("lfs",) else real(repo, *args, **kw)))
        result = pull_project(str(work), PLAIN)
        assert result.outcome == pgp.LFS_UNAVAILABLE
        assert git(work, "rev-parse", "HEAD") == head

    def test_the_targets_attributes_decide_not_the_current_ones(self, repos):
        work, editor = repos
        write(editor / "asset.bin", "v1\n")
        commit_all(editor, "plain asset")
        git(editor, "push", "-q", "origin", "HEAD:main")
        git(work, "pull", "-q", "--ff-only")
        target = push_change(editor, ".gitattributes", "*.bin filter=custom\n")
        git(work, "fetch", "-q")
        found = pgp._filters(str(work), target, ["asset.bin", "a.txt"])
        assert found == {"asset.bin": "custom"}


class TestGitUnavailable:
    def test_missing_git_is_log_only(self, repos, monkeypatch):
        work, _ = repos
        monkeypatch.setattr(pgp.shutil, "which", lambda name: None)
        result = pull_project(str(work), PLAIN)
        assert result.outcome == pgp.GIT_UNAVAILABLE
        assert result.notice is None


class TestGate:
    def test_exit_zero_allows(self, repos):
        work, editor = repos
        target = push_change(editor, "a.txt", "a2\n")
        result = pull_project(str(work), PullConfig(enabled=True, gate="exit 0"))
        assert result.outcome == pgp.UPDATED
        assert git(work, "rev-parse", "HEAD") == target

    def test_decline_reports_the_gates_last_line_and_sees_the_shas(self, repos):
        work, editor = repos
        head = git(work, "rev-parse", "HEAD")
        target = push_change(editor, "a.txt", "a2\n")
        gate = 'echo "checking"; echo "not yet: $BOOTSTRAP_PULL_FROM -> $BOOTSTRAP_PULL_TO"; exit 75'
        result = pull_project(str(work), PullConfig(enabled=True, gate=gate))
        assert result.outcome == pgp.GATE_DECLINED
        assert result.summary == f"not yet: {head} -> {target}"
        assert git(work, "rev-parse", "HEAD") == head

    def test_any_other_exit_is_a_gate_failure_that_blocks(self, repos):
        work, editor = repos
        head = git(work, "rev-parse", "HEAD")
        push_change(editor, "a.txt", "a2\n")
        result = pull_project(str(work), PullConfig(enabled=True, gate="echo boom; exit 3"))
        assert result.outcome == pgp.GATE_FAILED
        assert result.summary == "the gate exited 3"
        assert git(work, "rev-parse", "HEAD") == head

    def test_a_gate_that_overruns_its_timeout_blocks(self, repos):
        work, editor = repos
        head = git(work, "rev-parse", "HEAD")
        push_change(editor, "a.txt", "a2\n")
        result = pull_project(str(work), PullConfig(enabled=True, gate="sleep 5", gate_timeout=1))
        assert result.outcome == pgp.GATE_FAILED
        assert "timed out" in result.summary
        assert git(work, "rev-parse", "HEAD") == head

    def test_credentials_in_a_remote_url_never_reach_the_report(self):
        text = "fatal: unable to access 'https://user:ghp_secret@github.com/o/r.git/': 403"
        assert "ghp_secret" not in pgp._redact(text)
        assert "https://***@github.com/o/r.git/" in pgp._redact(text)
        assert "ghp_secret" not in pgp._short_cause(text)

    def test_the_gate_reason_is_printable_and_capped(self):
        reason = pgp._sanitize_reason("first\n" + "x\x1b" * 300 + "\n\n")
        assert len(reason) == pgp._GATE_REASON_MAX
        assert "\x1b" not in reason
        assert pgp._sanitize_reason("") == "the gate gave no reason"


class TestEngineWiring:
    def test_an_update_is_a_notice_with_an_authored_label_and_no_failure(self, repos):
        from bootstrap_lib import engine
        from bootstrap_lib.records import short_form
        work, editor = repos
        push_change(editor, "a.txt", "a2\n")
        quiet = []
        actions, oks, failures, notice, moved = engine._process_project_git_pull(
            True, str(work), quiet_entries=quiet)
        assert (actions, failures) == ([], [])
        assert moved
        # The authored label is what the width limit renders, so the whole
        # classified line reaches the user rather than a cut-down head.
        assert short_form(notice) == str(notice)
        assert str(notice).startswith("project updated: ")
        assert any(q.startswith("project_git_pull: updated - ") for q in quiet)

    def test_a_blocked_update_is_a_notice_not_a_fix_all_item(self, repos):
        from bootstrap_lib import engine
        work, editor = repos
        push_change(editor, "a.txt", "theirs\n")
        write(work / "a.txt", "mine\n")
        actions, oks, failures, notice, moved = engine._process_project_git_pull(True, str(work))
        assert (actions, failures, moved) == ([], [], False)
        assert str(notice).startswith("project not updated [local-changes-overlap]: ")

    def test_a_malformed_declaration_is_the_only_failure(self, repos):
        from bootstrap_lib import engine
        work, _ = repos
        actions, oks, failures, notice, moved = engine._process_project_git_pull("yes", str(work))
        assert notice is None and not moved
        assert [f["type"] for f in failures] == ["project_git_pull"]

    def test_disabled_does_nothing(self, repos):
        from bootstrap_lib import engine
        work, editor = repos
        head = git(work, "rev-parse", "HEAD")
        push_change(editor, "a.txt", "a2\n")
        actions, oks, failures, notice, moved = engine._process_project_git_pull(
            {"enabled": False}, str(work))
        assert notice is None and not moved
        assert oks == ["project_git_pull: skipped - disabled"]
        assert git(work, "rev-parse", "HEAD") == head

    def test_bootstrap_run_applies_the_manifest_the_update_brought(self, repos, tmp_path, data_dir):
        """The pull runs first and the manifest is reloaded, so a git_config
        entry that arrives WITH the update is applied in the same run -- here
        through the exact engine invocation `bootstrap run` makes."""
        import subprocess
        import sys
        from bootstrap.test_engine_personal import ENGINE_SCRIPT, make_minimal_root
        work, editor = repos
        write(editor / ".claude" / "bootstrap.json", '{"project_git_pull": true}\n')
        commit_all(editor, "opt in")
        git(editor, "push", "-q", "origin", "HEAD:main")
        git(work, "pull", "-q", "--ff-only")
        write(editor / ".claude" / "bootstrap.json",
              '{"project_git_pull": true, "git_config": [{"key": "fixture.pulled", "value": "yes"}]}\n')
        target = commit_all(editor, "add git_config")
        git(editor, "push", "-q", "origin", "HEAD:main")
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        env = dict(os.environ, HOME=str(home), USERPROFILE=str(home))
        result = subprocess.run(
            [sys.executable, ENGINE_SCRIPT, "--plugin-root", make_minimal_root(tmp_path),
             "--data-dir", data_dir, "--project-dir", str(work),
             "--project-key", "_global_", "--console", "--exit-status"],
            capture_output=True, text=True, env=env, cwd=str(work),
            stdin=subprocess.DEVNULL)
        assert result.returncode == 0, result.stdout + result.stderr
        assert git(work, "rev-parse", "HEAD") == target
        assert "project updated: " in result.stdout
        assert git(work, "config", "--local", "fixture.pulled") == "yes"

    def test_session_start_pass_shows_the_whole_line_as_a_notice(self, repos, tmp_path, data_dir):
        import json
        from bootstrap.test_engine_personal import make_minimal_root, run_engine
        work, editor = repos
        push_change(editor, "a.txt", "theirs\n")
        write(work / "a.txt", "mine\n")
        fake_root = make_minimal_root(tmp_path)
        home = tmp_path / "fakehome"
        write(home / ".claude" / "bootstrap.json", '{"project_git_pull": true}\n')
        with open(os.path.join(data_dir, "config.json"), "w") as f:
            json.dump({"schema_version": 5}, f)
        result = run_engine(data_dir, plugin_root=fake_root, project_dir=str(work),
                            env_override={"HOME": str(home), "USERPROFILE": str(home)})
        assert result.returncode == 0, result.stderr
        message = json.loads(result.stdout)["systemMessage"]
        assert " notice: project not updated [local-changes-overlap]: 1 local file(s) would be " \
               "overwritten by 1 incoming commit(s): a.txt ---" in message
        assert "fix-all" not in message
