"""Tests for import failures before bootstrap_lib.engine can contain a crash."""

import json
import runpy
import sys
from pathlib import Path

import pytest


WRAPPER = (
    Path(__file__).resolve().parents[2]
    / "plugins"
    / "bootstrap"
    / "engine"
    / "bootstrap_engine.py"
)


def test_wrapper_reports_poisoned_first_party_import(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setitem(sys.modules, "bootstrap_lib.records", None)
    monkeypatch.delitem(sys.modules, "bootstrap_lib.engine", raising=False)
    monkeypatch.setattr(sys, "argv", [
        str(WRAPPER),
        "--data-dir", str(data_dir),
        "--plugin-root", str(tmp_path / "root"),
    ])

    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(WRAPPER), run_name="__main__")

    assert exc.value.code == 1
    pending = data_dir / "bootstrap_display.pending"
    assert pending.is_file()
    response = json.loads(pending.read_text())
    assert "bootstrap_lib.records" in response["systemMessage"]
    assert "bootstrap_lib.records" in (data_dir / "bootstrap.log").read_text()
