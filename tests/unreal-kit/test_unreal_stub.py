"""Tests for durable enriched and machine-local stock Unreal API stubs."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

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
    source.write_text("VALUE = 'enriched'\n", encoding="ascii")
    config = {"uproject": str(uproject)}
    destination = unreal_stub.durable_stub_path(project_root, config)
    announcements: list[tuple[str, bool]] = []

    result = unreal_stub.refresh_durable_stub(
        project_root,
        config,
        lambda message: announcements.append((message, destination.exists())),
    )

    assert result == destination
    assert destination.read_text(encoding="ascii") == "VALUE = 'enriched'\n"
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
    source.write_text("VALUE = 'enriched'\n", encoding="ascii")
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
    stock.write_text("VALUE = 'stock'\n", encoding="ascii")

    assert unreal_stub.select_search_stub(project_root, config) == stock

    enriched.parent.mkdir(parents=True)
    enriched.write_text("VALUE = 'enriched'\n", encoding="ascii")
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


def _make_read_only(path: Path) -> None:
    path.chmod(stat.S_IREAD)


def _make_writable(path: Path) -> None:
    path.chmod(stat.S_IWRITE | stat.S_IREAD)


def _configured_project(tmp_path: Path, source_content: str) -> tuple[Path, dict]:
    project_root = tmp_path / "project"
    project_root.mkdir()
    uproject = project_root / "Game.uproject"
    uproject.write_text("{}", encoding="ascii")
    source = project_root / "Intermediate" / "PythonStub" / "unreal.py"
    source.parent.mkdir(parents=True)
    source.write_text(source_content, encoding="ascii")
    return project_root, {"uproject": str(uproject)}


def test_refresh_raises_actionable_error_for_read_only_destination(
    tmp_path: Path,
) -> None:
    project_root, config = _configured_project(tmp_path, "VALUE = 'enriched-v2'\n")
    destination = unreal_stub.durable_stub_path(project_root, config)
    destination.parent.mkdir(parents=True)
    destination.write_text("VALUE = 'enriched-v1'\n", encoding="ascii")
    _make_read_only(destination)

    try:
        with pytest.raises(unreal_stub.DestinationNotWritableError) as excinfo:
            unreal_stub.refresh_durable_stub(project_root, config, lambda _: None)
        # Actionable: names the real path, no raw traceback surfaces to the caller.
        assert str(destination) in str(excinfo.value)
        # No partial write: the read-only destination is untouched.
        assert destination.read_text(encoding="ascii") == "VALUE = 'enriched-v1'\n"
    finally:
        _make_writable(destination)


def test_refresh_overwrites_writable_existing_destination(tmp_path: Path) -> None:
    project_root, config = _configured_project(tmp_path, "VALUE = 'enriched-v2'\n")
    destination = unreal_stub.durable_stub_path(project_root, config)
    destination.parent.mkdir(parents=True)
    destination.write_text("VALUE = 'enriched-v1'\n", encoding="ascii")

    result = unreal_stub.refresh_durable_stub(project_root, config, lambda _: None)

    assert result == destination
    assert destination.read_text(encoding="ascii") == "VALUE = 'enriched-v2'\n"


def test_refresh_short_circuits_when_destination_already_matches(
    tmp_path: Path,
) -> None:
    project_root, config = _configured_project(tmp_path, "VALUE = 'enriched'\n")
    destination = unreal_stub.durable_stub_path(project_root, config)
    destination.parent.mkdir(parents=True)
    destination.write_text("VALUE = 'enriched'\n", encoding="ascii")
    # Read-only AND identical: must short-circuit on content equality before
    # ever reaching the writability check, so this must NOT raise.
    _make_read_only(destination)
    announcements: list[str] = []

    try:
        result = unreal_stub.refresh_durable_stub(
            project_root, config, announcements.append
        )
        assert result == destination
        assert announcements == [
            f"Unreal API stub already up to date at {destination}"
        ]
    finally:
        _make_writable(destination)


def test_refresh_copy_failure_preserves_old_bytes_and_cleans_candidate(
    tmp_path: Path, monkeypatch
) -> None:
    project_root, config = _configured_project(
        tmp_path, "VALUE = 'enriched-v2'\n"
    )
    destination = unreal_stub.durable_stub_path(project_root, config)
    destination.parent.mkdir(parents=True)
    old_bytes = b"VALUE = 'enriched-v1'\n"
    destination.write_bytes(old_bytes)
    candidates: list[Path] = []

    def partial_copy(
        source: str | os.PathLike[str], target: str | os.PathLike[str]
    ) -> None:
        candidate = Path(target)
        candidates.append(candidate)
        candidate.write_bytes(b"VALUE = 'partial'")
        raise OSError("simulated copy failure")

    monkeypatch.setattr(unreal_stub.shutil, "copy2", partial_copy)

    with pytest.raises(OSError, match="simulated copy failure"):
        unreal_stub.refresh_durable_stub(project_root, config, lambda _: None)

    assert destination.read_bytes() == old_bytes
    assert candidates and not candidates[0].exists()


def test_refresh_replace_failure_preserves_old_bytes_and_cleans_candidate(
    tmp_path: Path, monkeypatch
) -> None:
    project_root, config = _configured_project(
        tmp_path, "VALUE = 'enriched-v2'\n"
    )
    destination = unreal_stub.durable_stub_path(project_root, config)
    destination.parent.mkdir(parents=True)
    old_bytes = b"VALUE = 'enriched-v1'\n"
    destination.write_bytes(old_bytes)
    candidates: list[Path] = []
    def fail_replace(
        source: str | os.PathLike[str], target: str | os.PathLike[str]
    ) -> None:
        candidates.append(Path(source))
        raise OSError("simulated replace failure")

    monkeypatch.setattr(unreal_stub.os, "replace", fail_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        unreal_stub.refresh_durable_stub(project_root, config, lambda _: None)

    assert destination.read_bytes() == old_bytes
    assert candidates and not candidates[0].exists()


def test_refresh_rejects_empty_source_without_message_or_mutation(
    tmp_path: Path,
) -> None:
    project_root, config = _configured_project(tmp_path, "")
    destination = unreal_stub.durable_stub_path(project_root, config)
    destination.parent.mkdir(parents=True)
    old_bytes = b"VALUE = 'old'\n"
    destination.write_bytes(old_bytes)
    announcements: list[str] = []

    with pytest.raises(unreal_stub.StubValidationError, match="empty"):
        unreal_stub.refresh_durable_stub(project_root, config, announcements.append)

    assert destination.read_bytes() == old_bytes
    assert announcements == []
    assert list(destination.parent.glob(f".{destination.name}.*")) == []


def test_refresh_rejects_invalid_python_without_importing_it(tmp_path: Path) -> None:
    project_root, config = _configured_project(
        tmp_path, "raise RuntimeError('must not execute')\n"
    )
    source = unreal_stub.generated_stub_path(config)
    assert source is not None
    source.write_text("def broken(:\n", encoding="utf-8")

    with pytest.raises(unreal_stub.StubValidationError, match="valid Python"):
        unreal_stub.refresh_durable_stub(project_root, config, lambda _: None)


def test_refresh_validates_candidate_after_copy(tmp_path: Path, monkeypatch) -> None:
    project_root, config = _configured_project(tmp_path, "VALUE = 'valid'\n")
    destination = unreal_stub.durable_stub_path(project_root, config)
    destination.parent.mkdir(parents=True)
    destination.write_text("VALUE = 'old'\n", encoding="ascii")
    real_copy = unreal_stub.shutil.copy2

    def corrupt_copy(
        source: str | os.PathLike[str], target: str | os.PathLike[str]
    ) -> None:
        real_copy(source, target)
        Path(target).write_text("not valid Python :\n", encoding="ascii")

    monkeypatch.setattr(unreal_stub.shutil, "copy2", corrupt_copy)

    with pytest.raises(unreal_stub.StubValidationError, match="valid Python"):
        unreal_stub.refresh_durable_stub(project_root, config, lambda _: None)

    assert destination.read_text(encoding="ascii") == "VALUE = 'old'\n"
    assert list(destination.parent.glob(f".{destination.name}.*")) == []


def test_search_falls_back_from_empty_enriched_and_discloses_reason(
    tmp_path: Path, monkeypatch
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    data_root = tmp_path / "data"
    monkeypatch.setattr(unreal_stub, "default_data_root", lambda: data_root)
    config: dict[str, object] = {}
    enriched = unreal_stub.durable_stub_path(project_root, config)
    enriched.parent.mkdir(parents=True)
    enriched.write_text("\n", encoding="ascii")
    stock = unreal_stub.stock_stub_path()
    stock.parent.mkdir(parents=True)
    stock.write_text("VALUE = 'stock'\n", encoding="ascii")
    messages: list[str] = []

    assert (
        unreal_stub.select_search_stub(project_root, config, messages.append)
        == stock
    )
    assert messages and "falling back" in messages[0].lower()
    assert "enriched" in messages[0].lower()


def test_search_rejects_invalid_stock_as_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    data_root = tmp_path / "data"
    monkeypatch.setattr(unreal_stub, "default_data_root", lambda: data_root)
    stock = unreal_stub.stock_stub_path()
    stock.parent.mkdir(parents=True)
    stock.write_text("", encoding="ascii")

    assert unreal_stub.select_search_stub(project_root, {}) is None


def test_read_only_destination_refuses_replacement_even_on_posix(
    tmp_path: Path,
) -> None:
    project_root, config = _configured_project(tmp_path, "VALUE = 'new'\n")
    destination = unreal_stub.durable_stub_path(project_root, config)
    destination.parent.mkdir(parents=True)
    destination.write_text("VALUE = 'old'\n", encoding="ascii")
    _make_read_only(destination)
    try:
        with pytest.raises(unreal_stub.DestinationNotWritableError):
            unreal_stub.refresh_durable_stub(project_root, config, lambda _: None)
        assert destination.read_text(encoding="ascii") == "VALUE = 'old'\n"
    finally:
        _make_writable(destination)
