"""The environment scripts/claude_plugin_test.py (claudx) gives the session it launches.

``_session_env`` layers claudx's own variables over a copy of the launching
shell's environment. What the launching shell already carries is the hazard: a
normal session exports ``<PLUGIN>_ROOT`` for every installed plugin (a cached
release), and a process started from a re-exec'd plugin carries the interpreter
guard's loop flag. Neither may reach a session meant to test the working copy.
The launcher also exports the plugin directories it loads, so a nested claude
child can rebuild the same ``--plugin-dir`` pairs.

Everything here is a pure function over dicts. No claude is launched; the one
test that drives ``main()`` replaces ``subprocess.call`` and redirects the
synthetic registry into tmp_path.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "claude_plugin_test.py"

# Not in this tree: a plugin of another marketplace, installed in its own cache.
THIRD_PARTY_ROOT = "ENGINEER_ROOT"


@pytest.fixture(scope="module")
def launcher():
    spec = importlib.util.spec_from_file_location("claude_plugin_test_env_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _root_var(name: str) -> str:
    from bootstrap_lib.env_var_check import plugin_root_env_var_name
    return plugin_root_env_var_name(name)


def _tree_names(launcher) -> list[str]:
    names = [name for name, _version, _dir in launcher._tree_plugins()]
    assert len(names) > 3, "the working copy must hold plugins for this test to mean anything"
    return names


def _cache(tmp_path: Path, marketplace: str, plugin: str) -> str:
    return str(tmp_path / "plugins" / "cache" / marketplace / plugin / "1.0.0")


def _dirs(tmp_path: Path, *names: str) -> list[Path]:
    return [tmp_path / "wc" / n for n in names]


def _env(launcher, tmp_path, inherited, plugin_dirs=None):
    return launcher._session_env(
        dict(inherited), tmp_path / "data", "plugins-kit",
        plugin_dirs=_dirs(tmp_path, "a", "b") if plugin_dirs is None else plugin_dirs)


class TestInheritedPluginRoots:
    def test_every_working_copy_plugin_root_is_dropped(self, launcher, tmp_path):
        names = _tree_names(launcher)
        inherited = {_root_var(n): _cache(tmp_path, "plugins-kit", n) for n in names}
        env = _env(launcher, tmp_path, inherited)
        assert [v for v in inherited if v in env] == []

    def test_a_dev_tree_root_is_dropped_whatever_its_value(self, launcher, tmp_path):
        # A root already pointing at the working copy is dropped too: the session
        # exports its own, and an inherited one is not evidence of which it is.
        inherited = {"SKILLS_KIT_ROOT": str(REPO_ROOT / "plugins" / "skills-kit"),
                     "HUE_KIT_ROOT": _cache(tmp_path, "some-other-marketplace", "hue-kit")}
        env = _env(launcher, tmp_path, inherited)
        assert "SKILLS_KIT_ROOT" not in env and "HUE_KIT_ROOT" not in env

    def test_a_plugin_root_outside_the_working_copy_is_kept(self, launcher, tmp_path):
        assert THIRD_PARTY_ROOT.removesuffix("_ROOT").lower() not in [
            n.replace("-", "_") for n in _tree_names(launcher)]
        third_party = _cache(tmp_path, "other-marketplace", "engineer")
        inherited = {THIRD_PARTY_ROOT: third_party,
                     "SKILLS_KIT_ROOT": _cache(tmp_path, "plugins-kit", "skills-kit"),
                     "UNRELATED": "x"}
        env = _env(launcher, tmp_path, inherited)
        assert env[THIRD_PARTY_ROOT] == third_party
        assert env["UNRELATED"] == "x"
        assert "SKILLS_KIT_ROOT" not in env

    def test_an_explicit_name_list_decides_what_is_dropped(self, launcher, tmp_path):
        inherited = {"SKILLS_KIT_ROOT": "s", "HUE_KIT_ROOT": "h"}
        env = launcher._session_env(
            dict(inherited), tmp_path / "data", "plugins-kit",
            plugin_dirs=_dirs(tmp_path, "a"), tree_plugin_names=["hue-kit"])
        assert "HUE_KIT_ROOT" not in env and env["SKILLS_KIT_ROOT"] == "s"


class TestInheritedGuardVariables:
    @pytest.mark.parametrize("value", ["1", "/some/venv/bin/python"])
    def test_the_loop_flag_is_dropped(self, launcher, tmp_path, value):
        env = _env(launcher, tmp_path, {"_BOOTSTRAP_GUARD_VENV_REEXEC": value})
        assert "_BOOTSTRAP_GUARD_VENV_REEXEC" not in env

    def test_the_skip_variable_is_dropped(self, launcher, tmp_path):
        env = _env(launcher, tmp_path, {"_BOOTSTRAP_GUARD_SKIP_REEXEC": "1"})
        assert "_BOOTSTRAP_GUARD_SKIP_REEXEC" not in env

    def test_the_launcher_names_the_flag_the_guard_reads(self, launcher):
        import bootstrap_lib.bootstrap_guard as guard
        assert launcher.GUARD_LOOP_FLAG_ENV == guard._REEXEC_GUARD_ENV


class TestSessionVariables:
    def test_the_launcher_variables_are_set(self, launcher, tmp_path):
        env = _env(launcher, tmp_path, {})
        assert env["CLAUDE_PLUGIN_TEST"] == "1"
        assert env["CLAUDE_BOOTSTRAP_DATA_ROOT"] == str(tmp_path / "data")
        assert env["CLAUDE_PLUGIN_TEST_MARKETPLACE"] == "plugins-kit"

    def test_the_plugin_directories_are_exported_in_order(self, launcher, tmp_path):
        dirs = _dirs(tmp_path, "zeta", "alpha", "mid")
        env = _env(launcher, tmp_path, {}, plugin_dirs=dirs)
        assert env["CLAUDE_PLUGIN_TEST_DIRS"] == os.pathsep.join(str(d) for d in dirs)
        assert env["CLAUDE_PLUGIN_TEST_DIRS"].split(os.pathsep) == [str(d) for d in dirs]

    def test_an_inherited_directory_list_is_replaced(self, launcher, tmp_path):
        env = _env(launcher, tmp_path, {"CLAUDE_PLUGIN_TEST_DIRS": str(tmp_path / "stale")},
                   plugin_dirs=_dirs(tmp_path, "fresh"))
        assert env["CLAUDE_PLUGIN_TEST_DIRS"] == str(tmp_path / "wc" / "fresh")

    @pytest.mark.parametrize("bad", [[], [Path("relative/plugin")]])
    def test_a_list_that_cannot_be_exported_raises(self, launcher, tmp_path, bad):
        with pytest.raises(ValueError, match="CLAUDE_PLUGIN_TEST_DIRS"):
            launcher._session_env({}, tmp_path / "data", "plugins-kit", plugin_dirs=bad)


class TestMain:
    """main() hands the scrubbed env, and dirs equal to its --plugin-dir pairs, to claude."""

    def test_the_launched_command_and_environment_agree(self, launcher, tmp_path, monkeypatch):
        names = _tree_names(launcher)
        for n in names:
            monkeypatch.setenv(_root_var(n), _cache(tmp_path, "plugins-kit", n))
        monkeypatch.setenv(THIRD_PARTY_ROOT, "third-party")
        monkeypatch.setenv("_BOOTSTRAP_GUARD_VENV_REEXEC", "1")
        monkeypatch.setenv("_BOOTSTRAP_GUARD_SKIP_REEXEC", "1")
        monkeypatch.setattr(launcher, "SYNTHETIC_REGISTRY", tmp_path / "installed_plugins.json")
        captured = {}

        def fake_call(cmd, env):
            captured["cmd"], captured["env"] = cmd, env
            return 0

        monkeypatch.setattr(launcher.subprocess, "call", fake_call)
        monkeypatch.setattr(sys, "argv", [
            "claude_plugin_test.py", "--all", "--data-root", str(tmp_path / "data")])

        assert launcher.main() == 0

        cmd, env = captured["cmd"], captured["env"]
        launched = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--plugin-dir"]
        assert launched, "the command must carry --plugin-dir pairs"
        assert env["CLAUDE_PLUGIN_TEST_DIRS"].split(os.pathsep) == launched
        assert env["CLAUDE_PLUGIN_TEST"] == "1"
        assert env["CLAUDE_BOOTSTRAP_DATA_ROOT"] == str(tmp_path / "data")
        assert [_root_var(n) for n in names if _root_var(n) in env] == []
        assert "_BOOTSTRAP_GUARD_VENV_REEXEC" not in env
        assert "_BOOTSTRAP_GUARD_SKIP_REEXEC" not in env
        assert env[THIRD_PARTY_ROOT] == "third-party"


FIXTURE_DIST = "katsoft-fixture"
FIXTURE_REGISTRY = f"""\
schema = 1
repository = "https://example.invalid/fixture"

[[package]]
name = "{FIXTURE_DIST}"
import = "fixlib"
marker = "fixlib/_release.py"
sources = ["lib/fixlib"]
surface = {{ all = "lib/fixlib/__init__.py" }}
"""


def _git(root: Path, *args: str) -> str:
    import subprocess
    out = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                          *args], cwd=root, check=True, capture_output=True, text=True)
    return out.stdout.strip()


@pytest.fixture
def released_repo(tmp_path):
    """A git repo whose newest ledger entry names the commit holding its build inputs."""
    import json
    root = tmp_path / "repo"
    (root / "packages" / FIXTURE_DIST).mkdir(parents=True)
    (root / "lib" / "fixlib").mkdir(parents=True)
    (root / "packages" / "registry.toml").write_text(FIXTURE_REGISTRY, encoding="utf-8")
    (root / "packages" / FIXTURE_DIST / "pyproject.toml").write_text(
        '[project]\nname = "katsoft-fixture"\nversion = "2.0.0"\n', encoding="utf-8")
    (root / "lib" / "fixlib" / "__init__.py").write_text("__all__ = []\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "release inputs")
    commit = _git(root, "rev-parse", "HEAD")
    ledger = {FIXTURE_DIST: {"1.9.0": {"commit": "0" * 40, "version": "1.9.0"},
                             "2.0.0": {"commit": commit, "version": "2.0.0"}}}
    (root / "packages" / "released.json").write_text(json.dumps(ledger), encoding="utf-8")
    return root


class TestDriftedPackages:
    """The note names a registered package whose working tree differs from its newest release."""

    def test_a_package_matching_its_release_is_not_named(self, launcher, released_repo):
        assert launcher.drifted_packages(released_repo) == []

    def test_an_edited_source_is_named_with_the_newest_version(self, launcher, released_repo):
        (released_repo / "lib" / "fixlib" / "__init__.py").write_text(
            "__all__ = ['x']\n", encoding="utf-8")
        assert launcher.drifted_packages(released_repo) == [(FIXTURE_DIST, "2.0.0")]

    def test_a_new_untracked_source_file_is_named(self, launcher, released_repo):
        (released_repo / "lib" / "fixlib" / "extra.py").write_text("x = 1\n", encoding="utf-8")
        assert launcher.drifted_packages(released_repo) == [(FIXTURE_DIST, "2.0.0")]

    def test_a_changed_registry_entry_is_named(self, launcher, released_repo):
        reg = released_repo / "packages" / "registry.toml"
        reg.write_text(FIXTURE_REGISTRY.replace('marker = "fixlib/_release.py"',
                                                'marker = "fixlib/_marker.py"'),
                       encoding="utf-8")
        assert launcher.drifted_packages(released_repo) == [(FIXTURE_DIST, "2.0.0")]

    def test_a_change_outside_the_build_inputs_is_not_named(self, launcher, released_repo):
        (released_repo / "README.md").write_text("unrelated\n", encoding="utf-8")
        assert launcher.drifted_packages(released_repo) == []

    def test_the_note_text_names_the_distribution_and_version(self, launcher, released_repo,
                                                              capsys):
        (released_repo / "lib" / "fixlib" / "__init__.py").write_text(
            "__all__ = ['x']\n", encoding="utf-8")
        launcher._note_drifted_packages(released_repo)
        err = capsys.readouterr().err
        assert f"plugin venvs use {FIXTURE_DIST} 2.0.0 from PyPI" in err
        assert "working-tree changes to it are not loaded" in err

    def test_no_note_is_printed_for_a_matching_package(self, launcher, released_repo, capsys):
        launcher._note_drifted_packages(released_repo)
        assert capsys.readouterr().err == ""

    def test_main_prints_the_note_before_launching(self, launcher, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(launcher, "drifted_packages", lambda root=None: [("katsoft-x", "3.1.4")])
        monkeypatch.setattr(sys, "argv", ["claude_plugin_test.py", "--all", "--print"])
        assert launcher.main() == 0
        assert "plugin venvs use katsoft-x 3.1.4 from PyPI" in capsys.readouterr().err
