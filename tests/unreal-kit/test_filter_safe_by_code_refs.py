"""Integration tests for the code-reference cache and Phase 3.5 filter."""

from __future__ import annotations

import importlib.util
import json
import runpy
import sys
import types
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_LIB = _ROOT / "plugins" / "unreal-kit" / "skills" / "fix-up-redirectors" / "lib"
_SCRIPTS = _ROOT / "plugins" / "unreal-kit" / "skills" / "fix-up-redirectors" / "scripts"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import code_refs


def _load_filter():
    spec = importlib.util.spec_from_file_location(
        "filter_safe_by_code_refs_test_module",
        _SCRIPTS / "filter_safe_by_code_refs.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


FILTER = _load_filter()


def _make_project(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "Project"
    (project / "Content").mkdir(parents=True)
    (project / "Content" / "R.uasset").write_bytes(b"redirector")
    (project / "Content" / "Other.uasset").write_bytes(b"other")
    (project / "Project.uproject").write_text("{}", encoding="utf-8")
    source = project / "Source"
    source.mkdir()
    return project, source


def _write_safe(path: Path, pkg: str = "/Game/R") -> None:
    path.write_text(
        json.dumps(
            {
                "scope": "/Game",
                "count": 1,
                "redirectors": [{"pkg": pkg}],
            }
        ),
        encoding="utf-8",
    )


def _run_filter(monkeypatch, *, root: Path, refs: Path, safe_in: Path, safe_out: Path,
                extensions: tuple[str, ...] = (".cpp",), max_age: float = 24.0):
    argv = [
        "filter_safe_by_code_refs.py",
        "--safe-in", str(safe_in),
        "--safe-out", str(safe_out),
        "--refs", str(refs),
        "--root", str(root),
        "--extensions", ",".join(extensions),
        "--max-age-hours", str(max_age),
    ]
    monkeypatch.setattr(sys, "argv", argv)
    return FILTER.main()


def test_utf16_reference_is_removed_by_real_scan_cache_filter(tmp_path, monkeypatch):
    root, source = _make_project(tmp_path)
    (source / "ref.ini").write_bytes(
        'K = "/Game/R"'.encode("utf-16-le")
    )
    refs = tmp_path / "refs.yaml"
    safe_in = tmp_path / "safe.json"
    safe_out = tmp_path / "safe_filtered.json"
    _write_safe(safe_in)

    _run_filter(monkeypatch, root=root, refs=refs, safe_in=safe_in,
                safe_out=safe_out, extensions=(".ini",))

    assert json.loads(safe_out.read_text())["redirectors"] == []


def test_coverage_failure_discloses_and_preserves_existing_safe_output(tmp_path, monkeypatch, capsys):
    root, source = _make_project(tmp_path)
    (source / "huge.cpp").write_bytes(
        b"x" * (code_refs._MAX_FILE_BYTES + 1) + b'"/Game/R"'
    )
    refs = tmp_path / "refs.yaml"
    safe_in = tmp_path / "safe.json"
    safe_out = tmp_path / "safe_filtered.json"
    _write_safe(safe_in)
    safe_out.write_text("old successful output", encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        _run_filter(monkeypatch, root=root, refs=refs, safe_in=safe_in,
                    safe_out=safe_out)

    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "coverage" in (captured.err + captured.out).lower()
    assert safe_out.read_text(encoding="utf-8") == "old successful output"


def test_fresh_cache_from_different_root_is_regenerated(tmp_path, monkeypatch):
    root_a, source_a = _make_project(tmp_path / "a")
    (source_a / "ref.cpp").write_text('K = "/Game/Other"', encoding="utf-8")
    refs = tmp_path / "refs.yaml"
    code_refs.save(
        str(refs), {"/Game/Other"}, str(root_a), 1, 1, (".cpp",),
        mounts=code_refs.discover_mount_points(str(root_a)),
    )

    root_b, source_b = _make_project(tmp_path / "b")
    (source_b / "ref.cpp").write_text('K = "/Game/R"', encoding="utf-8")
    safe_in = tmp_path / "safe.json"
    safe_out = tmp_path / "safe_filtered.json"
    _write_safe(safe_in)

    _run_filter(monkeypatch, root=root_b, refs=refs, safe_in=safe_in, safe_out=safe_out)

    assert json.loads(safe_out.read_text())["redirectors"] == []


def test_fresh_cache_with_changed_extensions_is_regenerated(tmp_path, monkeypatch):
    root, source = _make_project(tmp_path)
    (source / "ref.cpp").write_text('K = "/Game/Other"', encoding="utf-8")
    refs = tmp_path / "refs.yaml"
    mounts = code_refs.discover_mount_points(str(root))
    code_refs.save(str(refs), {"/Game/Other"}, str(root), 1, 1, (".cpp",), mounts=mounts)
    (source / "ref.ini").write_text('K = "/Game/R"', encoding="utf-8")
    safe_in = tmp_path / "safe.json"
    safe_out = tmp_path / "safe_filtered.json"
    _write_safe(safe_in)

    _run_filter(monkeypatch, root=root, refs=refs, safe_in=safe_in,
                safe_out=safe_out, extensions=(".ini",))

    assert json.loads(safe_out.read_text())["redirectors"] == []


def test_fresh_cache_with_changed_mount_content_root_is_regenerated(tmp_path, monkeypatch):
    root, source = _make_project(tmp_path)
    plugin = root / "Plugins" / "Shared"
    (plugin / "Content").mkdir(parents=True)
    (plugin / "Content" / "Other.uasset").write_bytes(b"other")
    (plugin / "Shared.uplugin").write_text("{}", encoding="utf-8")
    (source / "ref.cpp").write_text('K = "/Shared/Other"', encoding="utf-8")
    refs = tmp_path / "refs.yaml"
    mounts = code_refs.discover_mount_points(str(root))
    code_refs.save(str(refs), {"/Shared/Other"}, str(root), 1, 1, (".cpp",), mounts=mounts)

    # Keep the mount name while changing its on-disk content root.
    (plugin / "Content" / "Other.uasset").unlink()
    replacement = root / "Plugins" / "Moved" / "Content"
    replacement.mkdir(parents=True)
    (replacement / "Other.uasset").write_bytes(b"other")
    (root / "Plugins" / "Moved" / "Shared.uplugin").write_text("{}", encoding="utf-8")
    (source / "ref.cpp").write_text('K = "/Shared/Other"', encoding="utf-8")
    safe_in = tmp_path / "safe.json"
    safe_out = tmp_path / "safe_filtered.json"
    _write_safe(safe_in, pkg="/Shared/Other")

    _run_filter(monkeypatch, root=root, refs=refs, safe_in=safe_in, safe_out=safe_out)

    assert json.loads(safe_out.read_text())["redirectors"] == []


def test_cache_without_complete_provenance_is_regenerated(tmp_path, monkeypatch):
    root, source = _make_project(tmp_path)
    (source / "ref.cpp").write_text('K = "/Game/R"', encoding="utf-8")
    refs = tmp_path / "refs.yaml"
    mounts = code_refs.discover_mount_points(str(root))
    code_refs.save(str(refs), {"/Game/R"}, str(root), 1, 1, (".cpp",), mounts=mounts)
    cache = code_refs.load(str(refs))
    cache.pop("provenance_version", None)
    refs.write_text(json.dumps(cache), encoding="utf-8")
    safe_in = tmp_path / "safe.json"
    safe_out = tmp_path / "safe_filtered.json"
    _write_safe(safe_in)

    _run_filter(monkeypatch, root=root, refs=refs, safe_in=safe_in, safe_out=safe_out)

    assert json.loads(safe_out.read_text())["redirectors"] == []


def test_cache_with_incompatible_verification_mode_is_regenerated(tmp_path, monkeypatch):
    root, source = _make_project(tmp_path)
    (source / "ref.cpp").write_text('K = "/Game/R"', encoding="utf-8")
    refs = tmp_path / "refs.yaml"
    mounts = code_refs.discover_mount_points(str(root))
    code_refs.save(
        str(refs), {"/Game/R"}, str(root), 1, 1, (".cpp",), mounts=mounts,
        verify_on_disk=False, scope="/Game",
    )
    safe_in = tmp_path / "safe.json"
    safe_out = tmp_path / "safe_filtered.json"
    _write_safe(safe_in)

    _run_filter(monkeypatch, root=root, refs=refs, safe_in=safe_in, safe_out=safe_out)

    assert json.loads(safe_out.read_text())["redirectors"] == []


def test_orphan_phase_four_uses_filtered_output_and_has_no_exemption():
    skill = (_ROOT / "plugins" / "unreal-kit" / "skills" / "fix-up-redirectors" / "SKILL.md").read_text()

    assert "orphaned_filtered.json" in skill
    assert "Skip this phase entirely in orphan-only mode" not in skill
    assert "--mode=delete-only" in skill


def test_real_orphan_classifier_output_is_filtered_before_delete(tmp_path, monkeypatch):
    root, source = _make_project(tmp_path)
    redirector = root / "Content" / "R.uasset"
    (source / "ref.cpp").write_text('K = "/Game/R"', encoding="utf-8")
    discovery = tmp_path / "discovery.yaml"
    discovery.write_text(
        "scope: /Game\nredirectors:\n"
        f"  - pkg: /Game/R\n    file: {redirector}\n"
        "    target_exists: false\n    referencer_files: []\n"
        "    referencer_pkgs: []\n    has_level_referencer: false\n",
        encoding="utf-8",
    )
    orphaned = tmp_path / "orphaned.json"
    report = tmp_path / "report.json"
    classifier = _SCRIPTS / "classify_safety.py"
    fake_p4 = types.ModuleType("p4cli")
    fake_p4.get_workspace_mapping = lambda: ("//depot/project", str(root))
    fake_p4.get_opened_map = lambda: {}
    fake_p4.local_to_depot = lambda local, depot, local_root: depot + local[len(local_root):]
    fake_bootstrap = types.ModuleType("bootstrap_guard")
    fake_bootstrap.reexec_under_plugin_venv = lambda _name: None
    fake_repair = types.ModuleType("path_repair")
    fake_repair.repair_path = lambda: None
    saved = {name: sys.modules.get(name) for name in ("p4cli", "bootstrap_guard", "path_repair")}
    sys.modules.update({
        "p4cli": fake_p4,
        "bootstrap_guard": fake_bootstrap,
        "path_repair": fake_repair,
    })
    monkeypatch.setattr(
        sys, "argv", [
            str(classifier), "--discovery", str(discovery),
            "--out-safe", str(tmp_path / "safe.json"),
            "--out-orphaned", str(orphaned), "--out-report", str(report),
        ]
    )
    try:
        runpy.run_path(str(classifier), run_name="__main__")
    finally:
        for name, old in saved.items():
            if old is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old

    orphan_doc = json.loads(orphaned.read_text())
    assert [record["pkg"] for record in orphan_doc["redirectors"]] == ["/Game/R"]
    filtered = tmp_path / "orphaned_filtered.json"
    refs = tmp_path / "refs.yaml"
    _run_filter(
        monkeypatch, root=root, refs=refs, safe_in=orphaned, safe_out=filtered,
    )

    assert json.loads(filtered.read_text())["redirectors"] == []
