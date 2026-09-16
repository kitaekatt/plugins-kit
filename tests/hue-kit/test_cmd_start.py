"""Truthful verdicts and exit codes (I01): `_call_scene_layers`, `_cmd_start`,
and `main`'s `export` branch.

Every fake `scene-layers.py` subprocess below is a tiny stub script -- never
the real tool, never a real bridge -- and HUE_BRIDGE_IP is pinned by the
`hue_cli` fixture to a TEST-NET address (192.0.2.1, RFC 5737) that the stubs
never contact. No network, no bridge, no HTTP, no mDNS.
"""

from argparse import Namespace

import pytest


def _write_stub(tmp_path, body):
    stub = tmp_path / "stub_scene_layers.py"
    stub.write_text(body)
    return stub


class TestFixtureHygiene:
    """M25: the hue_cli fixture must pin HUE_BRIDGE_IP so a test that forgets
    to stub bridge resolution fails fast instead of quietly reaching
    _discover_bridges. RED before the conftest fix (no HUE_BRIDGE_IP pinned):
    this call fell through to discovery and _boom raised. GREEN after: the
    pinned env short-circuits _resolve_bridge_ip before discovery runs."""

    def test_resolve_bridge_ip_never_reaches_discovery(self, hue_cli, monkeypatch):
        def _boom(*a, **k):
            raise AssertionError(
                "must not reach discovery -- HUE_BRIDGE_IP is pinned by the "
                "hue_cli fixture")

        monkeypatch.setattr(hue_cli, "_discover_bridges", _boom)
        assert hue_cli._resolve_bridge_ip() == "192.0.2.1"


class TestCallSceneLayersStderr:
    """RED step 2: a captured run used to discard the child's stderr
    entirely (capture_output=True captures both streams into proc.stdout/
    proc.stderr, and only proc.stdout was ever returned)."""

    def test_stderr_passes_through_on_a_captured_run(
            self, hue_cli, tmp_path, monkeypatch, capfd):
        stub = _write_stub(tmp_path, (
            "import sys\n"
            "print('boom', file=sys.stderr)\n"
            "sys.exit(1)\n"
        ))
        monkeypatch.setattr(hue_cli, "SCENE_LAYERS", stub)
        monkeypatch.setattr(hue_cli, "_scene_layers_env",
                            lambda workdir: __import__("os").environ.copy())

        rc, out = hue_cli._call_scene_layers(["x"], tmp_path, capture=True)

        captured = capfd.readouterr()
        assert rc == 1
        assert "boom" in captured.err
        assert "boom" not in (out or "")


class TestCmdStartVerdicts:
    """RED steps 3-5: _cmd_start must always print a `hue-kit-verdict:` line
    and must not misclassify a generic scene-layers failure as `changed`."""

    def _established_workdir(self, tmp_path, fingerprint="fp1"):
        (tmp_path / "scene-groups.yaml").write_text("groups: []\n")
        (tmp_path / "scene-designs.yaml").write_text("scenes: []\n")
        (tmp_path / "bridge-fingerprint.txt").write_text(fingerprint + "\n")
        return tmp_path

    def test_generic_validate_failure_is_validate_failed_not_changed(
            self, hue_cli, tmp_path, monkeypatch, capfd):
        """RED step 3: today the CLI maps ANY nonzero --validate-design exit
        to `changed` -- a registry typo (KITCHN) reads as bridge drift and
        routes the agent toward a destructive `hue-kit export` pull."""
        workdir = self._established_workdir(tmp_path)
        stub = _write_stub(tmp_path, (
            "import sys\n"
            "argv = sys.argv[1:]\n"
            "if '--fingerprint' in argv:\n"
            "    print('fp1')\n"
            "    sys.exit(0)\n"
            "if '--validate-design' in argv:\n"
            "    print(\"error: ... unknown group 'KITCHN'\", file=sys.stderr)\n"
            "    sys.exit(1)\n"
            "sys.exit(0)\n"
        ))
        monkeypatch.setattr(hue_cli, "SCENE_LAYERS", stub)

        rc = hue_cli._cmd_start(Namespace(dir=str(workdir), accept=False, open=False))

        captured = capfd.readouterr()
        lines = [ln for ln in captured.out.splitlines() if ln.strip()]
        assert lines[-1] == "hue-kit-verdict: validate-failed"
        assert rc != 0
        assert "KITCHN" in captured.err

    @pytest.mark.parametrize("validate_exit,expected_verdict", [
        (4, "changed"),           # DISCREPANCY_EXIT_CODE: a real diff was found
        (1, "validate-failed"),   # a generic error: never reads as `changed`
    ])
    def test_drift_contract(self, hue_cli, tmp_path, monkeypatch, capfd,
                            validate_exit, expected_verdict):
        """RED step 4: only the distinct discrepancy exit code may produce
        `changed`; a bare nonzero (today's only signal) must not."""
        workdir = self._established_workdir(tmp_path)
        stub = _write_stub(tmp_path, (
            "import os, sys\n"
            "argv = sys.argv[1:]\n"
            "if '--fingerprint' in argv:\n"
            "    print('fp1')\n"
            "    sys.exit(0)\n"
            "if '--validate-design' in argv:\n"
            "    code = int(os.environ.get('STUB_VALIDATE_EXIT', '0'))\n"
            "    if code:\n"
            "        print('1 discrepancies total')\n"
            "    else:\n"
            "        print('0 discrepancies total -- bridge matches the design')\n"
            "    sys.exit(code)\n"
            "sys.exit(0)\n"
        ))
        monkeypatch.setattr(hue_cli, "SCENE_LAYERS", stub)
        monkeypatch.setenv("STUB_VALIDATE_EXIT", str(validate_exit))
        assert hue_cli.DISCREPANCY_EXIT_CODE == 4

        rc = hue_cli._cmd_start(Namespace(dir=str(workdir), accept=False, open=False))

        captured = capfd.readouterr()
        lines = [ln for ln in captured.out.splitlines() if ln.strip()]
        assert lines[-1] == f"hue-kit-verdict: {expected_verdict}"
        if expected_verdict == "validate-failed":
            # `changed` keeps _cmd_start's pre-existing rc=0 (the verdict
            # line is the machine-readable signal; changing that return-code
            # contract is out of this increment's scope). validate-failed
            # must surface scene-layers' own failing exit code, not 0.
            assert rc != 0

    def test_verdict_always_printed_when_bridge_resolution_raises(
            self, hue_cli, tmp_path, monkeypatch, capfd):
        """RED step 5: SystemExit from bridge resolution used to escape
        _cmd_start (and main()) entirely -- no hue-kit-verdict: line at all."""
        monkeypatch.delenv("HUE_BRIDGE_IP", raising=False)
        monkeypatch.setattr(hue_cli, "_discover_bridges", lambda *a, **k: ([], None))
        monkeypatch.setattr(hue_cli, "_mdns_available", lambda: True)

        rc = hue_cli._cmd_start(Namespace(dir=str(tmp_path), accept=False, open=False))

        captured = capfd.readouterr()
        lines = [ln for ln in captured.out.splitlines() if ln.strip()]
        assert rc != 0
        assert lines[-1].startswith("hue-kit-verdict:")
        assert lines[-1] == "hue-kit-verdict: bridge-unreachable"


class TestExportRebaseline:
    """RED step 7: a failed post-export --fingerprint re-baseline was
    silent -- `export` reported success while bridge-fingerprint.txt went
    stale, so `start` would keep reporting a shape change forever with no
    clue why."""

    def test_failed_rebaseline_warns_on_stderr(
            self, hue_cli, tmp_path, monkeypatch, capfd):
        stub = _write_stub(tmp_path, (
            "import sys\n"
            "argv = sys.argv[1:]\n"
            "if '--export-designs' in argv:\n"
            "    sys.exit(0)\n"
            "if '--fingerprint' in argv:\n"
            "    sys.exit(1)\n"
            "sys.exit(0)\n"
        ))
        monkeypatch.setattr(hue_cli, "SCENE_LAYERS", stub)

        rc = hue_cli.main(["--dir", str(tmp_path), "export"])

        captured = capfd.readouterr()
        assert rc == 0  # the export itself succeeded
        assert "re-baseline" in captured.err
        assert "1" in captured.err
