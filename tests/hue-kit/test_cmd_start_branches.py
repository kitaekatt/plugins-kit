"""_cmd_start's first-run, accepted, clean and render-failed verdicts.

validate-failed, changed and bridge-unreachable are pinned in
test_cmd_start.py; this module covers the remaining four documented
verdicts, each driven by a tiny stub `scene-layers.py` subprocess (never the
real tool, never a bridge) and asserting on the printed
`hue-kit-verdict:` line and _cmd_start's return code.
"""

from __future__ import annotations

from argparse import Namespace


def _write_stub(tmp_path, body):
    stub = tmp_path / "stub_scene_layers.py"
    stub.write_text(body)
    return stub


def _established_workdir(tmp_path, fingerprint="fp1"):
    (tmp_path / "scene-groups.yaml").write_text("groups: []\n")
    (tmp_path / "scene-designs.yaml").write_text("scenes: []\n")
    (tmp_path / "bridge-fingerprint.txt").write_text(fingerprint + "\n")
    return tmp_path


class TestFirstRun:
    """No working files yet: _cmd_start must build the registry, the
    design, and the report (three separate scene-layers.py calls), then
    baseline the fingerprint and report `first-run` -- the one branch that
    writes without an existing baseline to compare against."""

    def test_first_run_builds_all_three_files_and_baselines_fingerprint(
            self, hue_cli, tmp_path, monkeypatch, capfd):
        calls = []
        stub = _write_stub(tmp_path, (
            "import sys, pathlib\n"
            "argv = sys.argv[1:]\n"
            "if '--fingerprint' in argv:\n"
            "    print('fpX')\n"
            "    sys.exit(0)\n"
            "if '--export-groups' in argv:\n"
            "    pathlib.Path(argv[argv.index('--export-groups') + 1]).write_text('g\\n')\n"
            "    sys.exit(0)\n"
            "if '--export-designs' in argv:\n"
            "    pathlib.Path(argv[argv.index('--export-designs') + 1]).write_text('d\\n')\n"
            "    sys.exit(0)\n"
            "if '--html' in argv:\n"
            "    pathlib.Path(argv[argv.index('--html') + 1]).write_text('h\\n')\n"
            "    sys.exit(0)\n"
            "sys.exit(0)\n"
        ))
        monkeypatch.setattr(hue_cli, "SCENE_LAYERS", stub)

        rc = hue_cli._cmd_start(
            Namespace(dir=str(tmp_path), accept=False, open=False))

        captured = capfd.readouterr()
        lines = [ln for ln in captured.out.splitlines() if ln.strip()]
        assert lines[-1] == "hue-kit-verdict: first-run"
        assert rc == 0
        assert (tmp_path / "scene-groups.yaml").read_text() == "g\n"
        assert (tmp_path / "scene-designs.yaml").read_text() == "d\n"
        assert (tmp_path / "index.html").read_text() == "h\n"
        assert (tmp_path / "bridge-fingerprint.txt").read_text() == "fpX\n"

    def test_first_run_stops_and_reports_setup_failed_when_a_step_fails(
            self, hue_cli, tmp_path, monkeypatch, capfd):
        """A failure while building the registry must not be papered over as
        first-run success -- and must not go on to try the design/report
        steps against a registry that was never written."""
        stub = _write_stub(tmp_path, (
            "import sys\n"
            "argv = sys.argv[1:]\n"
            "if '--fingerprint' in argv:\n"
            "    print('fpX')\n"
            "    sys.exit(0)\n"
            "if '--export-groups' in argv:\n"
            "    sys.exit(1)\n"
            "sys.exit(0)\n"
        ))
        monkeypatch.setattr(hue_cli, "SCENE_LAYERS", stub)

        rc = hue_cli._cmd_start(
            Namespace(dir=str(tmp_path), accept=False, open=False))

        captured = capfd.readouterr()
        lines = [ln for ln in captured.out.splitlines() if ln.strip()]
        assert lines[-1] == "hue-kit-verdict: setup-failed"
        assert rc == 1
        assert not (tmp_path / "scene-designs.yaml").exists()


class TestAccepted:
    """--accept re-baselines bridge-fingerprint.txt to the bridge's current
    shape without touching either YAML file, regardless of what the old
    fingerprint held."""

    def test_accept_rewrites_only_the_fingerprint(self, hue_cli, tmp_path,
                                                    monkeypatch, capfd):
        workdir = _established_workdir(tmp_path, fingerprint="old-fp")
        stub = _write_stub(tmp_path, (
            "import sys\n"
            "if '--fingerprint' in sys.argv[1:]:\n"
            "    print('new-fp')\n"
            "sys.exit(0)\n"
        ))
        monkeypatch.setattr(hue_cli, "SCENE_LAYERS", stub)
        groups_before = (workdir / "scene-groups.yaml").read_text()
        designs_before = (workdir / "scene-designs.yaml").read_text()

        rc = hue_cli._cmd_start(
            Namespace(dir=str(workdir), accept=True, open=False))

        captured = capfd.readouterr()
        lines = [ln for ln in captured.out.splitlines() if ln.strip()]
        assert lines[-1] == "hue-kit-verdict: accepted"
        assert rc == 0
        assert (workdir / "bridge-fingerprint.txt").read_text() == "new-fp\n"
        assert (workdir / "scene-groups.yaml").read_text() == groups_before
        assert (workdir / "scene-designs.yaml").read_text() == designs_before


class TestClean:
    """Fingerprint unchanged and --validate-design finds nothing: an
    existing report is left alone (never re-rendered) and the verdict is
    `clean`."""

    def test_clean_when_fingerprint_and_design_both_match(
            self, hue_cli, tmp_path, monkeypatch, capfd):
        workdir = _established_workdir(tmp_path, fingerprint="fp1")
        (workdir / "index.html").write_text("EXISTING REPORT\n")
        stub = _write_stub(tmp_path, (
            "import sys\n"
            "argv = sys.argv[1:]\n"
            "if '--fingerprint' in argv:\n"
            "    print('fp1')\n"
            "    sys.exit(0)\n"
            "if '--validate-design' in argv:\n"
            "    print('0 discrepancies total -- bridge matches the design')\n"
            "    sys.exit(0)\n"
            "if '--html' in argv:\n"
            "    raise SystemExit('must not re-render an existing report')\n"
            "sys.exit(0)\n"
        ))
        monkeypatch.setattr(hue_cli, "SCENE_LAYERS", stub)

        rc = hue_cli._cmd_start(
            Namespace(dir=str(workdir), accept=False, open=False))

        captured = capfd.readouterr()
        lines = [ln for ln in captured.out.splitlines() if ln.strip()]
        assert lines[-1] == "hue-kit-verdict: clean"
        assert rc == 0
        assert (workdir / "index.html").read_text() == "EXISTING REPORT\n"


class TestRenderFailed:
    """Fingerprint and design both match (clean-equivalent), but the report
    file is missing and re-rendering it fails: the verdict must be
    `render-failed`, distinct from `clean`, and must carry scene-layers'
    nonzero exit code."""

    def test_missing_report_that_fails_to_rerender_is_render_failed(
            self, hue_cli, tmp_path, monkeypatch, capfd):
        workdir = _established_workdir(tmp_path, fingerprint="fp1")
        assert not (workdir / "index.html").exists()
        stub = _write_stub(tmp_path, (
            "import sys\n"
            "argv = sys.argv[1:]\n"
            "if '--fingerprint' in argv:\n"
            "    print('fp1')\n"
            "    sys.exit(0)\n"
            "if '--validate-design' in argv:\n"
            "    print('0 discrepancies total -- bridge matches the design')\n"
            "    sys.exit(0)\n"
            "if '--html' in argv:\n"
            "    sys.exit(7)\n"
            "sys.exit(0)\n"
        ))
        monkeypatch.setattr(hue_cli, "SCENE_LAYERS", stub)

        rc = hue_cli._cmd_start(
            Namespace(dir=str(workdir), accept=False, open=False))

        captured = capfd.readouterr()
        lines = [ln for ln in captured.out.splitlines() if ln.strip()]
        assert lines[-1] == "hue-kit-verdict: render-failed"
        assert rc == 7
        assert not (workdir / "index.html").exists()


class TestHalfPresent:
    """Exactly one of the two working files exists.

    `start` writes without asking ONLY on a real first run. With one file
    already on disk the other cannot be rebuilt safely in either direction:
    regenerating the registry yields placeholder group names the existing
    design does not reference, and regenerating the design discards colour
    edits the user has not applied yet. So the half-present state is refused
    with a diagnostic naming the missing file, under its own verdict --
    `incomplete` -- and nothing is written.
    """

    _FINGERPRINT_ONLY = (
        "import sys, pathlib\n"
        "argv = sys.argv[1:]\n"
        "log = pathlib.Path(__file__).with_name('calls.log')\n"
        "log.write_text(log.read_text() + ' '.join(argv) + '\\n'\n"
        "               if log.is_file() else ' '.join(argv) + '\\n')\n"
        "if '--fingerprint' in argv:\n"
        "    print('fpX')\n"
        "    sys.exit(0)\n"
        "sys.exit(0)\n"
    )

    def _run(self, hue_cli, tmp_path, monkeypatch):
        stub = _write_stub(tmp_path, self._FINGERPRINT_ONLY)
        monkeypatch.setattr(hue_cli, "SCENE_LAYERS", stub)
        monkeypatch.setattr(hue_cli, "_open_report", lambda *a, **k: False)
        rc = hue_cli._cmd_start(
            Namespace(dir=str(tmp_path), accept=False, open=False))
        calls = (tmp_path / "calls.log")
        return rc, (calls.read_text() if calls.is_file() else "")

    def test_design_present_registry_missing_is_refused_without_export(
            self, hue_cli, tmp_path, monkeypatch, capfd):
        # The destructive direction: --export-designs has no exists guard
        # (scene-layers.py), so a first-run rebuild here would overwrite the
        # user's unapplied colour edits without asking.
        designs = tmp_path / "scene-designs.yaml"
        designs.write_text("scenes: [{name: Evening}]\n")

        rc, calls = self._run(hue_cli, tmp_path, monkeypatch)

        assert "--export-groups" not in calls
        assert "--export-designs" not in calls
        assert designs.read_text() == "scenes: [{name: Evening}]\n"
        assert not (tmp_path / "scene-groups.yaml").exists()
        assert not (tmp_path / "bridge-fingerprint.txt").exists()
        out = capfd.readouterr()
        assert "hue-kit-verdict: incomplete" in out.out
        assert "scene-groups.yaml" in (out.out + out.err)
        assert rc != 0

    def test_registry_present_design_missing_is_incomplete_not_setup_failed(
            self, hue_cli, tmp_path, monkeypatch, capfd):
        # The misleading direction: --export-groups DOES refuse an existing
        # path, so the old code reached it, failed, and reported
        # `setup-failed` with a --force hint whose remedy is destruction.
        groups = tmp_path / "scene-groups.yaml"
        groups.write_text("groups: [{name: ALL}]\n")

        rc, calls = self._run(hue_cli, tmp_path, monkeypatch)

        assert "--export-groups" not in calls
        assert "--export-designs" not in calls
        assert groups.read_text() == "groups: [{name: ALL}]\n"
        out = capfd.readouterr()
        assert "hue-kit-verdict: incomplete" in out.out
        assert "hue-kit-verdict: setup-failed" not in out.out
        assert "scene-designs.yaml" in (out.out + out.err)
        assert rc != 0
