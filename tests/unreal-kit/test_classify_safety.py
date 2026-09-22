"""Classifier tests for mapping identity and frozen mutation candidates."""

from __future__ import annotations

import json
import runpy
import sys
import types
from pathlib import Path


_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "plugins/unreal-kit/skills/fix-up-redirectors/scripts/classify_safety.py"
 )


def _run_classifier(monkeypatch, tmp_path: Path, *, mappings, opened, record):
    discovery = tmp_path / "discovery.yaml"
    discovery.write_text(
        "scope: /Game\nredirectors:\n" + "  - " + "\n    ".join(
            f"{key}: {value!r}" for key, value in record.items()
        ) + "\n",
        encoding="utf-8",
    )
    safe = tmp_path / "safe.json"
    orphaned = tmp_path / "orphaned.json"
    report = tmp_path / "report.json"

    fake_p4 = types.ModuleType("p4cli")
    fake_p4.get_workspace_mapping = lambda: ("//depot/project", str(tmp_path))
    fake_p4.get_opened_map = lambda: opened
    fake_p4.local_to_depot = lambda local, depot, root: depot + local[len(root):]
    fake_p4.where_records = lambda paths: [item for path in paths for item in mappings.get(path, [])]
    fake_bootstrap = types.ModuleType("bootstrap_guard")
    fake_bootstrap.reexec_under_plugin_venv = lambda _name: None
    fake_repair = types.ModuleType("path_repair")
    fake_repair.repair_path = lambda: None
    names = ("p4cli", "bootstrap_guard", "path_repair")
    saved = {name: sys.modules.get(name) for name in names}
    sys.modules.update({"p4cli": fake_p4, "bootstrap_guard": fake_bootstrap, "path_repair": fake_repair})
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(_SCRIPT), "--discovery", str(discovery), "--out-safe", str(safe),
            "--out-orphaned", str(orphaned), "--out-report", str(report),
        ],
    )
    try:
        runpy.run_path(str(_SCRIPT), run_name="__main__")
    finally:
        for name, old in saved.items():
            if old is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old
    return json.loads(safe.read_text(encoding="utf-8")), json.loads(report.read_text(encoding="utf-8"))


def test_remapped_opened_file_is_blocked_by_true_depot_identity(monkeypatch, tmp_path):
    local = str(tmp_path / "Plugin" / "Content" / "R.uasset")
    Path(local).parent.mkdir(parents=True)
    Path(local).write_bytes(b"redirector")
    record = {
        "pkg": "/Game/R", "file": local, "target_exists": True,
        "referencer_files": [], "referencer_pkgs": [],
    }
    mapped = [{"input": local, "depotFile": "//depot/plugin/Content/R.uasset"}]
    safe, report = _run_classifier(
        monkeypatch, tmp_path,
        mappings={local: mapped},
        opened={"//depot/plugin/content/r.uasset": [{"user": "bob", "change": "17"}]},
        record=record,
    )
    assert safe["redirectors"] == []
    assert report["counts"]["blocked"] == 1


def test_ambiguous_mapping_is_blocked_and_candidate_snapshot_is_emitted(monkeypatch, tmp_path):
    local = str(tmp_path / "R.uasset")
    Path(local).write_bytes(b"redirector")
    record = {
        "pkg": "/Game/R", "file": local, "target_exists": True,
        "referencer_files": [], "referencer_pkgs": [],
    }
    mappings = {
        local: [
            {"input": local, "depotFile": "//depot/a/R.uasset"},
            {"input": local, "depotFile": "//depot/b/R.uasset"},
        ],
    }
    safe, _report = _run_classifier(
        monkeypatch, tmp_path, mappings=mappings, opened={}, record=record,
    )
    assert safe["redirectors"] == []
