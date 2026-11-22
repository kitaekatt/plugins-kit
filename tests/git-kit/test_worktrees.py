"""Opt-in worktree configuration and real Git lifecycle tests."""

import importlib.util
import json
import os
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
    # Every lifecycle test must stay isolated from the real provisioning CLI.
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake = fake_bin / "bootstrap"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "Path(os.environ['BOOTSTRAP_TEST_RECORD']).write_text("
        "json.dumps({'cwd': os.getcwd(), 'args': sys.argv[1:]}))\n"
        "print('bootstrap progress')\n"
        "print('bootstrap diagnostic', file=sys.stderr)\n"
        "sys.exit(int(os.environ.get('BOOTSTRAP_TEST_EXIT', '0')))\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    if os.name == "nt":
        (fake_bin / "bootstrap.cmd").write_text(
            f'@"{sys.executable}" "{fake}" %*\n', encoding="utf-8"
        )
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("BOOTSTRAP_TEST_RECORD", str(tmp_path / "bootstrap-call.json"))
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


@pytest.mark.parametrize("local_head", [True, False], ids=["cached-head", "remote-head"])
@pytest.mark.parametrize("checkout", ["untracked-branch", "other-upstream", "detached"])
def test_main_upstream_uses_remote_default(wt, git_repo, tmp_path, local_head, checkout):
    """Trigger: source checkout leaves main. Run this file with pytest."""
    git_repo.commit_file("tracked", "base")
    remote = tmp_path / "remote.git"
    wt.git(git_repo.path, "clone", "--bare", str(git_repo.path), str(remote))
    wt.git(git_repo.path, "remote", "add", "origin", str(remote))
    wt.refresh(git_repo.path)
    if local_head:
        wt.git(git_repo.path, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    if checkout == "detached":
        wt.git(git_repo.path, "checkout", "--detach")
    else:
        wt.git(git_repo.path, "checkout", "-b", "ref-test")
        if checkout == "other-upstream":
            wt.git(git_repo.path, "config", "branch.ref-test.remote", "origin")
            wt.git(git_repo.path, "config", "branch.ref-test.merge", "refs/heads/task")
    before = wt.git(git_repo.path, "rev-parse", "HEAD")
    assert wt.main_upstream(git_repo.path) == "refs/remotes/origin/main"
    assert wt.git(git_repo.path, "rev-parse", "HEAD") == before


def test_main_upstream_refuses_unknown_remote_head(wt, git_repo, tmp_path):
    """Trigger: remote HEAD is unborn. Never guess main/master."""
    git_repo.commit_file("tracked", "base")
    remote = tmp_path / "empty.git"
    wt.git(git_repo.path, "init", "--bare", str(remote))
    wt.git(git_repo.path, "remote", "add", "origin", str(remote))
    with pytest.raises(ValueError, match="default branch"):
        wt.main_upstream(git_repo.path)


@pytest.mark.parametrize("remote", ["origin", "upstream"])
def test_main_upstream_uses_repository_override(wt, git_repo, tmp_path, remote):
    """Trigger: integration differs from origin HEAD. Run this file with pytest."""
    git_repo.commit_file("tracked", "base")
    bare = tmp_path / "remote.git"
    wt.git(git_repo.path, "clone", "--bare", str(git_repo.path), str(bare))
    wt.git(git_repo.path, "remote", "add", "origin", str(bare))
    if remote != "origin":
        wt.git(git_repo.path, "remote", "add", remote, str(bare))
    wt.git(git_repo.path, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    manifest(git_repo.path, {"git_kit": {"worktrees": {"enabled": True, "pool": {
        "upstream": f"{remote}/team/develop", "setup": [{"id": "deps", "argv": ["true"]}]}}}})
    assert wt.main_upstream(git_repo.path) == f"refs/remotes/{remote}/team/develop"


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


@pytest.mark.parametrize("entry", ["~/.worktrees", "~/.worktrees/"])
def test_permission_merge_accepts_tilde_entry_for_root(wt, entry):
    target = Path.home() / ".claude/settings.json"
    target.parent.mkdir()
    target.write_text(json.dumps({"permissions": {"additionalDirectories": [entry]}}), encoding="utf-8")
    unchanged = target.read_bytes()
    root = Path.home() / ".worktrees"
    assert wt.ensure_permissions(root) is True  # root directory created
    assert target.read_bytes() == unchanged


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


@pytest.mark.parametrize("command", [("new", "raw"), ("take", "old-task"), ("remove", "unused")])
def test_raw_mutations_refuse_and_direct_configuration(wt, remote_repo, command):
    result = cli(remote_repo, *command, check=False)
    assert result.returncode != 0
    assert "configure" in result.stderr or "--agent" in result.stderr
    assert len(wt.records(remote_repo.path)) == 1


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
