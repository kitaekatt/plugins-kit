"""`_write_text_atomic`, and that every user-visible writer in both scripts
goes through it instead of a direct `write_text`/`open(..., "w")`.

A destination is never left truncated or partial: the helper writes a
`<path>.tmp` beside the destination, flushes and fsyncs it, then
`os.replace`s it over the destination in one filesystem operation. On any
failure the temp file is removed and the destination's prior bytes are
untouched. No network -- every fixture below is a fake bridge/subprocess or
a bare tmp_path file, matching the rest of this package.
"""

import json
import os
import stat
from argparse import Namespace
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# The helper itself, exercised against both modules' copies.
# ---------------------------------------------------------------------------

@pytest.fixture(params=["hue_cli", "scene_layers"])
def mod(request, hue_cli, scene_layers):
    return {"hue_cli": hue_cli, "scene_layers": scene_layers}[request.param]


class TestWriteTextAtomicFailure:
    """os.replace is monkeypatched to raise once the temp file exists."""

    def test_replace_failure_leaves_an_existing_destination_untouched(
            self, mod, tmp_path, monkeypatch):
        dest = tmp_path / "target.txt"
        dest.write_text("original\n")
        tmp_marker = dest.with_name(dest.name + ".tmp")

        def _boom(src, dst):
            assert Path(src) == tmp_marker, "must replace the temp file, not the dest"
            assert tmp_marker.exists(), "the temp file must exist before replace runs"
            raise OSError("simulated replace failure")

        monkeypatch.setattr(mod.os, "replace", _boom)

        with pytest.raises(OSError):
            mod._write_text_atomic(dest, "new-bytes\n")

        assert dest.read_text() == "original\n", "prior bytes must survive"
        assert not tmp_marker.exists(), "no stray temp file after a failure"

    def test_replace_failure_leaves_no_destination_when_none_existed(
            self, mod, tmp_path, monkeypatch):
        dest = tmp_path / "target.txt"
        tmp_marker = dest.with_name(dest.name + ".tmp")
        assert not dest.exists()

        monkeypatch.setattr(mod.os, "replace",
                            lambda src, dst: (_ for _ in ()).throw(OSError("boom")))

        with pytest.raises(OSError):
            mod._write_text_atomic(dest, "new-bytes\n")

        assert not dest.exists()
        assert not tmp_marker.exists()


class TestWriteTextAtomicSuccess:
    def test_overwrites_and_leaves_no_temp_file(self, mod, tmp_path):
        dest = tmp_path / "target.txt"
        dest.write_text("old\n")
        tmp_marker = dest.with_name(dest.name + ".tmp")

        mod._write_text_atomic(dest, "new\n")

        assert dest.read_text() == "new\n"
        assert not tmp_marker.exists()

    def test_creates_a_fresh_destination(self, mod, tmp_path):
        dest = tmp_path / "fresh.txt"

        mod._write_text_atomic(dest, "hello\n")

        assert dest.read_text() == "hello\n"

    def test_a_stale_leftover_temp_file_does_not_survive(self, mod, tmp_path):
        dest = tmp_path / "target.txt"
        dest.write_text("old\n")
        tmp_marker = dest.with_name(dest.name + ".tmp")
        tmp_marker.write_text("stale-crash-leftover\n")

        mod._write_text_atomic(dest, "new\n")

        assert dest.read_text() == "new\n"
        assert not tmp_marker.exists()


class TestWriteTextAtomicMode:
    """The key-file test: mode governs the temp file AT CREATION, checked
    with a patched os.replace that stats the source before delegating to
    the real os.replace."""

    def test_mode_is_set_when_the_temp_file_is_created(
            self, mod, tmp_path, monkeypatch):
        dest = tmp_path / "app-key.txt"
        seen = {}
        real_replace = os.replace

        def _spy(src, dst):
            seen["mode"] = stat.S_IMODE(os.stat(src).st_mode)
            return real_replace(src, dst)

        monkeypatch.setattr(mod.os, "replace", _spy)

        mod._write_text_atomic(dest, "secret-key\n", mode=0o600)

        assert seen["mode"] == 0o600
        assert dest.read_text() == "secret-key\n"

    def test_a_wider_preexisting_temp_file_is_replaced_not_reused(
            self, mod, tmp_path):
        dest = tmp_path / "app-key.txt"
        tmp_marker = dest.with_name(dest.name + ".tmp")
        tmp_marker.write_text("stale\n")
        os.chmod(tmp_marker, 0o644)

        mod._write_text_atomic(dest, "secret-key\n", mode=0o600)

        assert dest.read_text() == "secret-key\n"
        assert stat.S_IMODE(os.stat(dest).st_mode) == 0o600


# ---------------------------------------------------------------------------
# Behavioural wiring: each of the 12 owned call sites is exercised through
# its normal entry point with os.replace monkeypatched to raise AFTER the
# temp file exists. This tests the writer's EFFECT, not that it calls a
# particular helper -- a test spying on `_write_text_atomic` would pin the
# implementation rather than the property (it would fail with AttributeError
# against the pre-fix source, which proves nothing about whether the old
# code truncated). None of the tests below reference `_write_text_atomic`.
#
# Where a destination cannot legitimately hold "prior content" in the
# branch under test (a first-baseline write, or a backup file whose path is
# freshly minted by `_unique_backup`), "prior state" is absence -- the
# assertion is that the destination stays absent and no temp file is left,
# which is the same untouched-destination property applied to an empty
# prior state.
# ---------------------------------------------------------------------------

def _write_stub(tmp_path, body, name="stub_scene_layers.py"):
    stub = tmp_path / name
    stub.write_text(body)
    return stub


def _assert_no_tmp_files(directory):
    stray = list(Path(directory).glob("*.tmp"))
    assert stray == [], f"stray temp file(s) left behind: {stray}"


class TestHueKitCliWiring:
    def test_cache_bridge_ip_failure_leaves_the_cache_untouched(
            self, hue_cli, tmp_path, monkeypatch):
        dest = tmp_path / "bridge-ip.txt"
        dest.write_text("sentinel-ip\n")
        monkeypatch.setattr(hue_cli, "BRIDGE_IP_CACHE", dest)
        monkeypatch.setattr(hue_cli.os, "replace",
                            lambda src, dst: (_ for _ in ()).throw(OSError("boom")))

        hue_cli._cache_bridge_ip("10.0.0.5")  # non-fatal: caught internally

        assert dest.read_text() == "sentinel-ip\n"
        _assert_no_tmp_files(tmp_path)

    def test_paired_key_write_failure_leaves_the_key_file_untouched(
            self, hue_cli, tmp_path, monkeypatch):
        dest = tmp_path / "app-key.txt"
        dest.write_text("sentinel-key\n")
        os.chmod(dest, 0o600)
        monkeypatch.setattr(hue_cli, "PAIRED_KEY_FILE", dest)
        monkeypatch.setattr(hue_cli.os, "replace",
                            lambda src, dst: (_ for _ in ()).throw(OSError("boom")))

        class _Resp:
            def json(self):
                return [{"success": {"username": "minted-key"}}]

        import sys
        monkeypatch.setattr(sys.modules["requests"], "post",
                            lambda *a, **k: _Resp(), raising=False)

        # --force: a key file already exists (our sentinel), and the pairing
        # flow otherwise refuses to mint a second key over it.
        with pytest.raises(SystemExit) as excinfo:
            hue_cli._cmd_pair(Namespace(no_wait=True, force=True))

        # The bridge minted a key that this run then failed to save. Reporting
        # success would lose it silently, so the failure is fatal and names
        # the path the user has to make writable.
        message = str(excinfo.value)
        assert str(dest) in message
        assert "paired." not in message
        assert dest.read_text() == "sentinel-key\n"
        _assert_no_tmp_files(tmp_path)

    def test_paired_key_perms_failure_is_not_a_write_failure(
            self, hue_cli, tmp_path, monkeypatch, capsys):
        # A chmod that will not take is cosmetic: the key IS saved, so the
        # run succeeds and warns, and the two concerns never share a message.
        dest = tmp_path / "app-key.txt"
        monkeypatch.setattr(hue_cli, "PAIRED_KEY_FILE", dest)
        monkeypatch.setattr(hue_cli.Path, "chmod",
                            lambda self, mode: (_ for _ in ()).throw(OSError("boom")))

        class _Resp:
            def json(self):
                return [{"success": {"username": "minted-key"}}]

        import sys
        monkeypatch.setattr(sys.modules["requests"], "post",
                            lambda *a, **k: _Resp(), raising=False)

        rc = hue_cli._cmd_pair(Namespace(no_wait=True, force=True))

        assert rc == 0
        assert dest.read_text() == "minted-key\n"
        err = capsys.readouterr().err
        assert "could not set 0600 perms" in err
        assert "paired." in err
        _assert_no_tmp_files(tmp_path)

    def _established_workdir(self, tmp_path, fingerprint="sentinel-fp"):
        (tmp_path / "scene-groups.yaml").write_text("groups: []\n")
        (tmp_path / "scene-designs.yaml").write_text("scenes: []\n")
        (tmp_path / "bridge-fingerprint.txt").write_text(fingerprint + "\n")
        return tmp_path

    def test_start_accept_failure_leaves_the_fingerprint_file_untouched(
            self, hue_cli, tmp_path, monkeypatch):
        workdir = self._established_workdir(tmp_path)
        stub = _write_stub(tmp_path, "print('new-fp')\n")
        monkeypatch.setattr(hue_cli, "SCENE_LAYERS", stub)
        monkeypatch.setattr(hue_cli.os, "replace",
                            lambda src, dst: (_ for _ in ()).throw(OSError("boom")))

        with pytest.raises(OSError):
            hue_cli._cmd_start(Namespace(dir=str(workdir), accept=True, open=False))

        assert (workdir / "bridge-fingerprint.txt").read_text() == "sentinel-fp\n"
        _assert_no_tmp_files(workdir)

    def test_start_first_run_failure_leaves_no_fingerprint_file(
            self, hue_cli, tmp_path, monkeypatch):
        # First run: no scene-groups.yaml/scene-designs.yaml yet, so no
        # bridge-fingerprint.txt exists either -- "prior state" is absence.
        stub = _write_stub(tmp_path, (
            "import sys\n"
            "argv = sys.argv[1:]\n"
            "if '--fingerprint' in argv:\n"
            "    print('fp1')\n"
            "sys.exit(0)\n"
        ))
        monkeypatch.setattr(hue_cli, "SCENE_LAYERS", stub)
        monkeypatch.setattr(hue_cli, "_open_report", lambda *a, **k: False)
        monkeypatch.setattr(hue_cli.os, "replace",
                            lambda src, dst: (_ for _ in ()).throw(OSError("boom")))
        dest = tmp_path / "bridge-fingerprint.txt"

        with pytest.raises(OSError):
            hue_cli._cmd_start(Namespace(dir=str(tmp_path), accept=False, open=False))

        assert not dest.exists()
        _assert_no_tmp_files(tmp_path)

    def test_start_establishes_baseline_failure_leaves_no_fingerprint_file(
            self, hue_cli, tmp_path, monkeypatch):
        # Established workdir, but bridge-fingerprint.txt predates
        # fingerprinting (absent) -- this is the "establish baseline"
        # branch, distinct from first-run.
        (tmp_path / "scene-groups.yaml").write_text("groups: []\n")
        (tmp_path / "scene-designs.yaml").write_text("scenes: []\n")
        stub = _write_stub(tmp_path, (
            "import sys\n"
            "argv = sys.argv[1:]\n"
            "if '--fingerprint' in argv:\n"
            "    print('fp1')\n"
            "    sys.exit(0)\n"
            "sys.exit(0)\n"
        ))
        monkeypatch.setattr(hue_cli, "SCENE_LAYERS", stub)
        monkeypatch.setattr(hue_cli.os, "replace",
                            lambda src, dst: (_ for _ in ()).throw(OSError("boom")))
        dest = tmp_path / "bridge-fingerprint.txt"

        with pytest.raises(OSError):
            hue_cli._cmd_start(Namespace(dir=str(tmp_path), accept=False, open=False))

        assert not dest.exists()
        _assert_no_tmp_files(tmp_path)

    def test_export_rebaseline_failure_leaves_the_fingerprint_file_untouched(
            self, hue_cli, tmp_path, monkeypatch):
        dest = tmp_path / "bridge-fingerprint.txt"
        dest.write_text("sentinel-fp\n")
        stub = _write_stub(tmp_path, (
            "import sys\n"
            "argv = sys.argv[1:]\n"
            "if '--fingerprint' in argv:\n"
            "    print('fp1')\n"
            "sys.exit(0)\n"
        ))
        monkeypatch.setattr(hue_cli, "SCENE_LAYERS", stub)
        monkeypatch.setattr(hue_cli.os, "replace",
                            lambda src, dst: (_ for _ in ()).throw(OSError("boom")))

        with pytest.raises(OSError):
            hue_cli.main(["--dir", str(tmp_path), "export"])

        assert dest.read_text() == "sentinel-fp\n"
        _assert_no_tmp_files(tmp_path)


class TestSceneLayersWiring:
    def test_export_groups_failure_leaves_the_registry_untouched(
            self, scene_layers, tmp_path, monkeypatch):
        cells = tmp_path / "cells.json"
        cells.write_text("{}")
        dest = tmp_path / "scene-groups.yaml"
        dest.write_text("sentinel-groups\n")
        monkeypatch.setattr(scene_layers, "export_groups", lambda data: "groups: []\n")
        monkeypatch.setattr(scene_layers.os, "replace",
                            lambda src, dst: (_ for _ in ()).throw(OSError("boom")))
        # --force: the registry already exists (our sentinel), and the CLI's
        # own overwrite guard would otherwise refuse before writing at all.
        monkeypatch.setattr("sys.argv",
                            ["x", "--cells", str(cells), "--export-groups", str(dest),
                             "--force"])

        with pytest.raises(OSError):
            scene_layers.main()

        assert dest.read_text() == "sentinel-groups\n"
        _assert_no_tmp_files(tmp_path)

    def test_export_designs_failure_leaves_the_design_untouched(
            self, scene_layers, tmp_path, monkeypatch):
        cells = tmp_path / "cells.json"
        cells.write_text("{}")
        dest = tmp_path / "scene-designs.yaml"
        dest.write_text("sentinel-designs\n")
        monkeypatch.setattr(scene_layers, "export_designs",
                            lambda data: ("scenes: []\n", 0))
        monkeypatch.setattr(scene_layers.os, "replace",
                            lambda src, dst: (_ for _ in ()).throw(OSError("boom")))
        monkeypatch.setattr("sys.argv",
                            ["x", "--cells", str(cells), "--export-designs", str(dest)])

        with pytest.raises(OSError):
            scene_layers.main()

        assert dest.read_text() == "sentinel-designs\n"
        _assert_no_tmp_files(tmp_path)

    def test_export_cells_failure_leaves_the_cells_file_untouched(
            self, scene_layers, tmp_path, monkeypatch):
        cells = tmp_path / "cells.json"
        cells.write_text(json.dumps({"scenes": [], "universe": []}))
        dest = tmp_path / "cells-out.json"
        dest.write_text("sentinel-cells\n")
        monkeypatch.setattr(scene_layers.os, "replace",
                            lambda src, dst: (_ for _ in ()).throw(OSError("boom")))
        monkeypatch.setattr("sys.argv",
                            ["x", "--cells", str(cells), "--export-cells", str(dest)])

        with pytest.raises(OSError):
            scene_layers.main()

        assert dest.read_text() == "sentinel-cells\n"
        _assert_no_tmp_files(tmp_path)

    def test_html_failure_leaves_the_report_untouched(
            self, scene_layers, tmp_path, monkeypatch):
        dest = tmp_path / "index.html"
        dest.write_text("sentinel-html\n")
        monkeypatch.setattr(scene_layers, "bridge_session", lambda: object())
        monkeypatch.setattr(scene_layers, "layered_view", lambda session: ([], [], {}))
        monkeypatch.setattr(scene_layers.smg, "layered_report",
                            lambda *a, **k: "<html></html>")
        monkeypatch.setattr(scene_layers.os, "replace",
                            lambda src, dst: (_ for _ in ()).throw(OSError("boom")))
        monkeypatch.setattr("sys.argv", ["x", "--html", str(dest)])

        with pytest.raises(OSError):
            scene_layers.main()

        assert dest.read_text() == "sentinel-html\n"
        _assert_no_tmp_files(tmp_path)

    def test_json_out_failure_leaves_the_output_file_untouched(
            self, scene_layers, tmp_path, monkeypatch):
        cells = tmp_path / "cells.json"
        cells.write_text("{}")
        out = tmp_path / "result.json"
        out.write_text("sentinel-json\n")
        monkeypatch.setattr(scene_layers, "build_model", lambda data: (None, None, None))
        monkeypatch.setattr(scene_layers, "json_result",
                            lambda U, zones, scenes: {"ok": True})
        monkeypatch.setattr(scene_layers.os, "replace",
                            lambda src, dst: (_ for _ in ()).throw(OSError("boom")))
        monkeypatch.setattr("sys.argv",
                            ["x", "--cells", str(cells), "--json", "--out", str(out)])

        with pytest.raises(OSError):
            scene_layers.main()

        assert out.read_text() == "sentinel-json\n"
        _assert_no_tmp_files(tmp_path)

    def test_apply_backup_failure_leaves_no_backup_file(
            self, scene_layers, tmp_path, monkeypatch):
        backup_dir = tmp_path / "backups"
        monkeypatch.setattr(scene_layers, "BACKUP_DIR", backup_dir)
        registry_path = tmp_path / "scene-groups.yaml"
        registry_path.write_text("groups:\n  - name: G\n    zones: [Room]\n")
        import functools
        monkeypatch.setattr(
            scene_layers, "load_group_registry",
            functools.partial(scene_layers.load_group_registry, path=registry_path))

        lights = [{"id": "L1", "metadata": {"name": "Lamp"}}]
        zones = [{"id": "Z1", "metadata": {"name": "Room"},
                  "children": [{"rid": "L1"}]}]
        live_scene = {
            "id": "S1", "metadata": {"name": "Relax"}, "group": {"rid": "Z1"},
            "actions": [{"target": {"rid": "L1", "rtype": "light"},
                        "action": {"on": {"on": True},
                                   "dimming": {"brightness": 50.0}}}],
        }

        def _fake_clip_get(session, resource):
            return {"light": lights, "zone": zones, "room": [],
                    "scene": [live_scene]}[resource]

        monkeypatch.setattr(scene_layers.smg, "clip_get", _fake_clip_get)
        # A fresh backup path is never pre-existing (`_unique_backup` bumps
        # past any collision) -- "prior state" is absence, computed before
        # the replace() patch so nothing here creates the file.
        backup_dir.mkdir(parents=True, exist_ok=True)
        expected_backup = scene_layers._unique_backup("Relax")
        monkeypatch.setattr(scene_layers.os, "replace",
                            lambda src, dst: (_ for _ in ()).throw(OSError("boom")))

        class _FakeResponse:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {"errors": []}

        class _FakeSession:
            def put(self, url, json, timeout, verify):
                return _FakeResponse()

        design = {"scenes": [
            {"name": "Relax", "layers": [{"group": "G", "xy": [0.3, 0.3], "bri": 60}]},
        ]}

        with pytest.raises(OSError):
            scene_layers.apply_design(_FakeSession(), design, None, assume_yes=True)

        assert not expected_backup.exists()
        _assert_no_tmp_files(backup_dir)
