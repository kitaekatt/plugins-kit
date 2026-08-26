"""Tests for the live model-dispatch checker without launching a model."""

import importlib.util
from pathlib import Path

import pytest
import yaml


_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_model_dispatch.py"
_SPEC = importlib.util.spec_from_file_location("check_model_dispatch", _SCRIPT)
_CHECKER = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_CHECKER)


def _write_policy(tmp_path, policy):
    path = tmp_path / "orchestration.yaml"
    path.write_text(yaml.safe_dump(policy, sort_keys=False), encoding="utf-8")
    return path


def test_routing_rows_are_read_and_names_are_classified(tmp_path):
    policy = {
        "routing": [
            {"shape": ["cross-check"], "models": ["agent:fable", "sol"]},
            {"shape": [], "models": ["agent:sonnet"]},
        ],
        "backends": [],
    }
    probes = _CHECKER.collect_probes(
        policy,
        tmp_path / "orchestration.yaml",
        model_entries={
            "sol": {
                "harness": "codex",
                "model": "gpt-5.6-sol",
                "effort": "high",
            }
        },
    )

    routing = [probe for probe in probes if probe.is_routing]
    assert [(probe.kind, probe.value) for probe in routing] == [
        ("claude-model", "fable"),
        ("codex-model", "gpt-5.6-sol"),
        ("claude-model", "sonnet"),
    ]
    assert routing[0].extra == "entry=agent:fable"
    assert routing[1].extra == "entry=sol, effort=high"
    assert "routing[row=1].models[1]" in routing[1].where
    assert all("ladders" not in probe.where for probe in routing)


def test_unresolved_name_is_reported_and_changes_exit_code(
    tmp_path, monkeypatch, capsys
):
    policy_path = _write_policy(
        tmp_path,
        {"routing": [{"shape": [], "models": ["missing-model"]}], "backends": []},
    )
    monkeypatch.setattr(
        _CHECKER,
        "_load_harness_models",
        lambda _root: ({}, ["llm_scripting_kit unavailable; harness model rows skipped"]),
    )
    monkeypatch.setattr(_CHECKER, "collect_config_keys", lambda _path: {})

    assert _CHECKER.main(["--policy", str(policy_path)]) == 2

    output = capsys.readouterr().out
    assert "missing-model" in output
    assert "could not be resolved" in output
    assert "llm_scripting_kit unavailable" in output
    assert "NO VALIDATION" in output
    assert "PASS" not in output


def test_list_performs_no_dispatch(tmp_path, monkeypatch, capsys):
    policy_path = _write_policy(
        tmp_path,
        {
            "routing": [{"shape": [], "models": ["agent:fable", "sol"]}],
            "backends": [],
        },
    )
    monkeypatch.setattr(
        _CHECKER,
        "collect_config_keys",
        lambda _path: {},
    )
    monkeypatch.setattr(
        _CHECKER,
        "_load_harness_models",
        lambda _root: ({"sol": {"harness": "codex", "model": "gpt-5.6-sol"}}, []),
    )

    def fail_if_called(*_args, **_kwargs):
        pytest.fail("--list dispatched a probe")

    for name in (
        "probe_claude_model",
        "probe_codex_model",
        "probe_opencode_model",
        "probe_grok_model",
        "probe_codex_config_key",
    ):
        monkeypatch.setattr(_CHECKER, name, fail_if_called)

    assert _CHECKER.main(["--policy", str(policy_path), "--list"]) == 0

    output = capsys.readouterr().out
    assert "claude-model fable" in output
    assert "codex-model gpt-5.6-sol" in output
    assert "entry=sol" in output
    assert "no dispatch performed" in output
