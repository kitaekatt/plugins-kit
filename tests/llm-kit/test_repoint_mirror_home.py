"""The re-pointed mirror runs under an empty HOME; the legacy run keeps the real one.

llm-kit ships no default model declaration, so a host's machine-local ``config.yaml`` that
names a ``default_endpoint`` would change which mirrored tests pass. The mirror run therefore
gets an empty HOME and USERPROFILE (``repoint.run_pytest``).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import repoint


@pytest.fixture
def captured(monkeypatch, tmp_path):
    seen = {}

    def fake_run(cmd, **kwargs):
        env = kwargs["env"]
        seen["env"] = env
        seen["home_listing"] = sorted(p.name for p in Path(env["HOME"]).iterdir()) if "HOME" in env \
            and Path(env["HOME"]).is_dir() else None
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(repoint.subprocess, "run", fake_run)
    monkeypatch.setenv("HOME", str(tmp_path / "real-home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "real-home"))
    return seen


def test_the_mirror_run_gets_an_empty_home(captured, tmp_path):
    repoint.run_pytest("tmp/x/test_a.py", tmp_path / "j.xml", mirror=True)
    env = captured["env"]
    assert env["HOME"] == env["USERPROFILE"]
    assert env["HOME"] != str(tmp_path / "real-home")
    assert captured["home_listing"] == []          # empty while the child ran
    assert not Path(env["HOME"]).exists()          # removed afterwards


def test_the_legacy_run_keeps_the_real_home(captured, tmp_path):
    repoint.run_pytest("tests/x/test_a.py", tmp_path / "j.xml", mirror=False)
    assert captured["env"]["HOME"] == str(tmp_path / "real-home")
    assert captured["env"]["USERPROFILE"] == str(tmp_path / "real-home")
