"""Tests for durable enriched and machine-local stock Unreal API stubs."""

from __future__ import annotations

import sys
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "unreal-kit"
_LIB_DIR = _PLUGIN_DIR / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import unreal_stub


def test_refresh_announces_before_writing_durable_destination(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    uproject = project_root / "Game.uproject"
    uproject.write_text("{}", encoding="ascii")
    source = project_root / "Intermediate" / "PythonStub" / "unreal.py"
    source.parent.mkdir(parents=True)
    source.write_text("enriched", encoding="ascii")
    config = {"uproject": str(uproject)}
    destination = unreal_stub.durable_stub_path(project_root, config)
    announcements: list[tuple[str, bool]] = []

    result = unreal_stub.refresh_durable_stub(
        project_root,
        config,
        lambda message: announcements.append((message, destination.exists())),
    )

    assert result == destination
    assert destination.read_text(encoding="ascii") == "enriched"
    assert announcements == [
        (f"Writing enriched Unreal API stub: {source} -> {destination}", False)
    ]


def test_refresh_honors_plugin_data_dir_override(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    uproject = project_root / "Game.uproject"
    uproject.write_text("{}", encoding="ascii")
    source = project_root / "Intermediate" / "PythonStub" / "unreal.py"
    source.parent.mkdir(parents=True)
    source.write_text("enriched", encoding="ascii")
    config = {"uproject": str(uproject), "plugin_data_dir": "Generated/PluginData"}

    result = unreal_stub.refresh_durable_stub(project_root, config, lambda _: None)

    assert result == project_root / "Generated" / "PluginData" / "unreal.py"


def test_search_prefers_enriched_then_stock(tmp_path: Path, monkeypatch) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    data_root = tmp_path / "data"
    monkeypatch.setattr(unreal_stub, "default_data_root", lambda: data_root)
    config: dict[str, object] = {}
    enriched = unreal_stub.durable_stub_path(project_root, config)
    stock = unreal_stub.stock_stub_path()
    stock.parent.mkdir(parents=True)
    stock.write_text("stock", encoding="ascii")

    assert unreal_stub.select_search_stub(project_root, config) == stock

    enriched.parent.mkdir(parents=True)
    enriched.write_text("enriched", encoding="ascii")
    assert unreal_stub.select_search_stub(project_root, config) == enriched


def test_search_returns_none_when_neither_stub_exists(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    monkeypatch.setattr(unreal_stub, "default_data_root", lambda: tmp_path / "data")

    assert unreal_stub.select_search_stub(project_root, {}) is None


def test_reads_deferred_requirement_prepared_statement(
    tmp_path: Path,
    monkeypatch,
) -> None:
    data_root = tmp_path / "data"
    monkeypatch.setattr(unreal_stub, "default_data_root", lambda: data_root)
    record = (
        data_root
        / "plugins-kit"
        / "unreal-kit"
        / "deferred_requirements.json"
    )
    record.parent.mkdir(parents=True)
    record.write_text(
        '{"requirements":[{"name":"unreal_enriched_stub",'
        '"agent_msg":"prepared"}]}',
        encoding="ascii",
    )

    assert unreal_stub.deferred_requirement_message("unreal_enriched_stub") == "prepared"
