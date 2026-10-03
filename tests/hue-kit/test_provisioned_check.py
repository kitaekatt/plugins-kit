"""hue_kit_cli.py refuses missing redirected interpreters before imports.

The bootstrap-log check in ``main()`` only proves bootstrap reached this
plugin at some point; it says nothing about whether the venv from that pass
is still on disk. Without a second check, a missing venv surfaces as a raw
``ModuleNotFoundError`` from whichever child happens to hit the absent
dependency first -- ``requests`` inside ``_cmd_pair``, or scene-layers.py
once handed off to it.

Redirected cases expect the shared guard's exit 2; ordinary cases retain the
CLI's canonical exit 3. Every case runs the real script with ``-S`` so site
packages cannot provide ``requests``, against an isolated home recording one
bootstrap pass (a ``bootstrap.log``) but carrying no ``.venv``. Offline:
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


def _provisioned_no_venv(tmp_path: Path) -> Path:
    """A data root recording one bootstrap pass (``bootstrap.log`` present)
    with no ``.venv`` beneath it: the shape the guard has to tell apart from
    a plugin bootstrap never reached at all."""
    data_root = tmp_path / ".claude" / "plugins" / "data"
    plugin_dir = data_root / "plugins-kit" / "hue-kit"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "bootstrap.log").write_text("provisioned\n", encoding="utf-8")
    return data_root


def _run(tmp_path: Path, data_root: Path | None,
        *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    # Another plugin's conftest sets this process-wide to disable the
    # re-exec loop guard for its own subprocess probes; drop it here so this
    # child starts from a clean slate rather than inheriting that plugin's
    # assumption.
    env.pop("_BOOTSTRAP_GUARD_VENV_REEXEC", None)
    env["HOME"] = str(tmp_path)
    env["USERPROFILE"] = str(tmp_path)
    env.pop("PYTHONPATH", None)
    env.pop("CLAUDE_BOOTSTRAP_DATA_ROOT", None)
    if data_root is not None:
        env["CLAUDE_BOOTSTRAP_DATA_ROOT"] = str(data_root)
    env["HUE_BRIDGE_IP"] = "192.0.2.1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, "-S", str(SCRIPT), *args],
        cwd=tmp_path, capture_output=True, text=True, env=env, timeout=60)


class TestPairReportsTheBootstrapAbsence:
    """``pair`` imports ``requests`` directly; a missing venv must surface
    through the same guard as every other absence, not a bare traceback."""

    @pytest.mark.parametrize("redirected", [False, True])
    def test_pair_reports_missing_environment(self, tmp_path, redirected):
        data_root = _provisioned_no_venv(tmp_path)
        run = _run(tmp_path, data_root if redirected else None, "pair")
        if redirected:
            assert run.returncode == 2, run.stderr
            assert "CLAUDE_BOOTSTRAP_DATA_ROOT is set" in run.stderr
            assert str(data_root / "plugins-kit" / "hue-kit" / ".venv") in run.stderr
        else:
            assert run.returncode == EXIT_BOOTSTRAP_MISSING, run.stderr
            assert CANONICAL_PAIR_MESSAGE in run.stderr
        assert "Traceback" not in run.stderr


class TestSceneLayersVerbsCheckTheVenvFirst:
    """``report`` reaches scene-layers.py through the exec runner;
    ``export`` reaches it through the subprocess runner. Both must stop at
    the same check before either runner spawns anything."""

    @pytest.mark.parametrize("verb", ["report", "export"])
    @pytest.mark.parametrize("redirected", [False, True])
    def test_verb_refuses_before_spawning_scene_layers(self, tmp_path, verb, redirected):
        data_root = _provisioned_no_venv(tmp_path)
        run = _run(tmp_path, data_root if redirected else None, "--dir", str(tmp_path), verb)
        if redirected:
            assert run.returncode == 2, run.stderr
            assert "CLAUDE_BOOTSTRAP_DATA_ROOT is set" in run.stderr
            assert str(data_root / "plugins-kit" / "hue-kit" / ".venv") in run.stderr
        else:
            assert run.returncode == EXIT_BOOTSTRAP_MISSING, run.stderr
            assert CANONICAL_SCENE_TOOLING_MESSAGE in run.stderr
        assert "Traceback" not in run.stderr
