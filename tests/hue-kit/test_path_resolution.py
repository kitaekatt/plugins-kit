"""One path resolution for the working files.

The CLI must honour HUE_GROUPS_FILE / HUE_DESIGNS_FILE for existence checks
AND write targets (not just the child's env), and standalone scene-layers.py
must resolve its own defaults from the cwd -- never from __file__, and never
falling back to the dead `references/` probe or the legacy
`secrets/hue-bridge-key.txt` path. No network, no bridge, no HTTP, no mDNS.
"""

import importlib.util
import sys
import types
from argparse import Namespace
from pathlib import Path

import pytest

_SCRIPTS = (Path(__file__).resolve().parent.parent.parent
            / "plugins" / "hue-kit" / "scripts")


class TestCliWritesHonourEnvOverride:
    """`export` / `groups` must honour an HUE_*_FILE override for their
    write target, not just <dir>/<name>, matching what reads already do."""

    def test_export_target_is_the_resolved_HUE_DESIGNS_FILE(
            self, hue_cli, tmp_path, monkeypatch):
        elsewhere = tmp_path / "elsewhere" / "designs.yaml"
        monkeypatch.setenv("HUE_DESIGNS_FILE", str(elsewhere))
        calls = []

        def fake_call(flags, workdir, *, capture=False):
            calls.append(list(flags))
            return 0, "fp"

        monkeypatch.setattr(hue_cli, "_call_scene_layers", fake_call)

        rc = hue_cli.main(["--dir", str(tmp_path), "export"])

        assert rc == 0
        assert calls[0] == ["--export-designs", str(elsewhere.resolve())]

    def test_groups_target_is_the_resolved_HUE_GROUPS_FILE(
            self, hue_cli, tmp_path, monkeypatch):
        elsewhere = tmp_path / "elsewhere" / "groups.yaml"
        monkeypatch.setenv("HUE_GROUPS_FILE", str(elsewhere))
        seen = {}

        def fake_run(flags, workdir):
            seen["flags"] = list(flags)
            return 0

        monkeypatch.setattr(hue_cli, "_run_scene_layers", fake_run)

        hue_cli.main(["--dir", str(tmp_path), "groups"])

        assert seen["flags"] == ["--export-groups", str(elsewhere.resolve())]


class TestCmdStartFirstRunDetectionHonoursEnvOverride:
    """`_cmd_start` must not check only <dir>/scene-groups.yaml /
    <dir>/scene-designs.yaml directly -- an env-overridden pair that exists
    ELSEWHERE must not read as first-run when the working files are already
    established."""

    def test_established_via_env_override_is_not_first_run(
            self, hue_cli, tmp_path, monkeypatch):
        elsewhere_groups = tmp_path / "elsewhere-groups.yaml"
        elsewhere_designs = tmp_path / "elsewhere-designs.yaml"
        elsewhere_groups.write_text("groups: []\n")
        elsewhere_designs.write_text("scenes: []\n")
        monkeypatch.setenv("HUE_GROUPS_FILE", str(elsewhere_groups))
        monkeypatch.setenv("HUE_DESIGNS_FILE", str(elsewhere_designs))

        workdir = tmp_path / "workdir"
        workdir.mkdir()  # empty -- no scene-groups.yaml / scene-designs.yaml here

        seen_flags = []

        def fake_call(flags, workdir_arg, *, capture=False):
            seen_flags.append(list(flags))
            if "--fingerprint" in flags:
                return 0, "fp1"
            if "--validate-design" in flags:
                return 0, "0 discrepancies total -- bridge matches the design"
            return 0, None

        monkeypatch.setattr(hue_cli, "_call_scene_layers", fake_call)

        hue_cli._cmd_start(Namespace(dir=str(workdir), accept=False, open=False))

        assert any("--validate-design" in f for f in seen_flags), (
            "the env-overridden files exist, so this must be treated as an "
            "established run"
        )
        assert not any("--export-groups" in f for f in seen_flags), (
            "must not re-run first-run setup when the env-overridden working "
            "files already exist"
        )


class TestSceneLayersFreshDefaultsAreCwdBased:
    """Loaded fresh with no env override, GROUPS_YAML/DESIGNS_YAML must not
    fall back to a path under this repo (../references/<name> or
    <script-dir>/<name>) -- i.e. derived from __file__. They must resolve
    against the cwd instead, and standalone --export-designs (no PATH) must
    fail argparse usage (exit 2) rather than silently defaulting and reaching
    the bridge."""

    def test_cwd_defaults_and_export_designs_requires_a_path(
            self, tmp_path, monkeypatch):
        if "requests" not in sys.modules:
            requests_stub = types.ModuleType("requests")
            requests_stub.Session = object
            monkeypatch.setitem(sys.modules, "requests", requests_stub)
        if "urllib3" not in sys.modules:
            urllib3_stub = types.ModuleType("urllib3")
            urllib3_stub.exceptions = types.SimpleNamespace(
                InsecureRequestWarning=Warning)
            urllib3_stub.disable_warnings = lambda *a, **k: None
            monkeypatch.setitem(sys.modules, "urllib3", urllib3_stub)

        monkeypatch.delenv("HUE_GROUPS_FILE", raising=False)
        monkeypatch.delenv("HUE_DESIGNS_FILE", raising=False)
        monkeypatch.chdir(tmp_path)

        mod_name = "hue_scene_layers_i03_fresh"
        path = _SCRIPTS / "scene-layers.py"
        spec = importlib.util.spec_from_file_location(mod_name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        try:
            spec.loader.exec_module(module)

            plugin_root = str(_SCRIPTS.parent)  # plugins/hue-kit
            assert not str(module.GROUPS_YAML).startswith(plugin_root)
            assert not str(module.DESIGNS_YAML).startswith(plugin_root)
            assert module.GROUPS_YAML == tmp_path / "scene-groups.yaml"
            assert module.DESIGNS_YAML == tmp_path / "scene-designs.yaml"

            def _boom(*a, **k):  # pragma: no cover - must never run
                raise AssertionError("must not reach extract_from_bridge")

            monkeypatch.setattr(module, "extract_from_bridge", _boom)
            monkeypatch.setattr(sys, "argv", ["x", "--export-designs"])

            with pytest.raises(SystemExit) as excinfo:
                module.main()
            assert excinfo.value.code == 2
        finally:
            sys.modules.pop(mod_name, None)


class TestBridgeSessionNoKeyNamesPairNotSecrets:
    """With no HUE_APP_KEY, no HUE_KEY_FILE, and no paired key file, the
    error must not point at secrets/hue-bridge-key.txt (a cwd-relative
    fallback); it must instead name `hue-kit pair` and never mention
    secrets/."""

    def test_no_key_configured(self, scene_layers, tmp_path, monkeypatch):
        monkeypatch.delenv("HUE_APP_KEY", raising=False)
        monkeypatch.delenv("HUE_KEY_FILE", raising=False)
        monkeypatch.setattr(scene_layers, "PAIRED_KEY_FILE",
                            tmp_path / "app-key.txt")

        with pytest.raises(SystemExit) as excinfo:
            scene_layers.bridge_session()

        msg = str(excinfo.value)
        assert "hue-kit pair" in msg
        assert "secrets/" not in msg
