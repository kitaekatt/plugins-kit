"""parse_frontmatter(mode="strict") and the skill-material binding.

mode="strict" delegates to bootstrap_lib.skill_material.parse_frontmatter_strict
through skills_kit_lib.material, the one binding skills-kit keeps to that
library. It is the only mode that raises; "light" and "full" keep their code
and results. Rows that exercise the library use the REAL module; fake modules
appear only in the absent and too-old rows, which assert `state` and the
remedy command.
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
from skills_kit_lib.markdown_heuristics import (
    FRONTMATTER_RE,
    Frontmatter,
    StrictFrontmatterError,
    parse_frontmatter,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP_PLUGIN_JSON = REPO_ROOT / "plugins" / "bootstrap" / ".claude-plugin" / "plugin.json"
REPO_SKILLS = sorted(REPO_ROOT.glob("plugins/*/skills/*/SKILL.md"))

DOC = """---
name: my-skill
description: "Does a thing."
disable-model-invocation: true
---
# Title
"""
NO_BLOCK = "# T\nno frontmatter\n"
INVALID_YAML = "---\n: : not valid : yaml :\n---\n# T\n"
NON_MAPPING = "---\n- a\n- b\n---\n# T\n"

INSTALL = "claude plugin install bootstrap@plugins-kit"
UPDATE = "claude plugin update bootstrap@plugins-kit"


def _version(text: str) -> tuple:
    return tuple(int(part) for part in text.split("."))


# ---------------------------------------------------------------------------
# The other modes are unchanged
# ---------------------------------------------------------------------------


def test_unknown_mode_behaves_as_light():
    light = parse_frontmatter(DOC)
    other = parse_frontmatter(DOC, mode="no-such-mode")
    assert other is not None and light is not None
    assert other.raw == light.raw
    assert other.fields == light.fields
    assert other.fields["disable-model-invocation"] == "true"  # a light string
    assert parse_frontmatter("# T\nno block\n", mode="no-such-mode") is None


def test_importing_markdown_heuristics_imports_no_binding():
    """A fresh interpreter that CAN import bootstrap_lib does not, and does not
    import skills_kit_lib.material, when it imports markdown_heuristics."""
    code = (
        "import importlib.util, json, sys\n"
        "import skills_kit_lib.markdown_heuristics\n"
        "loaded = sorted(m for m in sys.modules if m == 'bootstrap_lib'"
        " or m.startswith('bootstrap_lib.') or m == 'skills_kit_lib.material')\n"
        "findable = importlib.util.find_spec('bootstrap_lib') is not None\n"
        "print(json.dumps({'loaded': loaded, 'findable': findable}))\n"
    )
    env = dict(os.environ)
    paths = [str(REPO_ROOT / "plugins" / "skills-kit"), str(REPO_ROOT / "plugins" / "bootstrap")]
    if env.get("PYTHONPATH"):
        paths.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(paths)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, env=env, cwd=str(REPO_ROOT), timeout=120,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report["findable"] is True  # the absence below is not a path accident
    assert report["loaded"] == []


def test_light_and_full_run_without_bootstrap_lib(monkeypatch):
    monkeypatch.setitem(sys.modules, "bootstrap_lib", None)
    monkeypatch.setitem(sys.modules, "bootstrap_lib.skill_material", None)
    light = parse_frontmatter(DOC)
    full = parse_frontmatter(DOC, mode="full")
    assert light is not None and light.fields["name"] == "my-skill"
    assert full is not None and full.fields["disable-model-invocation"] is True
    assert parse_frontmatter(NO_BLOCK) is None
    assert parse_frontmatter(INVALID_YAML, mode="full").fields == {}
    # Control: the block is in force, so strict cannot reach the library.
    with pytest.raises(material.SkillMaterialUnavailable) as excinfo:
        parse_frontmatter(DOC, mode="strict")
    assert excinfo.value.state == "absent"


def test_corpus_still_degrades_with_yaml_present(tmp_path):
    """corpus.parse_skill_md reads frontmatter in full mode. With PyYAML
    present, invalid frontmatter still degrades to empty fields; it does not
    raise as strict mode would. (tests/skills-kit/test_corpus.py covers the
    PyYAML-absent path, which never calls the parser.)"""
    from skills_kit_lib import corpus

    assert corpus.HAVE_YAML
    skill = tmp_path / "y"
    skill.mkdir()
    path = skill / "SKILL.md"
    path.write_text(INVALID_YAML, encoding="utf-8")
    record = corpus.parse_skill_md(path)
    assert record is not None and record.frontmatter == {}


def test_frontmatter_pattern_equals_the_strict_readers():
    assert FRONTMATTER_RE.pattern == real.FRONTMATTER_RE.pattern
    assert FRONTMATTER_RE.flags == real.FRONTMATTER_RE.flags


# ---------------------------------------------------------------------------
# strict mode, against the REAL library
# ---------------------------------------------------------------------------


def test_strict_returns_frontmatter_record():
    fm = parse_frontmatter(DOC, mode="strict")
    assert isinstance(fm, Frontmatter)
    assert fm.fields == {
        "name": "my-skill",
        "description": "Does a thing.",
        "disable-model-invocation": True,
    }
    assert fm.raw == (
        'name: my-skill\ndescription: "Does a thing."\ndisable-model-invocation: true'
    )


def test_strict_missing_frontmatter_raises():
    with pytest.raises(StrictFrontmatterError, match="no frontmatter block"):
        parse_frontmatter(NO_BLOCK, mode="strict")


def test_strict_invalid_yaml_raises():
    with pytest.raises(StrictFrontmatterError, match="not valid YAML"):
        parse_frontmatter(INVALID_YAML, mode="strict")


def test_strict_non_mapping_raises():
    with pytest.raises(StrictFrontmatterError, match="must be a YAML mapping"):
        parse_frontmatter(NON_MAPPING, mode="strict")


def test_strict_error_is_strict_frontmatter_error_with_cause():
    assert issubclass(StrictFrontmatterError, ValueError)
    with pytest.raises(StrictFrontmatterError) as excinfo:
        parse_frontmatter(INVALID_YAML, mode="strict")
    cause = excinfo.value.__cause__
    assert isinstance(cause, real.FrontmatterError)
    assert str(excinfo.value) == str(cause)


def test_strict_without_yaml_is_unavailable_not_strict_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "yaml", None)
    with pytest.raises(material.SkillMaterialUnavailable) as excinfo:
        parse_frontmatter(DOC, mode="strict")
    assert not isinstance(excinfo.value, StrictFrontmatterError)
    assert excinfo.value.state == "no-pyyaml"
    assert isinstance(excinfo.value.__cause__, real.PyYamlUnavailableError)
    assert "PyYAML" in str(excinfo.value)
    assert "skills_kit_tool.py" in str(excinfo.value)


def test_real_library_passes_the_probe():
    assert material.load_skill_material() is real


def test_strict_and_full_agree_on_every_repo_skill():
    """The strict reader and full mode read the same name and description from
    every repo skill. The text is decoded as strict UTF-8 with a leading BOM
    removed, which is what the strict reader expects."""
    assert REPO_SKILLS
    for path in REPO_SKILLS:
        text = path.read_bytes().decode("utf-8")
        if text.startswith("\ufeff"):
            text = text[1:]
        strict = parse_frontmatter(text, mode="strict")
        full = parse_frontmatter(text, mode="full")
        rel = path.relative_to(REPO_ROOT).as_posix()
        assert isinstance(strict.fields.get("name"), str) and strict.fields["name"], rel
        for key in ("name", "description"):
            assert strict.fields.get(key) == full.fields.get(key), (rel, key)


# ---------------------------------------------------------------------------
# The binding's runtime states (fake modules)
# ---------------------------------------------------------------------------

_MISSING = object()
_PROBED = (
    "SUPPORTED_REPORT_SCHEMAS",
    "SkillMaterialError",
    "PyYamlUnavailableError",
    "FrontmatterError",
    "parse_frontmatter_strict",
    "SkillSelection",
    "materialize",
    "SkillMaterialReport",
)


def _install_fake(monkeypatch, **overrides):
    """Install a fake bootstrap_lib package whose skill_material carries the
    real module's probed attributes, with `overrides` applied."""
    fake = types.ModuleType("bootstrap_lib.skill_material")
    for name in _PROBED:
        setattr(fake, name, getattr(real, name))
    for name, value in overrides.items():
        if value is _MISSING:
            delattr(fake, name)
        else:
            setattr(fake, name, value)
    package = types.ModuleType("bootstrap_lib")
    package.__path__ = []
    package.skill_material = fake
    monkeypatch.setitem(sys.modules, "bootstrap_lib", package)
    monkeypatch.setitem(sys.modules, "bootstrap_lib.skill_material", fake)
    return fake


def _too_old(monkeypatch, **overrides):
    _install_fake(monkeypatch, **overrides)
    with pytest.raises(material.SkillMaterialUnavailable) as excinfo:
        material.load_skill_material()
    assert excinfo.value.state == "too-old"
    assert UPDATE in str(excinfo.value)
    return excinfo.value


def _absent_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "bootstrap_lib", None)
    with pytest.raises(material.SkillMaterialUnavailable) as excinfo:
        parse_frontmatter(DOC, mode="strict")
    return excinfo.value


def _module_missing_error(monkeypatch):
    package = types.ModuleType("bootstrap_lib")
    package.__path__ = []  # an older bootstrap_lib: no skill_material in it
    monkeypatch.setitem(sys.modules, "bootstrap_lib", package)
    monkeypatch.delitem(sys.modules, "bootstrap_lib.skill_material", raising=False)
    with pytest.raises(material.SkillMaterialUnavailable) as excinfo:
        parse_frontmatter(DOC, mode="strict")
    return excinfo.value


def test_unaltered_fake_passes_the_probe(monkeypatch):
    """Control for the too-old rows: each of them differs from this fake in
    one attribute only, so each refusal is that attribute's."""
    fake = _install_fake(monkeypatch)
    assert material.load_skill_material() is fake


def test_strict_without_bootstrap_lib_is_state_absent(monkeypatch):
    error = _absent_error(monkeypatch)
    assert isinstance(error, ImportError)
    assert error.state == "absent"
    assert INSTALL in str(error)


def test_strict_with_old_bootstrap_is_state_too_old_naming_update(monkeypatch):
    error = _module_missing_error(monkeypatch)
    assert error.state == "too-old"
    assert UPDATE in str(error)
    assert material.SKILL_MATERIAL_BOOTSTRAP in str(error)


def test_marker_without_schema_is_too_old(monkeypatch):
    _too_old(monkeypatch, SUPPORTED_REPORT_SCHEMAS=frozenset({"some-other/v9"}))


def test_missing_strict_parser_is_too_old(monkeypatch):
    _too_old(monkeypatch, parse_frontmatter_strict=_MISSING)


def test_missing_from_json_is_too_old(monkeypatch):
    class OldSelection:  # a SkillSelection without from_json
        pass

    _too_old(monkeypatch, SkillSelection=OldSelection)


def test_materialize_without_base_dir_is_too_old(monkeypatch):
    def materialize(selection):  # no base_dir keyword
        raise AssertionError("never called")

    _too_old(monkeypatch, materialize=materialize)


@pytest.mark.parametrize("name", ["SkillMaterialError", "PyYamlUnavailableError"])
def test_missing_exception_class_is_too_old(monkeypatch, name):
    _too_old(monkeypatch, **{name: _MISSING})


def _strict_no_parameter():
    raise AssertionError("never called")


def _strict_two_required(content, extra):
    raise AssertionError("never called")


@pytest.mark.parametrize("strict", [_strict_no_parameter, _strict_two_required])
def test_strict_parser_with_incompatible_signature_is_too_old(monkeypatch, strict):
    _too_old(monkeypatch, parse_frontmatter_strict=strict)


class _SelectionNoMapping:
    @classmethod
    def from_json(cls):
        raise AssertionError("never called")


class _SelectionTwoRequired:
    @classmethod
    def from_json(cls, mapping, extra):
        raise AssertionError("never called")


@pytest.mark.parametrize("selection", [_SelectionNoMapping, _SelectionTwoRequired])
def test_from_json_with_incompatible_signature_is_too_old(monkeypatch, selection):
    _too_old(monkeypatch, SkillSelection=selection)


class _ReportStaticToJson:
    @staticmethod
    def to_json():  # does not bind as to_json(self)
        raise AssertionError("never called")


class _ReportToJsonTwoRequired:
    def to_json(self, extra):
        raise AssertionError("never called")


@pytest.mark.parametrize("report", [_ReportStaticToJson, _ReportToJsonTwoRequired])
def test_report_to_json_with_incompatible_signature_is_too_old(monkeypatch, report):
    _too_old(monkeypatch, SkillMaterialReport=report)


def test_absent_and_too_old_messages_differ_and_name_version(monkeypatch):
    absent = str(_absent_error(monkeypatch))
    monkeypatch.undo()
    too_old = str(_module_missing_error(monkeypatch))
    assert absent != too_old
    assert INSTALL in absent and UPDATE not in absent
    assert UPDATE in too_old and INSTALL not in too_old
    assert f"bootstrap >= {material.SKILL_MATERIAL_BOOTSTRAP}" in too_old
    for message in (absent, too_old):
        assert "bootstrap.json" not in message  # never a manifest as the remedy


def test_skill_material_bootstrap_not_above_repo_version():
    repo_version = json.loads(BOOTSTRAP_PLUGIN_JSON.read_text(encoding="utf-8"))["version"]
    assert _version(material.SKILL_MATERIAL_BOOTSTRAP) <= _version(repo_version)
