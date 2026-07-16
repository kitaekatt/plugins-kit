"""Drift test: plugins/<name>/pyproject.toml version must equal the
authoritative plugins/<name>/.claude-plugin/plugin.json version (X17).

The rule itself lives in scripts/check_pyproject_sync.py and is loaded from
there rather than reimplemented, because that script is what the pre-commit
hook runs -- a second copy here could pass while the gate that actually blocks
the commit disagreed. Auto-discovers plugins and compares the two files rather
than pinning numbers, so a normal publish bump (edit both files) never touches
this test. Plugins without a pyproject.toml, or whose pyproject declares no
version, are out of scope -- pyproject versions are non-authoritative, the rule
is just "if you state one, it must not lie".

This test alone was never enough: it only fails a FULL suite run, which
CLAUDE.md tells you not to do routinely and publish.py does not do at all, so
bootstrap drifted across five releases before anyone noticed. It is the spec;
the hook is the enforcement. See the script's header.
"""

import importlib.util
import json
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_pyproject_sync.py"


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_pyproject_sync", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_checker = _load_checker()


def test_discovery_finds_plugins():
    # Vacuity guard for the drift assertion below.
    assert _checker.plugins_with_both_files(), (
        "no plugins with pyproject + plugin.json found")


def test_pyproject_versions_match_plugin_json():
    drift = _checker.find_drift()
    assert not drift, (
        "pyproject.toml versions drifted from the authoritative plugin.json "
        "(set them equal; plugin.json is the source of truth):\n  "
        + "\n  ".join(drift)
    )


class TestTheRuleItself:
    """The checker is now a commit gate, so its verdict has to be right: a
    false positive blocks every commit in the repo, a false negative is the
    hole that let bootstrap drift across five releases.
    """

    def _plugin(self, root, name, py_version, pj_version):
        d = root / name
        (d / ".claude-plugin").mkdir(parents=True)
        body = '[project]\nname = "x"\n'
        if py_version is not None:
            body += f'version = "{py_version}"\n'
        (d / "pyproject.toml").write_text(body)
        (d / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": name, "version": pj_version}))
        return d

    def test_matching_versions_are_not_drift(self, tmp_path):
        self._plugin(tmp_path, "ok", "1.2.3", "1.2.3")
        assert _checker.find_drift(tmp_path) == []

    def test_a_stated_version_that_lies_is_drift(self, tmp_path):
        self._plugin(tmp_path, "bad", "0.43.0", "0.44.0")
        assert _checker.find_drift(tmp_path) == [
            "bad: pyproject.toml=0.43.0 plugin.json=0.44.0"]

    def test_a_versionless_pyproject_is_out_of_scope(self, tmp_path):
        """Non-authoritative by design: state nothing, lie about nothing."""
        self._plugin(tmp_path, "quiet", None, "0.44.0")
        assert _checker.find_drift(tmp_path) == []

    def test_a_plugin_without_pyproject_is_out_of_scope(self, tmp_path):
        d = tmp_path / "nopy" / ".claude-plugin"
        d.mkdir(parents=True)
        (d / "plugin.json").write_text(json.dumps({"name": "nopy", "version": "1"}))
        assert _checker.find_drift(tmp_path) == []

    def test_every_drifting_plugin_is_reported_not_just_the_first(self, tmp_path):
        """A publish can bump several plugins; naming one and stopping would
        turn one fix-and-retry cycle into several."""
        self._plugin(tmp_path, "a", "1.0.0", "2.0.0")
        self._plugin(tmp_path, "b", "3.0.0", "4.0.0")
        assert len(_checker.find_drift(tmp_path)) == 2
