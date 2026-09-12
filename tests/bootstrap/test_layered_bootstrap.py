"""The terminal run applies only the four user/project manifest layers."""

import json
from pathlib import Path
import subprocess
import sys

import pytest

from bootstrap_lib import engine


RUNNER = Path(__file__).resolve().parents[2] / "plugins/bootstrap/scripts/bootstrap_run.py"


@pytest.fixture
def environment(tmp_path, monkeypatch):
    home = tmp_path / "home"
    project = tmp_path / "project"
    plugin = tmp_path / "plugin"
    data = home / ".claude/plugins/data/plugins-kit/bootstrap"
    (home / ".claude").mkdir(parents=True)
    (project / ".claude").mkdir(parents=True)
    plugin.mkdir()
    data.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    return home, project, plugin, data


def test_only_four_layers_are_merged_and_processed(environment, monkeypatch):
    from bootstrap_lib.layered_bootstrap import run_layered_bootstrap

    home, project, plugin, data = environment
    paths = [home / ".claude/bootstrap.json", home / ".claude/bootstrap.local.json",
             project / ".claude/bootstrap.json", project / ".claude/bootstrap.local.json"]
    for index, path in enumerate(paths):
        path.write_text(json.dumps({"tools": [{"name": f"layer-{index}"},
                                              {"name": "shared", "check": f"check-{index}"}]}))
    (data / "user-bootstrap.json").write_text('{"tools":[{"name":"legacy-forbidden"}]}')
    (plugin / "bootstrap.json").write_text('{"tools":[{"name":"plugin-forbidden"}]}')
    (home / ".claude/env.json").write_text("not valid JSON")

    seen = []

    def tool(entry, *args, **kwargs):
        seen.append(entry)

    monkeypatch.setattr(engine, "_process_tool_entry", tool)
    for name in ("list_enabled_plugins", "_process_env_pass", "_shared_lib_convergence_sweep",
                 "_run_agent_skills_link_check"):
        monkeypatch.setattr(engine, name, lambda *a, **k: pytest.fail("automatic work ran"),
                            raising=False)
    result = run_layered_bootstrap(project, plugin, data, "macos")
    assert result.failures == []
    assert {entry["name"] for entry in seen} == {"shared", *(f"layer-{i}" for i in range(4))}
    assert next(entry for entry in seen if entry["name"] == "shared")["check"] == "check-3"


def test_subdirectory_does_not_search_parent_project(environment, monkeypatch):
    from bootstrap_lib.layered_bootstrap import run_layered_bootstrap

    home, project, plugin, data = environment
    (project / ".claude/bootstrap.json").write_text('{"tools":[{"name":"parent"}]}')
    child = project / "src"
    child.mkdir()
    seen = []
    monkeypatch.setattr(engine, "_process_tool_entry", lambda entry, *a, **k: seen.append(entry))
    result = run_layered_bootstrap(child, plugin, data, "macos")
    assert result.failures == []
    assert seen == []


def test_parse_error_prevents_provisioning(environment, monkeypatch):
    from bootstrap_lib.layered_bootstrap import run_layered_bootstrap

    home, project, plugin, data = environment
    (home / ".claude/bootstrap.json").write_text('{"tools":[{"name":"user"}]}')
    (project / ".claude/bootstrap.local.json").write_text("{bad")
    monkeypatch.setattr(engine, "_process_manifest", lambda *a, **k: pytest.fail("provisioned"))
    result = run_layered_bootstrap(project, plugin, data, "macos")
    assert len(result.failures) == 1
    assert result.failures[0]["type"] == "manifest_parse"


def test_runner_does_not_touch_plugin_lifecycle_state(environment):
    home, project, plugin, data = environment
    for name in ("engine_ran_version", "plugins_state_hash", "last_session_id", "env_state.json"):
        (data / name).write_text("keep")
    (data / "user-bootstrap.json").write_text("malformed legacy file must be ignored")
    (plugin / "bootstrap.json").write_text("malformed plugin file must be ignored")
    completed = subprocess.run(
        [sys.executable, str(RUNNER), "--project-dir", str(project),
         "--plugin-root", str(plugin), "--data-dir", str(data)],
        capture_output=True, text=True, cwd=project,
    )
    assert completed.returncode == 0, completed.stderr
    assert "user/project" in completed.stdout
    assert "bootstrap.local.json" in completed.stdout
    assert "absent" in completed.stdout
    for name in ("engine_ran_version", "plugins_state_hash", "last_session_id", "env_state.json"):
        assert (data / name).read_text() == "keep"
    assert not (data / "config.json").exists()


def test_runner_refuses_contended_lock(environment):
    from bootstrap_lib.proc_lock import engine_lock

    home, project, plugin, data = environment
    with engine_lock(str(data)):
        completed = subprocess.run(
            [sys.executable, str(RUNNER), "--project-dir", str(project),
             "--plugin-root", str(plugin), "--data-dir", str(data)],
            capture_output=True, text=True, cwd=project,
        )
    assert completed.returncode == 2
    assert "already running" in completed.stderr


def test_cli_shim_launches_only_the_layered_runner(environment, monkeypatch):
    import os

    home, project, plugin, data = environment
    source_root = RUNNER.parents[1]
    monkeypatch.setenv("BOOTSTRAP_PLUGIN_ROOT", str(source_root))
    monkeypatch.setenv("BOOTSTRAP_MARKETPLACE", "plugins-kit")
    monkeypatch.setenv("CLAUDE_BOOTSTRAP_DATA_ROOT", str(data.parents[1]))
    (home / ".claude/env.json").write_text("malformed env file must be ignored")
    (data / "user-bootstrap.json").write_text("malformed legacy file must be ignored")
    completed = subprocess.run(
        ["bash", str(source_root / "scripts/bootstrap.sh"), "run"],
        capture_output=True, text=True, cwd=project, env=os.environ.copy(),
    )
    assert completed.returncode == 0, completed.stderr
    assert "User/project bootstrap completed" in completed.stdout
    assert str(project / ".claude/bootstrap.local.json") in completed.stdout
    assert not (data / "engine_ran_version").exists()


def test_declared_ini_uses_yaml_from_existing_bootstrap_environment(environment):
    import yaml

    home, project, plugin, data = environment
    target = project / "review.ini"
    (home / ".claude/bootstrap.json").write_text(json.dumps({
        "ini_settings": [{"file": "${target}", "section": "Review",
                          "settings": {"value": "yes"}}],
    }))
    (data / "config.yaml").write_text(yaml.safe_dump({"target": str(target)}))
    site_packages = data / ".venv/lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
    site_packages.mkdir(parents=True)
    (site_packages / "yaml").symlink_to(Path(yaml.__file__).parent, target_is_directory=True)
    completed = subprocess.run(
        [sys.executable, "-S", str(RUNNER), "--project-dir", str(project),
         "--plugin-root", str(plugin), "--data-dir", str(data)],
        capture_output=True, text=True, cwd=project,
    )
    assert completed.returncode == 0, completed.stderr
    assert target.exists(), completed.stdout
    assert "value=yes" in target.read_text().replace(" ", "")
