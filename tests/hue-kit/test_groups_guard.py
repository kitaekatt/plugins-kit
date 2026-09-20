"""Registry overwrite guard: `--export-groups` / `hue-kit groups` must refuse
to overwrite an existing scene-groups.yaml unless `--force` is given.

A regenerated registry carries placeholder names (G1, G2, ...) -- overwriting
one a user has hand-renamed silently discards every name they set. The guard
fires before the family is solved or anything is written, and never applies
to a first-run export (the file does not exist yet). No network, no bridge.
"""

from argparse import Namespace

import pytest


class TestSceneLayersExportGroupsGuard:
    """In-process `scene_layers.main()` -- the guard is checked before
    `export_groups()` runs and before the destination is touched."""

    def test_refuses_an_existing_destination_before_computing_anything(
            self, scene_layers, tmp_path, monkeypatch):
        cells = tmp_path / "cells.json"
        cells.write_text("{}")
        dest = tmp_path / "scene-groups.yaml"
        original = b"groups:\n  - name: Kitchen\n    zones: [Kitchen]\n"
        dest.write_bytes(original)

        def _boom(*a, **k):  # pragma: no cover - must never run
            raise AssertionError("export_groups must not run before the guard")

        monkeypatch.setattr(scene_layers, "export_groups", _boom)
        monkeypatch.setattr("sys.argv",
                            ["x", "--cells", str(cells),
                             "--export-groups", str(dest)])

        with pytest.raises(SystemExit) as excinfo:
            scene_layers.main()

        assert "--force" in str(excinfo.value)
        assert excinfo.value.code != 0
        assert dest.read_bytes() == original

    def test_force_overwrites(self, scene_layers, tmp_path, monkeypatch):
        cells = tmp_path / "cells.json"
        cells.write_text("{}")
        dest = tmp_path / "scene-groups.yaml"
        dest.write_bytes(b"groups:\n  - name: Kitchen\n    zones: [Kitchen]\n")

        monkeypatch.setattr(scene_layers, "export_groups",
                            lambda data: "groups: []\n")
        monkeypatch.setattr("sys.argv",
                            ["x", "--cells", str(cells),
                             "--export-groups", str(dest), "--force"])

        rc = scene_layers.main()

        assert rc == 0
        assert dest.read_text() == "groups: []\n"

    def test_absent_destination_needs_no_force(
            self, scene_layers, tmp_path, monkeypatch):
        cells = tmp_path / "cells.json"
        cells.write_text("{}")
        dest = tmp_path / "scene-groups.yaml"
        assert not dest.exists()

        monkeypatch.setattr(scene_layers, "export_groups",
                            lambda data: "groups: []\n")
        monkeypatch.setattr("sys.argv",
                            ["x", "--cells", str(cells),
                             "--export-groups", str(dest)])

        rc = scene_layers.main()

        assert rc == 0
        assert dest.read_text() == "groups: []\n"

    def test_stdout_destination_is_never_guarded(
            self, scene_layers, tmp_path, monkeypatch, capsys):
        """`--export-groups` with no PATH (stdout, `const="-"`) has no file to
        protect -- the guard must not misfire on it."""
        cells = tmp_path / "cells.json"
        cells.write_text("{}")

        monkeypatch.setattr(scene_layers, "export_groups",
                            lambda data: "groups: []\n")
        monkeypatch.setattr("sys.argv",
                            ["x", "--cells", str(cells), "--export-groups"])

        rc = scene_layers.main()

        assert rc == 0
        assert capsys.readouterr().out == "groups: []\n"


class TestCliGroupsGuard:
    """`hue-kit groups` -- the CLI itself must refuse before ever invoking
    the recorder in place of `_run_scene_layers`; POSIX's `_run_scene_layers`
    execve()s and never returns, so a refusal has to happen before that call."""

    def test_refuses_before_the_recorder_is_called(
            self, hue_cli, tmp_path, monkeypatch, capsys):
        registry = tmp_path / "scene-groups.yaml"
        original = b"groups:\n  - name: Kitchen\n    zones: [Kitchen]\n"
        registry.write_bytes(original)

        def _boom(*a, **k):  # pragma: no cover - must never run
            raise AssertionError("_run_scene_layers must not run before the guard")

        monkeypatch.setattr(hue_cli, "_run_scene_layers", _boom)

        rc = hue_cli.main(["--dir", str(tmp_path), "groups"])

        assert rc != 0
        assert "--force" in capsys.readouterr().err
        assert registry.read_bytes() == original

    def test_force_passes_the_flag_through(self, hue_cli, tmp_path, monkeypatch):
        registry = tmp_path / "scene-groups.yaml"
        registry.write_bytes(b"groups:\n  - name: Kitchen\n    zones: [Kitchen]\n")
        seen = {}

        def fake_run(flags, workdir):
            seen["flags"] = list(flags)
            return 0

        monkeypatch.setattr(hue_cli, "_run_scene_layers", fake_run)

        rc = hue_cli.main(["--dir", str(tmp_path), "groups", "--force"])

        assert rc == 0
        assert seen["flags"] == ["--export-groups", str(registry.resolve()),
                                  "--force"]

    def test_no_registry_yet_needs_no_force(self, hue_cli, tmp_path, monkeypatch):
        seen = {}

        def fake_run(flags, workdir):
            seen["flags"] = list(flags)
            return 0

        monkeypatch.setattr(hue_cli, "_run_scene_layers", fake_run)

        rc = hue_cli.main(["--dir", str(tmp_path), "groups"])

        assert rc == 0
        assert seen["flags"] == [
            "--export-groups",
            str((tmp_path / "scene-groups.yaml").resolve()),
        ]


class TestCmdStartFirstRunNeverNeedsForce:
    """`_cmd_start`'s first-run branch only reaches `--export-groups` when
    BOTH working files are absent: a half-present workdir is refused earlier
    under the `incomplete` verdict. So the registry is always absent by the
    time the call is made, it never has to pass `--force`, and the guard
    never fires there. (An earlier docstring justified this from the branch
    condition alone, which was the weaker claim -- that condition fires when
    EITHER file is missing, and the half-present case used to reach the
    export and die on the guard.)"""

    def test_first_run_export_groups_call_carries_no_force_flag(
            self, hue_cli, tmp_path, monkeypatch):
        seen_flags = []

        def fake_call(flags, workdir_arg, *, capture=False):
            seen_flags.append(list(flags))
            if "--fingerprint" in flags:
                return 0, "fp1"
            return 0, None

        monkeypatch.setattr(hue_cli, "_call_scene_layers", fake_call)
        monkeypatch.setattr(hue_cli, "_open_report", lambda *a, **k: False)

        hue_cli._cmd_start(Namespace(dir=str(tmp_path), accept=False, open=False))

        groups_calls = [f for f in seen_flags if "--export-groups" in f]
        assert len(groups_calls) == 1
        assert "--force" not in groups_calls[0]
