"""``llm-kit models project`` inside the clean-venv closure.

The project model file is a harness-independent surface: it must run from a
plain install of the wheels (no ``bootstrap_lib``, no plugin tree, a scratch
HOME) and leave no ``~/.claude`` behind. The venvs are the ones the other
closure tests use (conftest.py); ``test_closure_catches_*`` breaks the closure on
purpose and requires the same judging function to report it.

    uv run --extra dev pytest tests/packaging --wheel-source local -v
"""
from __future__ import annotations

import json

import pytest

import closure_support as support

pytestmark = pytest.mark.wheel_closure

_DOC = {
    "version": 1,
    "catalog": [
        {"id": "second", "label": "Second", "endpoint": "openrouter", "model": "vendor/model-b"},
        {"id": "first", "label": "First", "endpoint": "sol", "model": "gpt-6.1-sol",
         "efforts": ["low", "high"]},
    ],
    "selections": {"k.two": {"model": "second"}, "k.one": {"model": "first", "effort": "high"}},
}


def _cli(venv, tmp_path, via, argv, stdin=None):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    work = tmp_path / "work"
    work.mkdir(parents=True, exist_ok=True)
    return support.run_cli(venv, via, argv, home=home, cwd=work, env={}, stdin=stdin)


def project_problems(venv, tmp_path, via="script") -> list[str]:
    """Why the project-file surface is not closed under ``venv``; empty when it is."""
    problems = []

    def step(label, argv, expect, stdin=None):
        proc = _cli(venv, tmp_path, via, argv, stdin=stdin)
        if proc.returncode != expect or "Traceback" in proc.stderr:
            problems.append(f"{label}: exit {proc.returncode} (wanted {expect}): {proc.stderr.strip()[-300:]}")
            return None
        return proc

    base = ["models", "project"]
    wrote = step("write", [*base, "write", "--input", "-"], 0, json.dumps(_DOC))
    if wrote is not None:
        out = json.loads(wrote.stdout)
        shown = step("show", [*base, "show"], 0)
        if shown is not None:
            view = json.loads(shown.stdout)
            if [r["id"] for r in view["document"]["catalog"]] != ["second", "first"]:
                problems.append("catalogue order was not preserved")
            if view["sha256"] != out["sha256"] or view["path"] != ".llm-kit/models.yaml":
                problems.append(f"show disagrees with write: {view['path']} {view['sha256']}")
        step("stale", [*base, "write", "--input", "-", "--if-match", "0" * 64], 2, json.dumps(_DOC))
        local = step(
            "local override",
            [*base, "write", "--layer", "local", "--key", "k.one", "--model", "first", "--effort", "low"], 0,
        )
        if local is not None:
            view = json.loads(step("show with override", [*base, "show"], 0).stdout)
            got = view["resolved_selections"]["k.one"]
            if got["source"] != "override" or got["effective"]["effort"] != "low":
                problems.append(f"local override did not win: {got}")
            if json.loads(local.stdout)["path"] != ".llm-kit/models.local.yaml":
                problems.append("the personal file is not beside the project file")
            step("clear", [*base, "clear", "--key", "k.one"], 0)
    step("escape", [*base, "write", "--file", "../elsewhere.yaml", "--input", "-"], 2, json.dumps(_DOC))
    if (tmp_path / "elsewhere.yaml").exists():
        problems.append("a path outside the project root was written")
    if (tmp_path / "home" / ".claude").exists():
        problems.append("HOME/.claude was created")
    return problems


@pytest.mark.parametrize("via", ("script", "module"))
def test_project_file_runs_from_a_plain_install(closure_venv, tmp_path, via):
    assert project_problems(closure_venv, tmp_path, via) == []
    assert (tmp_path / "work" / ".llm-kit" / "models.yaml").is_file()
    assert not (tmp_path / "work" / ".llm-kit" / "models.local.yaml").exists()  # cleared when empty


def test_project_file_names_the_extra_when_pyyaml_is_absent(base_venv, tmp_path):
    proc = _cli(base_venv, tmp_path, "script", ["models", "project", "validate", "--input", "-"],
                stdin=json.dumps(_DOC))
    assert proc.returncode == 2 and "Traceback" not in proc.stderr
    error = json.loads(proc.stderr)["error"]
    assert error["code"] == "dependency" and "katsoft-llm-kit[config]" in error["message"]


def test_closure_catches_a_project_module_that_imports_bootstrap_lib(local_wheels, tmp_path):
    member = "llm_kit/registry/project_models.py"

    def mutate(members):
        members[member] = members[member] + b"\nimport bootstrap_lib\n"

    broken = support.rewrite_wheel(local_wheels["kit"], tmp_path / "inject", mutate)
    venv = support.make_venv(tmp_path / "v", [str(local_wheels["contracts"]), str(broken), "pyyaml>=6.0"])
    problems = project_problems(venv, tmp_path / "run")
    assert problems and any("bootstrap_lib" in p for p in problems), problems
