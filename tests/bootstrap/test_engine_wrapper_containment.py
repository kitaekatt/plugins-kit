"""Tests for import failures before bootstrap_lib.engine can contain a crash."""

import json
import runpy
import sys
from pathlib import Path

import pytest

from bootstrap_lib import display_relay


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


def test_wrapper_import_failure_queues_sidecar_without_losing_pending_verdict(
    tmp_path, monkeypatch, capsys
):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    primary = data_dir / display_relay.PENDING_NAME
    sidecar = data_dir / display_relay.WRAPPER_IMPORT_PENDING_NAME
    payload_a = b'{"continue": true, "systemMessage": "verdict A"}'
    primary.write_bytes(payload_a)

    def run_wrapper() -> None:
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

    run_wrapper()
    payload_b = sidecar.read_bytes()
    run_wrapper()

    assert primary.read_bytes() == payload_a
    assert sidecar.read_bytes() == payload_b

    assert display_relay.relay(str(data_dir), now=0) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["systemMessage"].startswith("verdict A")
    assert primary.exists() is False
    assert sidecar.exists()

    assert display_relay.relay(str(data_dir), now=0) == 0
    second = json.loads(capsys.readouterr().out)
    assert "bootstrap wrapper import failed" in second["systemMessage"]
    assert sidecar.exists() is False
