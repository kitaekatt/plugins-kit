"""hue_kit_cli.py fails the same way whether bootstrap never ran for this
plugin or ran once and the venv it built is gone.

The bootstrap-log check in ``main()`` only proves bootstrap reached this
plugin at some point; it says nothing about whether the venv from that pass
is still on disk. Without a second check, a missing venv surfaces as a raw
``ModuleNotFoundError`` from whichever child happens to hit the absent
dependency first -- ``requests`` inside ``_cmd_pair``, or scene-layers.py
once handed off to it.

Every case here runs the real script as a subprocess (an interpreter path
plus the script path, no shell) under an interpreter that lacks ``requests``,
with ``CLAUDE_BOOTSTRAP_DATA_ROOT`` pointed at a tree that records one
bootstrap pass (a ``bootstrap.log``) but carries no ``.venv``. Offline:
``HUE_BRIDGE_IP`` is pinned to a documentation-only address (RFC 5737) so
nothing tries to reach a real bridge, and the child never gets far enough to
try anyway.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "plugins" / "hue-kit" / "scripts" / "hue_kit_cli.py"

EXIT_BOOTSTRAP_MISSING = 3

CANONICAL_PAIR_MESSAGE = (
    "[hue-kit] the 'plugins-kit:bootstrap' plugin has not provisioned "
    "hue-kit's setup (missing: requests). Install/enable the bootstrap "
    "plugin and start a new session so it can build this plugin's "
    "dependencies, then retry."
)
CANONICAL_SCENE_TOOLING_MESSAGE = (
    "[hue-kit] the 'plugins-kit:bootstrap' plugin has not provisioned "
    "hue-kit's scene tooling. Install/enable the bootstrap plugin and "
    "start a new session so it can build this plugin's dependencies, "
    "then retry."
)


@pytest.fixture(scope="module", autouse=True)
def _interpreter_under_test_lacks_requests():
    """Every case in this module needs an interpreter that fails the import
    the fix is guarding against. Skip rather than fake the failure in
    process, so a machine whose test interpreter happens to carry
    ``requests`` gets an honest skip instead of a misleading pass."""
    probe = subprocess.run([sys.executable, "-c", "import requests"],
                          capture_output=True)
    if probe.returncode == 0:
        pytest.skip("sys.executable already has requests installed; this "
                    "module needs an interpreter that does not")


def _provisioned_no_venv(tmp_path: Path) -> Path:
    """A data root recording one bootstrap pass (``bootstrap.log`` present)
    with no ``.venv`` beneath it: the shape the guard has to tell apart from
    a plugin bootstrap never reached at all."""
    data_root = tmp_path / "data"
    plugin_dir = data_root / "plugins-kit" / "hue-kit"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "bootstrap.log").write_text("provisioned\n", encoding="utf-8")
    return data_root


def _run(tmp_path: Path, data_root: Path,
        *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    # Another plugin's conftest sets this process-wide to disable the
    # re-exec loop guard for its own subprocess probes; drop it here so this
    # child starts from a clean slate rather than inheriting that plugin's
    # assumption.
    env.pop("_BOOTSTRAP_GUARD_VENV_REEXEC", None)
    env["CLAUDE_BOOTSTRAP_DATA_ROOT"] = str(data_root)
    env["HUE_BRIDGE_IP"] = "192.0.2.1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=tmp_path, capture_output=True, text=True, env=env, timeout=60)


class TestPairReportsTheBootstrapAbsence:
    """``pair`` imports ``requests`` directly; a missing venv must surface
    through the same guard as every other absence, not a bare traceback."""

    def test_pair_exits_3_with_the_canonical_message(self, tmp_path):
        data_root = _provisioned_no_venv(tmp_path)
        run = _run(tmp_path, data_root, "pair")
        assert run.returncode == EXIT_BOOTSTRAP_MISSING, run.stderr
        assert CANONICAL_PAIR_MESSAGE in run.stderr
        assert "Traceback" not in run.stderr


class TestSceneLayersVerbsCheckTheVenvFirst:
    """``report`` reaches scene-layers.py through the exec runner;
    ``export`` reaches it through the subprocess runner. Both must stop at
    the same check before either runner spawns anything."""

    @pytest.mark.parametrize("verb", ["report", "export"])
    def test_verb_exits_3_before_spawning_scene_layers(self, tmp_path, verb):
        data_root = _provisioned_no_venv(tmp_path)
        run = _run(tmp_path, data_root, "--dir", str(tmp_path), verb)
        assert run.returncode == EXIT_BOOTSTRAP_MISSING, run.stderr
        assert CANONICAL_SCENE_TOOLING_MESSAGE in run.stderr
        assert "Traceback" not in run.stderr
