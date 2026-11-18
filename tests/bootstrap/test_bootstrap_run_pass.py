"""`bootstrap run` launches the same engine pass the hooks run.

The terminal command used to compose a separate, layered-only runner
(bootstrap_run.py -> layered_bootstrap), so it never refreshed marketplaces,
updated plugins, or processed installed plugins' manifests. It now launches
engine/bootstrap_engine.py -- the entry the SessionStart and Codex hooks run --
and differs from a hook pass only by the flags it passes:

* ``--console``: terminal output; no log writes, cooldown or lifecycle stamps;
* ``--project-key _global_``: no per-project interpreter record;
* ``--exit-status``: a pass with failures exits 1, a stand-down exits 2.

Every engine run here is isolated: HOME and USERPROFILE point into tmp_path,
the plugin root is a linked tree outside the repository (so the engine cannot
walk up to a dev-layout registry), and nothing declares a marketplace.
"""

import json
import os
import subprocess
import sys

from bootstrap.link_compat import link_tree
from bootstrap.test_engine_personal import BOOTSTRAP_ROOT, ENGINE_SCRIPT, make_minimal_root

SHIM = os.path.join(BOOTSTRAP_ROOT, "scripts", "bootstrap.sh")
MISSING_TOOL = {"tools": [{"name": "bootstrap_run_missing_tool_xyz",
                           "install": {"windows": "manual", "macos": "manual",
                                       "ubuntu": "manual"}}]}


def _world(tmp_path, *, user_layer=None, plugin_manifest=None):
    """An isolated HOME, project, data root and plugin root."""
    home = tmp_path / "home"
    project = tmp_path / "project"
    data_root = tmp_path / "data"
    (home / ".claude" / "plugins").mkdir(parents=True)
    (project / ".claude").mkdir(parents=True)
    data_dir = data_root / "plugins-kit" / "bootstrap"
    data_dir.mkdir(parents=True)
    root = make_minimal_root(tmp_path)
    link_tree(tmp_path / "bootstrap_minimal" / "scripts",
              os.path.join(BOOTSTRAP_ROOT, "scripts"))
    hook = tmp_path / "bootstrap_minimal" / "hooks" / "sessionstart"
    hook.mkdir(parents=True)
    (hook / "session-bootstrap.sh").write_text("# plugin-root marker\n")
    plugin_json = tmp_path / "bootstrap_minimal" / ".claude-plugin"
    plugin_json.mkdir()
    (plugin_json / "plugin.json").write_text(
        json.dumps({"name": "bootstrap", "version": "0.130.0"}))
    if user_layer is not None:
        (home / ".claude" / "bootstrap.json").write_text(json.dumps(user_layer))
    if plugin_manifest is not None:
        demo = tmp_path / "demo"
        demo.mkdir()
        (demo / "bootstrap.json").write_text(json.dumps(plugin_manifest))
        (home / ".claude" / "plugins" / "installed_plugins.json").write_text(json.dumps(
            {"plugins": {"kit:demo": [{"installPath": str(demo), "version": "1.0.0"}]}}))
    env = dict(os.environ)
    env.update({
        "HOME": str(home), "USERPROFILE": str(home),
        "BOOTSTRAP_PLUGIN_ROOT": root, "BOOTSTRAP_MARKETPLACE": "plugins-kit",
        "CLAUDE_BOOTSTRAP_DATA_ROOT": str(data_root),
    })
    return type("World", (), {"home": home, "project": project, "root": root,
                              "data_root": data_root, "data_dir": data_dir,
                              "env": env})()


def _shim_run(world):
    return subprocess.run(["bash", SHIM, "run"], capture_output=True, text=True,
                          cwd=world.project, env=world.env, stdin=subprocess.DEVNULL)


def _engine(world, *flags):
    return subprocess.run(
        [sys.executable, ENGINE_SCRIPT, "--plugin-root", world.root,
         "--data-dir", str(world.data_dir), "--project-dir", str(world.project),
         *flags],
        capture_output=True, text=True, cwd=world.project, env=world.env,
        stdin=subprocess.DEVNULL)


def test_run_processes_installed_plugin_manifests_without_lifecycle_stamps(tmp_path):
    world = _world(tmp_path, user_layer={}, plugin_manifest={})
    completed = _shim_run(world)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "Bootstrap pass completed." in completed.stdout
    assert str(world.project / ".claude" / "bootstrap.local.json") in completed.stdout
    # The installed plugin's manifest is part of the pass now.
    assert (world.data_root / "kit" / "demo" / "last_version").read_text().strip() == "1.0.0"
    # Console mode: no engine lifecycle stamps, no cooldown, no log, and the
    # _global_ key reads and writes no project interpreter record.
    for name in ("engine_ran_version", "last_version", "bootstrap.log",
                 "cooldowns", "project_python"):
        assert not (world.data_dir / name).exists(), name


def test_run_exits_1_when_the_pass_reports_failures(tmp_path):
    world = _world(tmp_path, user_layer=MISSING_TOOL)
    completed = _shim_run(world)
    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert "bootstrap_run_missing_tool_xyz" in completed.stdout
    assert "Bootstrap pass reported failures (exit 1)." in completed.stdout


def test_hook_invocations_keep_exit_0_on_failures(tmp_path):
    """--exit-status is opt-in: the same failing pass without it exits 0, as
    the SessionStart and Codex hooks (which never pass it) always have."""
    world = _world(tmp_path, user_layer=MISSING_TOOL)
    assert _engine(world, "--console").returncode == 0
    assert _engine(world, "--console", "--exit-status").returncode == 1


def test_a_stand_down_exits_2_only_with_exit_status(tmp_path):
    from bootstrap_lib.proc_lock import engine_lock

    world = _world(tmp_path, user_layer={})
    # A held lock and an engine that is not newer than the last completed pass:
    # the immediate stand-down, not the version-aware retry.
    (world.data_dir / "engine_ran_version").write_text("0.130.0")
    with engine_lock(str(world.data_dir)) as acquired:
        assert acquired
        refused = _engine(world, "--console", "--exit-status",
                          "--project-key", "_global_")
        hook = _engine(world, "--console")
    assert refused.returncode == 2, refused.stdout + refused.stderr
    assert "stand-down" in refused.stdout
    assert hook.returncode == 0


# --------------------------------------------------------------------------
# bootstrap run --project-only
# --------------------------------------------------------------------------

def _shim_run_project_only(world):
    return subprocess.run(["bash", SHIM, "run", "--project-only"], capture_output=True,
                          text=True, cwd=world.project, env=world.env,
                          stdin=subprocess.DEVNULL)


def _git_project(world, ignored):
    git_env = dict(world.env, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    subprocess.run(["git", "init", "-q", str(world.project)], check=True, env=git_env)
    (world.project / ".gitignore").write_text(ignored + "\n")


def test_project_only_skips_user_layers_and_plugin_manifests(tmp_path):
    """The user layer's missing tool would fail a full pass (see
    test_run_exits_1_when_the_pass_reports_failures); a project-only pass
    never reads that layer, nor the installed plugin's manifest."""
    world = _world(tmp_path, user_layer=MISSING_TOOL, plugin_manifest={})
    (world.project / ".claude" / "bootstrap.json").write_text("{}")
    completed = _shim_run_project_only(world)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "bootstrap_run_missing_tool_xyz" not in completed.stdout
    assert not (world.data_root / "kit" / "demo" / "last_version").exists()
    for name in ("engine_ran_version", "last_version", "bootstrap.log",
                 "cooldowns", "project_python"):
        assert not (world.data_dir / name).exists(), name


def test_project_only_applies_the_project_layer_and_its_links(tmp_path):
    world = _world(tmp_path, user_layer={})
    target = tmp_path / "link-target"
    target.mkdir()
    _git_project(world, "dev/tasks")
    (world.project / ".claude" / "bootstrap.json").write_text(json.dumps(dict(
        MISSING_TOOL, links=[{"path": "dev/tasks", "target": str(target)}])))
    completed = _shim_run_project_only(world)
    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert "bootstrap_run_missing_tool_xyz" in completed.stdout
    assert "project-only" in completed.stdout
    link = world.project / "dev" / "tasks"
    assert link.is_symlink() or link.is_dir()
    assert os.path.realpath(link) == os.path.realpath(target)


def test_project_only_stand_down_leaves_the_session_cooldown_alone(tmp_path):
    """A project-only run never consumed the SessionStart cooldown stamp, so
    losing the lock must not roll it back -- unlike a full pass, which clears
    the stamp it assumes the shell hook wrote for it."""
    from bootstrap_lib.proc_lock import engine_lock
    from bootstrap_lib.stamps import project_stamp

    world = _world(tmp_path, user_layer={})
    (world.data_dir / "engine_ran_version").write_text("0.130.0")
    cooldown = project_stamp(str(world.data_dir), "last_run_epoch", str(world.project))
    with engine_lock(str(world.data_dir)) as acquired:
        assert acquired
        cooldown.write("1")
        refused = _engine(world, "--console", "--exit-status",
                          "--project-key", "_global_", "--run-kind", "project")
        assert refused.returncode == 2, refused.stdout + refused.stderr
        assert "stand-down" in refused.stdout
        assert cooldown.exists()
        # Control: the full kind on the same lock does clear it.
        _engine(world, "--console", "--exit-status", "--project-key", "_global_")
        assert not cooldown.exists()


def test_project_only_requires_console_mode(tmp_path):
    """Only a console pass is defined for the project kind: it writes no log,
    pending display file or lifecycle stamp. Refused, not degraded."""
    world = _world(tmp_path, user_layer={})
    refused = _engine(world, "--run-kind", "project")
    assert refused.returncode == 2
    # The specific refusal, not argparse's usage line (which names every flag).
    assert "--run-kind project requires --console" in refused.stderr
    fix_all = _engine(world, "--console", "--fix-all", "--run-kind", "project")
    assert fix_all.returncode == 2
    assert "--run-kind project does not support --fix-all" in fix_all.stderr
