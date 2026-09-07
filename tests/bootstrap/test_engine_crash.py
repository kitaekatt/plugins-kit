"""Tests for engine crash containment (B2).

The shell hook stamps the per-project cooldown BEFORE launching the engine
and (in background mode) only surfaces what the engine writes to
bootstrap_display.pending. An unhandled engine exception therefore used to
mean: no pending file (silent failure), traceback only in engine_output.log,
and a stamped cooldown throttling every retry for the rest of the window.
main() must contain the crash: write a crash .pending, clear the cooldown,
and exit 1 with the traceback on stderr.
"""

import hashlib
import json
import sys

import pytest

from bootstrap_lib import engine


def _boom():
    raise RuntimeError("boom xyz 12345")


def _seed_cooldown(data_dir, project_dir):
    """Plant a cooldown stamp the way session-bootstrap.sh does."""
    key = hashlib.sha1(str(project_dir).encode("utf-8")).hexdigest()
    cooldowns = data_dir / "cooldowns"
    cooldowns.mkdir(parents=True, exist_ok=True)
    stamp = cooldowns / f"last_run_epoch.{key}"
    stamp.write_text("123")
    return stamp


class TestEngineCrashContainment:
    def test_always_prologue_import_crash_logs_only(self, tmp_path, monkeypatch):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        stamp = _seed_cooldown(data_dir, project_dir)

        def _import_boom():
            raise ModuleNotFoundError(
                "No module named 'bootstrap_lib.records'",
                name="bootstrap_lib.records",
            )

        monkeypatch.setattr(engine, "_main", _import_boom)
        monkeypatch.setattr(sys, "argv", [
            "bootstrap_engine.py",
            "--plugin-root", str(tmp_path / "unused-root"),
            "--data-dir", str(data_dir),
            "--project-dir", str(project_dir),
            "--background",
            "--run-kind", "always",
        ])

        with pytest.raises(SystemExit) as exc:
            engine.main()
        assert exc.value.code == 1
        assert stamp.exists()
        assert not (data_dir / "bootstrap_display.pending").exists()
        assert "always" in (data_dir / "bootstrap.log").read_text()

    def test_crash_writes_pending_and_clears_cooldown(self, tmp_path, monkeypatch, capsys):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        stamp = _seed_cooldown(data_dir, project_dir)

        monkeypatch.setattr(engine, "_main", _boom)
        monkeypatch.setattr(sys, "argv", [
            "bootstrap_engine.py",
            "--plugin-root", str(tmp_path / "unused-root"),
            "--data-dir", str(data_dir),
            "--project-dir", str(project_dir),
            "--background",
        ])

        with pytest.raises(SystemExit) as exc:
            engine.main()
        assert exc.value.code == 1

        # The next prompt surfaces the failure via the pending file.
        pending = data_dir / "bootstrap_display.pending"
        assert pending.is_file(), "crash must write a crash .pending"
        response = json.loads(pending.read_text())
        assert response["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
        assert "boom xyz 12345" in response["hookSpecificOutput"]["additionalContext"]
        assert "crashed" in response["systemMessage"]

        # The optimistic cooldown stamp is rolled back so the next
        # SessionStart retries instead of silently throttling.
        assert not stamp.exists(), "crash must clear the cooldown stamp"

        # Traceback still lands on stderr (captured by engine_output.log).
        assert "boom xyz 12345" in capsys.readouterr().err

    def test_crash_without_project_dir_uses_global_stamp(self, tmp_path, monkeypatch):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        cooldowns = data_dir / "cooldowns"
        cooldowns.mkdir()
        stamp = cooldowns / "last_run_epoch._global_"
        stamp.write_text("123")

        monkeypatch.setattr(engine, "_main", _boom)
        monkeypatch.setattr(sys, "argv", [
            "bootstrap_engine.py",
            "--plugin-root", str(tmp_path / "unused-root"),
            "--data-dir", str(data_dir),
            "--background",
        ])

        with pytest.raises(SystemExit):
            engine.main()
        assert (data_dir / "bootstrap_display.pending").is_file()
        assert not stamp.exists()

    def test_console_crash_writes_no_files(self, tmp_path, monkeypatch, capsys):
        """--console is a human at a terminal: traceback on stderr, no file
        writes (console mode's contract), cooldown left alone (the shell
        never stamped one for a console run)."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        stamp = _seed_cooldown(data_dir, project_dir)

        monkeypatch.setattr(engine, "_main", _boom)
        monkeypatch.setattr(sys, "argv", [
            "bootstrap_engine.py",
            "--plugin-root", str(tmp_path / "unused-root"),
            "--data-dir", str(data_dir),
            "--project-dir", str(project_dir),
            "--console",
        ])

        with pytest.raises(SystemExit):
            engine.main()
        assert not (data_dir / "bootstrap_display.pending").exists()
        assert stamp.exists()
        assert "boom xyz 12345" in capsys.readouterr().err

    def test_system_exit_passes_through_uncontained(self, tmp_path, monkeypatch):
        """A deliberate sys.exit() from the engine is not a crash — no pending
        file, no cooldown rollback."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        def _clean_exit():
            sys.exit(0)

        monkeypatch.setattr(engine, "_main", _clean_exit)
        monkeypatch.setattr(sys, "argv", [
            "bootstrap_engine.py",
            "--plugin-root", str(tmp_path / "unused-root"),
            "--data-dir", str(data_dir),
            "--background",
        ])

        with pytest.raises(SystemExit) as exc:
            engine.main()
        assert exc.value.code == 0
        assert not (data_dir / "bootstrap_display.pending").exists()
