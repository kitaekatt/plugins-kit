"""Opt-in worktree configuration and real Git lifecycle tests."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "plugins/git-kit/scripts/worktrees.py"


@pytest.fixture
def wt(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    monkeypatch.setenv("PYTHONPATH", str(SCRIPT.parents[2] / "bootstrap"))
    spec = importlib.util.spec_from_file_location("git_kit_worktrees", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def manifest(base, value, local=False):
    folder = base / ".claude"
    folder.mkdir(exist_ok=True, parents=True)
    (folder / ("bootstrap.local.json" if local else "bootstrap.json")).write_text(
        json.dumps(value), encoding="utf-8"
    )


def test_setting_layers_and_validation(wt, tmp_path):
    home = Path.home()
    project = tmp_path / "project"
    project.mkdir()
    assert wt.resolve_settings(project).enabled is False
    manifest(home, {"git_kit": {"worktrees": {"enabled": True, "root": "~/trees"}}})
    assert wt.resolve_settings(project).root == home / "trees"
    manifest(project, {"git_kit": {"worktrees": {"enabled": False}}})
    settings = wt.resolve_settings(project)
    assert settings.enabled is False
    assert settings.root == home / "trees"
    manifest(project, {"git_kit": {"worktrees": {"enabled": True}}}, local=True)
    assert wt.resolve_settings(project).enabled is True
    manifest(project, {"git_kit": {"worktrees": {"enabled": "false"}}}, local=True)
    with pytest.raises(ValueError, match="enabled"):
        wt.resolve_settings(project)


def test_bad_manifest_is_loud(wt):
    folder = Path.home() / ".claude"
    folder.mkdir()
    (folder / "bootstrap.json").write_text("{bad", encoding="utf-8")
    with pytest.raises(ValueError, match="bootstrap.json"):
        wt.resolve_settings(None)


@pytest.mark.parametrize("selection,profiles", [
    ("broken", {"broken": {"extends": ["missing"], "git_kit": {"worktrees": {"enabled": False}}}}),
    ("missing", {"valid": {"git_kit": {"worktrees": {"enabled": False}}}}),
])
def test_profile_failure_does_not_apply_base_permissions(wt, selection, profiles):
    manifest(Path.home(), {"git_kit": {"worktrees": {"enabled": True}}, "profiles": profiles})
    manifest(Path.home(), {"profile": selection}, local=True)
    with pytest.raises(ValueError, match="profile"):
        wt.resolve_settings(None)
    assert not (Path.home() / ".claude/settings.json").exists()


def test_permission_merge_idempotent_preserves_entries(wt):
    target = Path.home() / ".claude/settings.json"
    target.parent.mkdir()
    original = {"env": {"KEEP": "yes"}, "permissions": {
        "allow": ["Read"], "additionalDirectories": ["../other", "/existing"]}}
    target.write_text(json.dumps(original), encoding="utf-8")
    root = Path.home() / ".worktrees"
    assert wt.ensure_permissions(root) is True
    changed = target.read_bytes()
    payload = json.loads(changed)
    assert payload["env"] == original["env"]
    assert payload["permissions"]["allow"] == ["Read"]
    assert payload["permissions"]["additionalDirectories"] == ["../other", "/existing", str(root)]
    assert wt.ensure_permissions(root) is False
    assert target.read_bytes() == changed
    assert root.is_dir()


def test_off_bootstrap_changes_nothing(wt, monkeypatch):
    import git_kit_bootstrap as gb
    from test_bootstrap import FakeCtx

    ctx = FakeCtx()
    monkeypatch.setattr(gb, "_resolve_gh", lambda: None)
    gb.bootstrap(ctx)
    assert not (Path.home() / ".worktrees").exists()
    assert not (Path.home() / ".claude/settings.json").exists()


def test_real_script_phase_enables_before_missing_gh(wt, tmp_path, monkeypatch):
    from bootstrap_lib.engine import _run_script_phase

    manifest(Path.home(), {"git_kit": {"worktrees": {"enabled": True}}})
    monkeypatch.setenv("PATH", "")
    actions, oks = [], []
    failures = _run_script_phase(
        {"path": "scripts/bootstrap.py"}, str(SCRIPT.parents[1]), str(tmp_path / "data"),
        {}, actions, oks, plugin_name="git-kit"
    )
    assert failures == []
    settings = json.loads((Path.home() / ".claude/settings.json").read_text())
    assert str(Path.home() / ".worktrees") in settings["permissions"]["additionalDirectories"]
    assert any("worktrees" in entry for entry in actions)


@pytest.fixture
def remote_repo(wt, git_repo, tmp_path):
    git_repo.commit_file("file.txt", "initial")
    remote = tmp_path / "remote.git"
    git_repo.git("init", "--bare", str(remote))
    git_repo.git("remote", "add", "origin", str(remote))
    git_repo.git("push", "-u", "origin", "main")
    git_repo.git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    manifest(Path.home(), {"git_kit": {"worktrees": {"enabled": True}}})
    return git_repo


def cli(repo, *args, check=True):
    proc = subprocess.run([sys.executable, str(SCRIPT), *args], cwd=repo.path,
                          capture_output=True, text=True)
    if check:
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout)
    return proc


def test_new_fresh_list_remove(wt, remote_repo):
    repo = remote_repo
    new = cli(repo, "new", "task-one")
    task = Path(new["path"])
    assert task == Path.home() / ".worktrees" / repo.path.name / "task-one"
    assert task.is_dir()
    rows = cli(repo, "list")
    assert rows[0]["path"] == str(task)
    assert rows[0]["no_unpublished_commits"] is True
    assert rows[0]["merged"] is True
    cli(repo, "remove", str(task))
    assert not task.exists()
    assert str(task) not in repo.git("worktree", "list").stdout


def test_unpublished_dirty_and_outside_refused(wt, remote_repo):
    repo = remote_repo
    task = Path(cli(repo, "new", "task-two")["path"])
    (task / "new.txt").write_text("private", encoding="utf-8")
    assert cli(repo, "remove", str(task), check=False).returncode != 0
    subprocess.run(["git", "-C", str(task), "add", "new.txt"], check=True)
    subprocess.run(["git", "-C", str(task), "commit", "-qm", "unpublished"], check=True)
    assert cli(repo, "list")[0]["no_unpublished_commits"] is False
    assert cli(repo, "remove", str(task), check=False).returncode != 0
    assert cli(repo, "remove", str(repo.path), check=False).returncode != 0
    assert cli(repo, "new", "../escape", check=False).returncode != 0
    assert cli(repo, "new", "local", "--from", "main", check=False).returncode != 0


def test_cli_off_loud(wt, git_repo):
    assert cli(git_repo, "new", "disabled", check=False).returncode != 0
    assert not (Path.home() / ".worktrees").exists()


@pytest.mark.parametrize("payload", [[], {"permissions": []}, {"permissions": {"additionalDirectories": "bad"}}, {"permissions": {"additionalDirectories": [3]}}])
def test_malformed_settings_preserved(wt, payload):
    target = Path.home() / ".claude/settings.json"
    target.parent.mkdir()
    target.write_text(json.dumps(payload), encoding="utf-8")
    before = target.read_bytes()
    with pytest.raises(ValueError):
        wt.ensure_permissions(Path.home() / ".worktrees")
    assert target.read_bytes() == before
    assert not (Path.home() / ".worktrees").exists()


def test_fetches_advance_and_project_config_from_linked_tree(wt, remote_repo, tmp_path):
    repo = remote_repo
    manifest(Path.home(), {})
    manifest(repo.path, {"git_kit": {"worktrees": {"enabled": True, "root": "~/custom trees"}}})
    first = Path(cli(repo, "new", "first")["path"])
    peer = tmp_path / "peer"
    repo.git("clone", "-b", "main", str(tmp_path / "remote.git"), str(peer))
    peer_repo = type(repo)(peer)
    peer_repo.git("config", "user.name", "Peer")
    peer_repo.git("config", "user.email", "peer@example.com")
    peer_repo.commit_file("advance.txt", "fresh remote")
    peer_repo.git("push", "origin", "main")
    repo.git("push", "origin", "HEAD:refs/heads/topic")
    repo.git("config", "remote.origin.fetch", "+refs/heads/topic:refs/remotes/origin/topic")
    linked = type(repo)(first)
    second = Path(cli(linked, "new", "second")["path"])
    assert second == Path.home() / "custom trees" / repo.path.name / "second"
    assert (second / "advance.txt").read_text() == "fresh remote"
    explicit = Path(cli(linked, "new", "explicit", "--from", "origin/main")["path"])
    assert (explicit / "advance.txt").read_text() == "fresh remote"
    assert repo.git("rev-parse", "HEAD").stdout != peer_repo.git("rev-parse", "HEAD").stdout


def test_root_wide_list_and_gone_merged_branch(wt, remote_repo, tmp_path):
    repo = remote_repo
    first = Path(cli(repo, "new", "first")["path"])
    branch_repo = type(repo)(first)
    branch_repo.commit_file("topic.txt", "published")
    branch_repo.git("push", "-u", "origin", "first")
    assert cli(repo, "list")[0]["merged"] is False
    repo.git("merge", "--ff-only", "first")
    repo.git("push", "origin", "main")
    repo.git("push", "origin", "--delete", "first")
    other = tmp_path / "other repo"
    repo.git("clone", "-b", "main", str(tmp_path / "remote.git"), str(other))
    other_repo = type(repo)(other)
    second = Path(cli(other_repo, "new", "second")["path"])
    rows = {row["path"]: row for row in cli(repo, "list")}
    assert set(rows) == {str(first), str(second)}
    assert rows[str(first)]["gone"] is True
    assert rows[str(first)]["merged"] is True
    assert rows[str(first)]["no_unpublished_commits"] is True
    cli(repo, "remove", str(second))
    assert not second.exists()
    nonrepo = type(repo)(Path.home())
    assert cli(nonrepo, "list")[0]["path"] == str(first)
    assert cli(nonrepo, "config")["enabled"] is True
    cli(nonrepo, "remove", str(first))
    assert not first.exists()


def test_symlink_escape_refused(wt, remote_repo, tmp_path):
    repo = remote_repo
    root = Path.home() / ".worktrees"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (root / repo.path.name).symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    assert cli(repo, "new", "escape", check=False).returncode != 0
    assert list(outside.iterdir()) == []


def test_fetch_failure_and_orphan_leftover_are_loud(wt, remote_repo):
    repo = remote_repo
    repo.git("remote", "set-url", "origin", str(Path.home() / "missing.git"))
    failed = cli(repo, "new", "offline", check=False)
    assert failed.returncode != 0
    assert "repository" in failed.stderr
    orphan = Path.home() / ".worktrees/repo/orphan"
    orphan.mkdir(parents=True)
    failure = cli(repo, "list", check=False)
    assert failure.returncode != 0
    assert "Unregistered leftover" in failure.stderr


def test_ignored_files_block_removal(wt, remote_repo):
    repo = remote_repo
    task = Path(cli(repo, "new", "ignored")["path"])
    (repo.path / ".git/info/exclude").write_text("*.secret\n", encoding="utf-8")
    valuable = task / "only-copy.secret"
    valuable.write_text("unique ignored content", encoding="utf-8")
    assert cli(repo, "list")[0]["dirty"] is True
    failure = cli(repo, "remove", str(task), check=False)
    assert failure.returncode != 0
    assert "including ignored" in failure.stderr
    assert valuable.read_text() == "unique ignored content"


def test_remove_from_inside_target_refused(wt, remote_repo):
    task = Path(cli(remote_repo, "new", "inside")["path"])
    failure = cli(type(remote_repo)(task), "remove", str(task), check=False)
    assert failure.returncode != 0
    assert task.is_dir()


def test_deleted_unmapped_remote_ref_cannot_authorize_removal(wt, remote_repo):
    repo = remote_repo
    task = Path(cli(repo, "new", "retired")["path"])
    task_repo = type(repo)(task)
    task_repo.commit_file("only.txt", "unique commit")
    task_repo.git("push", "-u", "origin", "retired")
    repo.git("config", "remote.origin.fetch", "+refs/heads/main:refs/remotes/origin/main")
    repo.git("--git-dir", str(repo.path.parent / "remote.git"), "update-ref", "-d", "refs/heads/retired")
    assert repo.git("show-ref", "--verify", "refs/remotes/origin/retired").returncode == 0
    rows = cli(repo, "list")
    assert rows[0]["gone"] is True
    assert rows[0]["no_unpublished_commits"] is False
    failure = cli(repo, "remove", str(task), check=False)
    assert failure.returncode != 0
    assert "unpublished" in failure.stderr
    assert (task / "only.txt").read_text() == "unique commit"


def test_bootstrap_resolution_in_linked_tree_uses_main_local_layer(wt, remote_repo):
    repo = remote_repo
    task = Path(cli(repo, "new", "linked")["path"])
    manifest(repo.path, {"git_kit": {"worktrees": {"root": "~/main-local"}}}, local=True)
    assert wt.resolve_settings(task).root == Path.home() / "main-local"


def test_script_phase_from_subdirectory_resolves_project_opt_in(wt, remote_repo, tmp_path, monkeypatch):
    from bootstrap_lib.engine import _run_script_phase

    repo = remote_repo
    manifest(Path.home(), {})
    manifest(repo.path, {"git_kit": {"worktrees": {"enabled": True, "root": "~/subdir-root"}}})
    subdir = repo.path / "src"
    subdir.mkdir()
    # Avoid a network-backed gh probe while preserving real Git discovery.
    import shutil
    real_which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda name: None if name == "gh" else real_which(name))
    from bootstrap_lib import tool_paths
    monkeypatch.setattr(tool_paths, "resolve", lambda *_: None)
    actions = []
    _run_script_phase({"path": "scripts/bootstrap.py"}, str(SCRIPT.parents[1]),
                      str(tmp_path / "data"), {}, actions, [], plugin_name="git-kit",
                      project_dir=str(subdir))
    settings = json.loads((Path.home() / ".claude/settings.json").read_text())
    assert str(Path.home() / "subdir-root") in settings["permissions"]["additionalDirectories"]
