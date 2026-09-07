"""Transient first-party import-crash handling (partial plugin-download race).

A SessionStart bootstrap pass can import a first-party package whose submodule
has not been written yet because the plugin cache download is still in flight
(the reported case: `No module named 'bootstrap_lib.fix_queue'`). That crash
self-heals once the download completes, so the engine handles it SILENTLY --
no user-facing message -- and the UserPromptSubmit harvest relaunches the pass
off a pending marker until a completed pass clears it.
"""

import json
import sys

from bootstrap_lib import harvest
from bootstrap_lib import engine
from bootstrap_lib.engine import _is_transient_import_crash, _defer_transient_retry
from bootstrap_lib.harvest import run_harvest
from bootstrap_lib.stamps import global_stamp


def _registry(tmp_path, installed, install_path="/cache/bootstrap/CUR"):
    p = tmp_path / "installed_plugins.json"
    p.write_text(json.dumps({"plugins": {
        "bootstrap@plugins-kit": [{"version": installed, "installPath": install_path}],
    }}))
    return str(p)


class TestTransientClassifier:
    def test_first_party_submodule_is_transient(self):
        exc = ModuleNotFoundError(
            "No module named 'bootstrap_lib.fix_queue'", name="bootstrap_lib.fix_queue"
        )
        assert _is_transient_import_crash(exc)

    def test_first_party_top_level_is_transient(self):
        assert _is_transient_import_crash(ModuleNotFoundError("x", name="bootstrap_lib"))

    def test_other_shared_lib_is_transient(self):
        assert _is_transient_import_crash(
            ModuleNotFoundError("x", name="skills_kit_lib.audit")
        )

    def test_third_party_missing_dep_is_not_transient(self):
        # A genuinely missing third-party dependency is a real config gap, not a
        # download race -- it must keep the loud crash path.
        assert not _is_transient_import_crash(ModuleNotFoundError("x", name="yaml"))

    def test_non_import_error_is_not_transient(self):
        assert not _is_transient_import_crash(RuntimeError("boom"))

    def test_lookalike_prefix_is_not_transient(self):
        # "bootstrap_libextra" must not match "bootstrap_lib".
        assert not _is_transient_import_crash(
            ModuleNotFoundError("x", name="bootstrap_libextra")
        )


class TestDeferTransientRetry:
    def test_marks_pending_and_writes_no_message(self, tmp_path, monkeypatch):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        proj = tmp_path / "proj"
        proj.mkdir()
        exc = ModuleNotFoundError(
            "No module named 'bootstrap_lib.fix_queue'",
            name="bootstrap_lib.fix_queue",
        )
        _defer_transient_retry(
            "Traceback...\nModuleNotFoundError: bootstrap_lib.fix_queue",
            exc,
            (str(data_dir), str(proj), "", False, True, "full"),
        )
        assert global_stamp(str(data_dir), "import_retry_pending").read() == "1"
        # SILENT: no user-facing crash display is written.
        assert not (data_dir / "bootstrap_display.pending").exists()

    def test_console_mode_writes_nothing(self, tmp_path, monkeypatch):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        _defer_transient_retry(
            "tb",
            ModuleNotFoundError("x", name="bootstrap_lib.x"),
            (str(data_dir), "", "", True, False, "full"),
        )
        assert global_stamp(str(data_dir), "import_retry_pending").read() == ""

    def test_fourth_identical_import_crash_becomes_loud(self, tmp_path, monkeypatch):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        root = tmp_path / "root"
        (root / ".claude-plugin").mkdir(parents=True)
        (root / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"version": "1.0.0"})
        )
        exc = ModuleNotFoundError("missing", name="bootstrap_lib.records")
        lock_args = (str(data_dir), "", str(root), False, True, "full")
        for _ in range(3):
            assert _defer_transient_retry("tb", exc, lock_args)
        assert not (data_dir / "bootstrap_display.pending").exists()

        # Escalation is HANDLED by the retry helper (it emits the loud crash
        # itself), so the caller must not emit a second crash for the same
        # traceback: count the emits.
        emits = []
        monkeypatch.setattr(
            engine, "_emit_engine_crash",
            lambda tb, args, _real=engine._emit_engine_crash: (emits.append(tb), _real(tb, args)),
        )
        assert _defer_transient_retry("tb", exc, lock_args)
        assert len(emits) == 1
        pending = data_dir / "bootstrap_display.pending"
        assert pending.is_file()
        assert "bootstrap_lib.records" in pending.read_text()

    def test_import_retry_counter_resets_for_new_engine_version(self, tmp_path):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        root = tmp_path / "root"
        (root / ".claude-plugin").mkdir(parents=True)
        manifest = root / ".claude-plugin" / "plugin.json"
        manifest.write_text(json.dumps({"version": "1.0.0"}))
        exc = ModuleNotFoundError("missing", name="bootstrap_lib.records")
        lock_args = (str(data_dir), "", str(root), False, True, "full")
        for _ in range(3):
            _defer_transient_retry("tb", exc, lock_args)

        manifest.write_text(json.dumps({"version": "1.0.1"}))
        assert _defer_transient_retry("tb", exc, lock_args)
        state = json.loads((data_dir / "import_retry_state.json").read_text())
        assert state["version"] == "1.0.1"
        assert state["count"] == 1


class TestHarvestImportRetry:
    def _setup(self, tmp_path, monkeypatch, installed="0.44.0", ran="0.44.0"):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        if ran is not None:
            global_stamp(str(data_dir), "engine_ran_version").write(ran)
        reg = _registry(tmp_path, installed)
        calls = []
        monkeypatch.setattr(
            harvest, "launch_new_engine",
            lambda ip, pd, dd: calls.append((ip, pd, dd)) or True,
        )
        return str(data_dir), reg, calls

    def test_pending_marker_relaunches_at_equal_version(self, tmp_path, monkeypatch):
        # The crashing and installed versions are equal, so the version-harvest
        # gate would NOT fire -- the pending marker is what drives the retry.
        data_dir, reg, calls = self._setup(tmp_path, monkeypatch)
        global_stamp(data_dir, "import_retry_pending").write("1")
        status = run_harvest(data_dir, "/proj", reg, "plugins-kit")
        assert len(calls) == 1
        assert calls[0][0] == "/cache/bootstrap/CUR"
        assert status is not None and "import-retry" in status
        assert global_stamp(data_dir, "import_retry_launched").read() == "1"

    def test_in_flight_guard_blocks_double_spawn(self, tmp_path, monkeypatch):
        data_dir, reg, calls = self._setup(tmp_path, monkeypatch)
        global_stamp(data_dir, "import_retry_pending").write("1")
        run_harvest(data_dir, "/proj", reg, "plugins-kit")
        run_harvest(data_dir, "/proj", reg, "plugins-kit")  # launched guard set
        assert len(calls) == 1

    def test_crash_voids_guard_so_next_prompt_retries(self, tmp_path, monkeypatch):
        # Simulate the relaunched pass crashing again: _defer_transient_retry
        # clears the launched guard, re-enabling exactly one more retry.
        data_dir, reg, calls = self._setup(tmp_path, monkeypatch)
        global_stamp(data_dir, "import_retry_pending").write("1")
        run_harvest(data_dir, "/proj", reg, "plugins-kit")
        global_stamp(data_dir, "import_retry_launched").clear()  # what a crash does
        run_harvest(data_dir, "/proj", reg, "plugins-kit")
        assert len(calls) == 2

    def test_no_marker_leaves_version_harvest_untouched(self, tmp_path, monkeypatch):
        # Equal versions, no pending marker -> no launch (regression guard: the
        # retry path must not fire on the ordinary no-update prompt).
        data_dir, reg, calls = self._setup(tmp_path, monkeypatch)
        assert run_harvest(data_dir, "/proj", reg, "plugins-kit") is None
        assert calls == []
