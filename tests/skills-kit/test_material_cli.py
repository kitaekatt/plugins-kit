"""The `material` command of scripts/skills_kit_tool.py (skills_kit_lib/material.py).

Rows that exercise the library use the REAL bootstrap_lib.skill_material. A
fake library appears only where the row is about a library that misbehaves
(the report's `to_json`), and the three unavailable states are produced in a
child interpreter whose `sitecustomize` blocks a module, because the command
is a launcher entry point and the block must be in force before anything
imports.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

import bootstrap_lib.skill_material as real
from skills_kit_lib import material

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL = REPO_ROOT / "plugins" / "skills-kit" / "scripts" / "skills_kit_tool.py"
BOOTSTRAP_PLUGIN = REPO_ROOT / "plugins" / "bootstrap"

INSTALL = "claude plugin install bootstrap@plugins-kit"
UPDATE = "claude plugin update bootstrap@plugins-kit"

INTRO = (
    'Skill material selected by the caller for this request. A skill at level '
    '"catalog" is listed by name and description only; its instructions are not '
    "included and cannot be loaded in this request."
)

ALPHA_GOLDEN = (
    '<skill_context version="1">\n'
    + INTRO
    + "\n"
    + '<skill name="alpha" level="full">\n'
    "<description>Does alpha.</description>\n"
    "<instructions>\n"
    "Body line.\n"
    "</instructions>\n"
    "</skill>\n"
    "</skill_context>\n"
)


def _skill(root: Path, name: str, body: str = "Body line.\n", description: str = None) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    desc = description if description is not None else f"Does {name}."
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\n{body}", encoding="utf-8", newline="\n"
    )
    return directory


@pytest.fixture
def skills(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    return root


def _main(capsys, *argv):
    code = material.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _child(cwd, *args, block=None, env_extra=None):
    """Run the launcher in a child interpreter. `block` names a module the
    child's sitecustomize blocks (sys.modules[name] = None)."""
    env = dict(os.environ, _BOOTSTRAP_GUARD_VENV_REEXEC="1", PYTHONDONTWRITEBYTECODE="1")
    paths = [str(BOOTSTRAP_PLUGIN)]
    if block:
        shim = Path(cwd) / "_shim"
        shim.mkdir(exist_ok=True)
        (shim / "sitecustomize.py").write_text(
            f"import sys\nsys.modules[{block!r}] = None\n", encoding="utf-8"
        )
        paths.insert(0, str(shim))
    env["PYTHONPATH"] = os.pathsep.join(paths)
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, str(TOOL), "material", *args],
        cwd=str(cwd), capture_output=True, env=env, timeout=120,
    )


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


def test_text_output_equals_materialized_text(skills, capsys):
    _skill(skills, "alpha")
    code, out, err = _main(capsys, "--skill", str(skills / "alpha"), "--budget", "1000")
    assert (code, err) == (0, "")
    assert out == ALPHA_GOLDEN  # the block plus exactly one newline, no banner
    expected = real.materialize(
        real.SkillSelection.from_json(
            {"skills": [{"path": str(skills / "alpha")}], "token_budget": 1000}
        )
    ).text
    assert out == expected + "\n"


def test_output_is_utf8_with_lf_through_the_launcher(skills):
    _skill(skills, "alpha", description="Caf\u00e9 skill.")
    run = _child(skills, "--skill", "alpha", "--budget", "1000")
    assert run.returncode == 0, run.stderr
    assert run.stdout == ALPHA_GOLDEN.replace("Does alpha.", "Caf\u00e9 skill.").encode("utf-8")
    assert b"\r" not in run.stdout


def test_json_output_is_report_to_json(skills, capsys):
    _skill(skills, "alpha")
    code, out, err = _main(
        capsys, "--skill", str(skills / "alpha"), "--budget", "1000", "--json"
    )
    assert (code, err) == (0, "")
    document = json.loads(out)
    expected = real.materialize(
        real.SkillSelection.from_json(
            {"skills": [{"path": str(skills / "alpha")}], "token_budget": 1000}
        )
    ).report.to_json()
    assert document == expected
    assert "text" not in document
    assert out == json.dumps(expected, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    assert out.isascii()


@pytest.mark.parametrize(
    "argv, stderr_has, exit_code",
    [
        (["--skill", "{work}/missing", "--budget", "1000"], "missing", 1),
        (["--skill", "{work}/alpha", "--budget", "30"], "over budget", 4),
        (["--skill", "{work}/alpha"], "--budget", 2),
    ],
)
def test_failure_prints_nothing_on_stdout(skills, capsys, argv, stderr_has, exit_code):
    _skill(skills, "alpha")
    argv = [a.replace("{work}", str(skills)) for a in argv]
    code, out, err = _main(capsys, *argv)
    assert code == exit_code
    assert out == ""
    assert stderr_has in err


def test_failure_prints_nothing_on_stdout_when_unavailable(skills, capsys, monkeypatch):
    _skill(skills, "alpha")
    monkeypatch.setitem(sys.modules, "bootstrap_lib", None)
    code, out, err = _main(capsys, "--skill", str(skills / "alpha"), "--budget", "1000")
    assert (code, out) == (3, "")
    assert err.startswith("material: ")


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_invalid_frontmatter_exits_1_naming_the_file(skills, capsys):
    directory = skills / "bad"
    directory.mkdir()
    (directory / "SKILL.md").write_text("# no frontmatter\n", encoding="utf-8")
    code, out, err = _main(capsys, "--skill", str(directory), "--budget", "1000")
    assert code == 1
    assert out == ""
    assert err.startswith("material: ")
    assert "SKILL.md" in err and "no frontmatter block" in err


def test_over_budget_exits_4_and_itemizes(skills, capsys):
    _skill(skills, "alpha")
    code, out, err = _main(capsys, "--skill", str(skills / "alpha"), "--budget", "30")
    assert code == 4
    assert out == ""
    assert "over budget" in err
    assert "skill 'alpha' (full)" in err  # the itemized line
    assert "budget 30" in err
    assert "To fit:" in err


# ---------------------------------------------------------------------------
# The unavailable states: exit 3, produced in a child interpreter
# ---------------------------------------------------------------------------


def test_child_control_without_a_block_renders(skills):
    _skill(skills, "alpha")
    run = _child(skills, "--skill", "alpha", "--budget", "1000")
    assert run.returncode == 0, run.stderr
    assert run.stdout == ALPHA_GOLDEN.encode("utf-8")


def test_absent_bootstrap_lib_exits_3(skills):
    _skill(skills, "alpha")
    run = _child(skills, "--skill", "alpha", "--budget", "1000", block="bootstrap_lib")
    assert run.returncode == 3
    assert run.stdout == b""
    err = run.stderr.decode("utf-8")
    assert err.startswith("material: ")
    assert INSTALL in err
    assert "scripts/skills_kit_tool.py" in err


def test_old_bootstrap_exits_3_naming_update(skills):
    _skill(skills, "alpha")
    run = _child(
        skills, "--skill", "alpha", "--budget", "1000", block="bootstrap_lib.skill_material"
    )
    assert run.returncode == 3
    assert run.stdout == b""
    err = run.stderr.decode("utf-8")
    assert UPDATE in err
    assert material.SKILL_MATERIAL_BOOTSTRAP in err
    assert INSTALL not in err


def test_without_yaml_exits_3_naming_pyyaml(skills):
    _skill(skills, "alpha")
    run = _child(skills, "--skill", "alpha", "--budget", "1000", block="yaml")
    assert run.returncode == 3
    assert run.stdout == b""  # never a degraded block
    err = run.stderr.decode("utf-8")
    assert "PyYAML" in err
    assert "scripts/skills_kit_tool.py" in err
    assert UPDATE not in err and INSTALL not in err


# ---------------------------------------------------------------------------
# A library that misbehaves in the report
# ---------------------------------------------------------------------------


def _library_with(to_json):
    """The real library with `materialize` returning a report whose `to_json`
    is the given function."""

    class Report:
        def to_json(self):
            return to_json()

    def materialize(selection, *, base_dir=None):
        return types.SimpleNamespace(text="block", report=Report())

    return types.SimpleNamespace(
        SkillSelection=real.SkillSelection,
        materialize=materialize,
        SkillMaterialError=real.SkillMaterialError,
        SkillMaterialBudgetExceeded=real.SkillMaterialBudgetExceeded,
        PyYamlUnavailableError=real.PyYamlUnavailableError,
    )


def _raise_type_error():
    raise TypeError("to_json() takes 0 positional arguments")


BAD_DOCUMENTS = {
    "top-level object": lambda: {"bad": object()},
    "nested object": lambda: {"a": {"b": [1, {"c": object()}]}},
    "tuple": lambda: {"a": (1, 2)},
    "non-string key": lambda: {1: "x"},
    "not a mapping": lambda: ["a"],
    "non-finite float": lambda: {"a": float("nan")},
}


def test_report_to_json_failure_exits_3_not_a_traceback(skills, capsys, monkeypatch):
    _skill(skills, "alpha")
    monkeypatch.setattr(material, "load_skill_material", lambda: _library_with(_raise_type_error))
    code, out, err = _main(
        capsys, "--skill", str(skills / "alpha"), "--budget", "1000", "--json"
    )
    assert (code, out) == (3, "")
    assert UPDATE in err


@pytest.mark.parametrize("name", sorted(BAD_DOCUMENTS))
def test_report_with_a_non_json_native_value_exits_3(skills, capsys, monkeypatch, name):
    _skill(skills, "alpha")
    monkeypatch.setattr(material, "load_skill_material", lambda: _library_with(BAD_DOCUMENTS[name]))
    code, out, err = _main(
        capsys, "--skill", str(skills / "alpha"), "--budget", "1000", "--json"
    )
    assert (code, out) == (3, "")
    assert UPDATE in err


def test_a_json_native_report_passes_the_same_check(skills, capsys, monkeypatch):
    """Control for the rows above: the check refuses the listed shapes, not
    every fake library."""
    _skill(skills, "alpha")
    good = lambda: {"a": [1, 2.5, None, True, {"b": "c"}]}  # noqa: E731
    monkeypatch.setattr(material, "load_skill_material", lambda: _library_with(good))
    code, out, err = _main(
        capsys, "--skill", str(skills / "alpha"), "--budget", "1000", "--json"
    )
    assert (code, err) == (0, "")
    assert json.loads(out) == good()


# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------


def test_missing_budget_is_usage_error_exit_2(skills, capsys):
    _skill(skills, "alpha")
    code, out, err = _main(capsys, "--skill", str(skills / "alpha"))
    assert (code, out) == (2, "")
    assert "--budget" in err and err.count("material: error:") == 1


def test_no_skill_is_a_usage_error(capsys):
    code, out, err = _main(capsys, "--budget", "100")
    assert (code, out) == (2, "")
    assert "--skill" in err


def test_non_positive_budget_is_a_usage_error(skills, capsys):
    _skill(skills, "alpha")
    code, out, err = _main(capsys, "--skill", str(skills / "alpha"), "--budget", "0")
    assert (code, out) == (2, "")
    assert "greater than 0" in err


def test_modifiers_apply_to_preceding_skill(skills, capsys):
    _skill(skills, "a")
    _skill(skills, "b")
    a, b = str(skills / "a"), str(skills / "b")

    def levels(*argv):
        code, out, err = _main(capsys, *argv, "--budget", "5000", "--json")
        assert (code, err) == (0, "")
        return {s["name"]: s["level"] for s in json.loads(out)["skills"]}

    assert levels("--skill", a, "--catalog", "--skill", b) == {"a": "catalog", "b": "full"}
    assert levels("--skill", a, "--skill", b, "--catalog") == {"a": "full", "b": "catalog"}


def test_modifier_before_skill_is_usage_error(skills, capsys):
    _skill(skills, "a")
    for flag in (["--catalog"], ["--no-declared"], ["--resource", "references/x.md"]):
        code, out, err = _main(
            capsys, *flag, "--skill", str(skills / "a"), "--budget", "1000"
        )
        assert (code, out) == (2, ""), flag
        assert "must come after the --skill" in err


def _skill_with_declared_resource(skills):
    directory = _skill(
        skills,
        "decl",
        body=(
            "Intro.\n\n```yaml\nreferences:\n  - id: details\n"
            "    path: references/details.md\n```\n"
        ),
    )
    (directory / "references").mkdir()
    (directory / "references" / "details.md").write_text("Detail text.\n", encoding="utf-8")
    (directory / "references" / "extra.md").write_text("Extra text.\n", encoding="utf-8")
    return directory


def test_default_includes_declared_resources(skills, capsys):
    directory = _skill_with_declared_resource(skills)
    code, out, err = _main(capsys, "--skill", str(directory), "--budget", "5000")
    assert (code, err) == (0, "")
    assert '<resource path="references/details.md">\nDetail text.\n</resource>' in out
    assert "Extra text." not in out


def test_no_declared_omits_declared_resources(skills, capsys):
    directory = _skill_with_declared_resource(skills)
    code, out, err = _main(
        capsys, "--skill", str(directory), "--no-declared", "--budget", "5000"
    )
    assert (code, err) == (0, "")
    assert "Detail text." not in out
    assert "<resource" not in out


def test_resource_adds_a_named_file(skills, capsys):
    directory = _skill_with_declared_resource(skills)
    code, out, err = _main(
        capsys, "--skill", str(directory), "--no-declared",
        "--resource", "references/extra.md", "--budget", "5000",
    )
    assert (code, err) == (0, "")
    assert '<resource path="references/extra.md">\nExtra text.\n</resource>' in out
    assert "Detail text." not in out


def test_skill_order_is_command_line_order(skills, capsys):
    _skill(skills, "a")
    _skill(skills, "b")
    code, out, err = _main(
        capsys, "--skill", str(skills / "b"), "--skill", str(skills / "a"), "--budget", "5000"
    )
    assert (code, err) == (0, "")
    assert out.index('<skill name="b"') < out.index('<skill name="a"')


# ---------------------------------------------------------------------------
# The launcher
# ---------------------------------------------------------------------------


def test_launcher_runs_material_from_any_directory(skills):
    _skill(skills, "alpha")
    elsewhere = skills.parent / "elsewhere"
    elsewhere.mkdir()
    run = _child(elsewhere, "--skill", str(skills / "alpha"), "--budget", "1000")
    assert run.returncode == 0, run.stderr
    assert run.stdout == ALPHA_GOLDEN.encode("utf-8")


def test_relative_skill_path_resolves_against_cwd(skills):
    _skill(skills, "alpha")
    run = _child(skills.parent, "--skill", "work/alpha", "--budget", "1000")
    assert run.returncode == 0, run.stderr
    assert run.stdout == ALPHA_GOLDEN.encode("utf-8")
    inside = _child(skills, "--skill", "alpha", "--budget", "1000")
    assert inside.stdout == run.stdout
    outside = _child(skills.parent, "--skill", "alpha", "--budget", "1000")
    assert outside.returncode == 1  # the path is relative to the caller's directory


def test_launcher_usage_error_exits_2_with_argparse_message(skills):
    run = _child(skills, "--budget", "5")
    assert run.returncode == 2
    assert run.stdout == b""
    assert b"material: error:" in run.stderr


# ---------------------------------------------------------------------------
# Exit codes
# ---------------------------------------------------------------------------


def test_exit_code_constants_are_0_1_2_3_4():
    assert (
        material.EXIT_OK,
        material.EXIT_REFUSED,
        material.EXIT_USAGE,
        material.EXIT_UNAVAILABLE,
        material.EXIT_OVER_BUDGET,
    ) == (0, 1, 2, 3, 4)


def test_usage_exit_is_outside_the_verdict_mapping(skills, capsys):
    assert material.EXIT_USAGE == 2
    assert material.EXIT_USAGE not in material.OUTCOMES
    assert set(material.OUTCOMES) == {
        material.EXIT_OK,
        material.EXIT_REFUSED,
        material.EXIT_UNAVAILABLE,
        material.EXIT_OVER_BUDGET,
    }
    assert set(material.OUTCOMES.values()) == {
        "RENDERED", "REFUSED", "UNAVAILABLE", "OVER-BUDGET"
    }
    code, out, err = _main(capsys, "--no-such-flag")
    assert (code, out) == (2, "")
    assert "material: error:" in err
