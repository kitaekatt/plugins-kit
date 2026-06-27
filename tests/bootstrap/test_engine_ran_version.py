"""Tests that the engine stamps engine_ran_version on a completed pass.

engine_ran_version is the single-session update protocol's loop guard: the
UserPromptSubmit harvest only launches a newer engine when installed > this
stamp, and the harvested engine writes its OWN version here on completion.

Driven in-process with an EMPTY self_setup (no tools / path_entries / venv) and
an isolated HOME so the pass has no external side effects (no PATH registry
writes, no uv sync) — just enough to reach the end of engine._main and stamp.
"""

import json

from bootstrap_lib import engine
from bootstrap_lib.stamps import global_stamp


def _fake_root(tmp_path, version):
    root = tmp_path / "plugin_root"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "bootstrap", "version": version}), encoding="utf-8"
    )
    defaults = root / "defaults"
    defaults.mkdir()
    (defaults / "config.json").write_text(json.dumps({
        "schema_version": 5,
        "no_bootstrap": [],
        "bootstrap_cache": [],
        "log_success_shell": False,
        "log_success_checks": False,
        "self_setup": {},          # empty -> no tools/path/venv side effects
        "notify_reload_needed": False,
    }), encoding="utf-8")
    (root / "bootstrap.json").write_text(json.dumps({}), encoding="utf-8")
    return str(root)


def _run_main(tmp_path, monkeypatch, version):
    root = _fake_root(tmp_path, version)
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    iso_home = tmp_path / "home"
    iso_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(iso_home))
    monkeypatch.setenv("USERPROFILE", str(iso_home))
    monkeypatch.setattr("sys.argv", [
        "bootstrap_engine.py",
        "--plugin-root", root,
        "--data-dir", str(data_dir),
        "--background",  # write display to a file, not stdout
    ])
    engine._main()
    return str(data_dir)


class TestEngineRanVersionStamp:
    def test_stamps_running_version_on_completion(self, tmp_path, monkeypatch):
        data_dir = _run_main(tmp_path, monkeypatch, "0.21.0")
        assert global_stamp(data_dir, "engine_ran_version").read() == "0.21.0"

    def test_stamp_updates_to_new_version_on_next_pass(self, tmp_path, monkeypatch):
        data_dir = _run_main(tmp_path, monkeypatch, "0.21.0")
        assert global_stamp(data_dir, "engine_ran_version").read() == "0.21.0"
        # A newer engine running a pass overwrites it (what a harvested run does).
        data_dir2 = _run_main(tmp_path / "second", monkeypatch, "0.22.0")
        assert global_stamp(data_dir2, "engine_ran_version").read() == "0.22.0"
