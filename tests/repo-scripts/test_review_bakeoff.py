"""The review bakeoff's `run` arm passes a registry id as the lane's --model."""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "review-bakeoff" / "run_bakeoff.py"
_SPEC = importlib.util.spec_from_file_location("run_bakeoff", _SCRIPT)
_BAKEOFF = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
sys.modules[_SPEC.name] = _BAKEOFF  # dataclasses resolve the module by name
_SPEC.loader.exec_module(_BAKEOFF)


@pytest.fixture
def launches(monkeypatch, tmp_path):
    """Record every lane launch; one corpus case, results under tmp_path."""
    case = _BAKEOFF.Case(case_id="c1", kind="positive", files=("a.py",), planted=())
    monkeypatch.setattr(_BAKEOFF, "load_cases", lambda: [case])
    monkeypatch.setattr(_BAKEOFF, "RESULTS", tmp_path / "results")
    calls = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="[]", stderr="")

    monkeypatch.setattr(_BAKEOFF.subprocess, "run", fake_run)
    return calls


def test_run_passes_the_arm_id_as_the_lane_model(launches):
    assert _BAKEOFF.main(["run", "--arm", "qwen38-5090-harness"]) == 0
    assert len(launches) == 1
    command = launches[0]
    assert command[command.index("--model") + 1] == "qwen38-5090-harness"


@pytest.mark.parametrize("arm", ["agent:sonnet", "peer:opus"])
def test_run_refuses_a_prefixed_arm(arm, launches, capsys):
    assert _BAKEOFF.main(["run", "--arm", arm]) == 1
    assert launches == []
    assert "registry id" in capsys.readouterr().err


@pytest.mark.parametrize("arm", ["sonnet", "opus", "fable", "haiku"])
def test_run_refuses_a_claude_harness_id(arm, launches, capsys):
    assert _BAKEOFF.main(["run", "--arm", arm]) == 1
    assert launches == []
    err = capsys.readouterr().err
    assert "harness: claude" in err
    assert "prompts" in err
