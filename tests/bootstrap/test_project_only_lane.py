"""The project-only run kind (`bootstrap run --project-only`).

It applies only the project manifest layers and the project venv, skipping
marketplace refresh, plugin updates, plugin manifests and every lifecycle
stamp. These tests pin WHICH phases run, by replacing each phase function with
a recorder, rather than by reading output strings.
"""

import json
import sys
from types import SimpleNamespace

from bootstrap_lib import engine

#: Phases a project-only pass must run, in this order.
PROJECT_PHASES = [
    "_pass_prelude",
    "_pass_open_record",
    "_pass_identity",
    "_pass_load_layered",
    "_pass_project_scope_manifest",
    "_pass_project_interpreter_env",
    "_pass_layered_manifest",
    "_pass_project_venv",
    "_pass_project_npm_and_links",
    "_pass_project_shared_libs",
    "_pass_display_lines",
    "_pass_console_report",
]

#: Full-pass phases a project-only pass must never run: marketplace refresh,
#: plugin updates and manifests, machine-wide state, and lifecycle stamps.
SKIPPED_PHASES = {
    "_pass_version_change", "_pass_self_setup", "_pass_registry_repair",
    "_pass_snapshot_installed", "_pass_tiers_and_uninstalls",
    "_pass_discovery_inputs", "_pass_marketplace_barrier",
    "_pass_interpreter_env", "_pass_env_json", "_pass_plugins",
    "_pass_self_register", "_pass_env_records", "_pass_shared_libs",
    "_pass_notices", "_pass_codex_hook", "_pass_write_logs",
    "_pass_elevation", "_pass_emit", "_pass_complete", "_pass_always_lane",
}


def _run_with_recorded_phases(monkeypatch, tmp_path, *flags):
    calls = []
    # Every phase function, except the project lane driver under test.
    names = sorted(n for n in dir(engine) if n.startswith("_pass_")
                   and n != "_pass_project_lane")

    def recorder(name):
        def stub(*args, **kwargs):
            calls.append(name)
            if name == "_pass_prelude":
                return SimpleNamespace(
                    args=args[0], data_dir=str(tmp_path), bootstrap_label="b",
                    display_sections=[], boot_action=[], boot_ok=[],
                    all_failures=[], recorder=engine._NullRecorder(),
                    project_pull_notices=[])
            if name == "_pass_display_lines":
                return []
            return None
        return stub

    for name in names:
        monkeypatch.setattr(engine, name, recorder(name))
    monkeypatch.setattr(engine, "_activate_bootstrap_venv", lambda d: None)
    monkeypatch.setattr(sys, "argv", [
        "bootstrap_engine.py", "--plugin-root", str(tmp_path),
        "--data-dir", str(tmp_path), "--project-dir", str(tmp_path), *flags])
    engine._main_pass()
    return calls


def test_project_kind_runs_only_the_project_phases(monkeypatch, tmp_path):
    calls = _run_with_recorded_phases(
        monkeypatch, tmp_path, "--console", "--run-kind", "project")
    assert calls == PROJECT_PHASES
    assert not SKIPPED_PHASES & set(calls)


def test_full_kind_still_runs_the_skipped_phases(monkeypatch, tmp_path):
    """Control for the recorder harness: the same stubs, on a full pass,
    record the phases the project kind must skip."""
    calls = _run_with_recorded_phases(monkeypatch, tmp_path, "--console")
    expected = SKIPPED_PHASES - {"_pass_always_lane", "_pass_complete",
                                 "_pass_emit"}
    assert expected <= set(calls)


def test_project_layers_exclude_every_user_layer(monkeypatch, tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    data_dir = tmp_path / "data"
    for d in (home / ".claude", project / ".claude", data_dir):
        d.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    (data_dir / "user-bootstrap.json").write_text(json.dumps({"tools": [{"name": "legacy"}]}))
    (home / ".claude" / "bootstrap.json").write_text(json.dumps({"tools": [{"name": "user"}]}))
    (home / ".claude" / "bootstrap.local.json").write_text(
        json.dumps({"tools": [{"name": "user_local"}]}))
    (project / ".claude" / "bootstrap.json").write_text(
        json.dumps({"tools": [{"name": "project"}]}))
    (project / ".claude" / "bootstrap.local.json").write_text(
        json.dumps({"tools": [{"name": "project_local"}]}))

    full, _errors, _state = engine._load_layered_manifests_ex(str(project), str(data_dir))
    scoped, _errors, _state = engine._load_layered_manifests_ex(
        str(project), str(data_dir), kinds=engine._PROJECT_LAYER_KINDS)

    assert {t["name"] for t in full["tools"]} == {
        "legacy", "user", "user_local", "project", "project_local"}
    assert {t["name"] for t in scoped["tools"]} == {"project", "project_local"}


def test_project_scope_drops_marketplaces_and_plugins():
    st = SimpleNamespace(
        layered_manifest={"marketplaces": [{"name": "m"}], "plugins": [{"ref": "m:p"}],
                          "tools": [{"name": "t"}], "project_venv": {}},
        boot_ok=[])
    engine._pass_project_scope_manifest(st)
    assert set(st.layered_manifest) == {"tools", "project_venv"}
    assert any("plugins" in str(e) for e in st.boot_ok)
    assert any("marketplaces" in str(e) for e in st.boot_ok)


def test_project_kind_records_itself_in_the_pass_record(monkeypatch, tmp_path):
    seen = []

    class _Recorder:
        def record(self, kind, text="", sev=None, **kw):
            seen.append((kind, text, kw.get("detail")))

        def record_entry(self, *a, **kw):
            pass

    monkeypatch.setattr(engine, "_new_recorder", lambda *a, **k: _Recorder())
    st = SimpleNamespace(
        args=SimpleNamespace(project_dir=str(tmp_path), verbose=True,
                             run_kind="project", fix_all=False),
        config={}, data_dir=str(tmp_path), start_time=None, current_os="macos",
        plugin_root=str(tmp_path), engine_python="/py",
        project_python_entry=None,
        path_repair_result=SimpleNamespace(changed=False))
    engine._pass_open_record(st)
    started = [d for kind, text, d in seen if text == "engine pass started"]
    assert started and started[0]["run_kind"] == "project"


def test_a_full_pass_record_detail_is_unchanged(monkeypatch, tmp_path):
    seen = []

    class _Recorder:
        def record(self, kind_, text="", sev=None, **kw):
            seen.append((text, kw.get("detail")))

        def record_entry(self, *a, **kw):
            pass

    monkeypatch.setattr(engine, "_new_recorder", lambda *a, **k: _Recorder())
    st = SimpleNamespace(
        args=SimpleNamespace(project_dir=str(tmp_path), verbose=True,
                             run_kind="full", fix_all=False),
        config={}, data_dir=str(tmp_path), start_time=None, current_os="macos",
        plugin_root=str(tmp_path), engine_python="/py",
        project_python_entry=None,
        path_repair_result=SimpleNamespace(changed=False))
    engine._pass_open_record(st)
    started = [d for text, d in seen if text == "engine pass started"]
    assert "run_kind" not in started[0]
