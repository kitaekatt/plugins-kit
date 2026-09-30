"""Skill context at the seam: the records, the adapter function and the binding.

``llm_scripting_kit.completion.skill_context`` binds
``bootstrap_lib.skill_material`` (bootstrap's shared library). These tests run
against the REAL library from the dev tree (the root pytest ``pythonpath``
puts ``plugins/bootstrap`` first). A fake module appears only in the absent and
too-old rows, and each of those asserts the ``state`` and the remedy command.
"""
from __future__ import annotations

import ast
import dataclasses
import hashlib
import importlib.abc
import inspect
import json
import os
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

import bootstrap_lib
import bootstrap_lib.skill_material as real_library
from llm_scripting_kit.completion import skill_context as sc
from llm_scripting_kit.completion import skill_context_types
from llm_scripting_kit.completion.skill_context import (
    SKILL_MATERIAL_BOOTSTRAP,
    materialize_skill_context,
    skill_context_from,
)
from llm_scripting_kit.completion.skill_context_types import (
    SkillContext,
    SkillContextError,
    SkillContextReport,
    SkillContextSupportError,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
LSK_LIB = REPO_ROOT / "plugins" / "llm-scripting-kit" / "lib"
LIBRARY = "bootstrap_lib.skill_material"
INSTALL = "claude plugin install bootstrap@plugins-kit"
UPDATE = "claude plugin update bootstrap@plugins-kit"
REPORT_SCHEMA = "plugins-kit.skill-material-report/v1"

_PROBED = (
    "SUPPORTED_REPORT_SCHEMAS",
    "SkillSelection",
    "materialize",
    "SkillMaterialReport",
    "SkillMaterialError",
    "PyYamlUnavailableError",
)
_DELETE = object()


# -- helpers -----------------------------------------------------------------


def _write_skill(root, name="alpha", *, body="Follow the alpha procedure.",
                 description=None, declared=(), files=None, frontmatter=None):
    """Write ``root/name/SKILL.md`` (LF, UTF-8) and any resource files."""
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    if frontmatter is None:
        desc = description if description is not None else f"The {name} skill."
        frontmatter = f"name: {name}\ndescription: {desc}\n"
    text = f"---\n{frontmatter}---\n{body}\n"
    if declared:
        text += "\n```yaml\nreferences:\n"
        text += "".join(f"  - path: {path}\n" for path in declared)
        text += "```\n"
    (skill_dir / "SKILL.md").write_bytes(text.encode("utf-8"))
    for rel, content in (files or {}).items():
        target = skill_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode("utf-8"))
    return skill_dir


def _selection(*skill_dirs, budget=100_000):
    return {"skills": [{"path": str(d)} for d in skill_dirs], "token_budget": budget}


def _fake_library(**overrides):
    """A module carrying the real library's probed names, with overrides."""
    module = types.ModuleType(LIBRARY)
    for name in _PROBED:
        setattr(module, name, getattr(real_library, name))
    for name, value in overrides.items():
        if value is _DELETE:
            delattr(module, name)
        else:
            setattr(module, name, value)
    return module


def _install_library(monkeypatch, module):
    """Make ``import bootstrap_lib.skill_material`` yield ``module``.

    ``import a.b as m`` reads the package attribute first and ``sys.modules``
    second, so both are patched. ``None`` makes the import raise
    ``ModuleNotFoundError`` naming the module.
    """
    monkeypatch.setitem(sys.modules, LIBRARY, module)
    if module is None:
        monkeypatch.delattr(bootstrap_lib, "skill_material", raising=False)
    else:
        monkeypatch.setattr(bootstrap_lib, "skill_material", module, raising=False)


def _too_old_via(monkeypatch, tmp_path, module):
    _install_library(monkeypatch, module)
    skill = _write_skill(tmp_path)
    with pytest.raises(SkillContextSupportError) as info:
        materialize_skill_context(_selection(skill))
    return info.value


def _assert_too_old(error):
    assert error.state == "too-old"
    assert UPDATE in str(error)
    assert SKILL_MATERIAL_BOOTSTRAP in str(error)


class _Report:
    """A library-shaped report whose ``to_json`` result the test chooses."""

    schema = REPORT_SCHEMA
    format_version = "1"
    skills = ()
    suppressed = ()
    estimated_tokens = 1
    token_budget = 10
    token_estimate = "chars/4"
    digest = hashlib.sha256(b"x").hexdigest()

    def __init__(self, document=None, error=None):
        self._document = document if document is not None else {}
        self._error = error

    def to_json(self):
        if self._error is not None:
            raise self._error
        return self._document


def _materialized(report):
    return SimpleNamespace(text="x", report=report)


# -- records -------------------------------------------------------------------


def test_skill_context_types_is_a_leaf_module():
    """skill_context_types imports the stdlib only -- never .types (which
    imports IT at runtime) and never bootstrap_lib (so importing
    llm_scripting_kit never needs the library)."""
    source = Path(inspect.getsourcefile(skill_context_types)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    relative = set()
    absolute = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                relative.add(node.module or "")
            else:
                absolute.add((node.module or "").split(".")[0])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                absolute.add(alias.name.split(".")[0])
    assert relative == set(), relative
    assert "bootstrap_lib" not in absolute
    assert "llm_scripting_kit" not in absolute
    stdlib = set(sys.stdlib_module_names) | {"__future__"}
    assert absolute <= stdlib, absolute - stdlib


def test_llm_scripting_kit_imports_without_skill_material_or_yaml():
    """A fresh interpreter with the library AND PyYAML blocked still imports
    the package, the completion seam and the request protocol."""
    code = "\n".join(
        [
            "import sys",
            f"sys.path[:0] = [{str(LSK_LIB)!r}, {str(REPO_ROOT / 'plugins' / 'bootstrap')!r}]",
            "sys.modules['bootstrap_lib.skill_material'] = None",
            "sys.modules['yaml'] = None",
            "import llm_scripting_kit",
            "import llm_scripting_kit.completion as completion",
            "import llm_scripting_kit.request_protocol",
            "assert completion.BackendOptions().skill_context is None",
            "print('imported')",
        ]
    )
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=120
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "imported"


def test_report_asdict_is_json_serializable(tmp_path):
    context = materialize_skill_context(_selection(_write_skill(tmp_path)))
    delivered = dataclasses.replace(
        context.report, adapter="openrouter", delivery="system-message", emits="e"
    )
    for report in (context.report, delivered):
        json.dumps(dataclasses.asdict(report))
        json.dumps(report.to_json())
    assert delivered.to_json()["adapter"] == "openrouter"


# -- the adapter function (REAL library) ---------------------------------------


def test_skill_context_from_copies_every_summary_field(tmp_path):
    selection = real_library.SkillSelection.from_json(
        _selection(_write_skill(tmp_path, "alpha"), _write_skill(tmp_path, "beta"))
    )
    materialized = real_library.materialize(selection)
    context = skill_context_from(materialized)
    report = materialized.report
    assert context.text == materialized.text
    assert context.report.resolver == "bootstrap_lib.skill_material"
    assert context.report.report_schema == report.schema
    assert context.report.format_version == report.format_version
    assert context.report.digest == report.digest
    assert context.report.estimated_tokens == report.estimated_tokens
    assert context.report.token_budget == report.token_budget
    assert context.report.token_estimate == report.token_estimate
    assert context.report.skills == len(report.skills) == 2
    assert (context.report.adapter, context.report.delivery, context.report.emits) == (
        None, None, None,
    )


def test_provenance_equals_library_report_to_json(tmp_path):
    selection = real_library.SkillSelection.from_json(_selection(_write_skill(tmp_path)))
    materialized = real_library.materialize(selection)
    context = skill_context_from(materialized)
    assert context.report.provenance == materialized.report.to_json()
    assert context.report.provenance["schema"] == REPORT_SCHEMA


def test_lsk_report_matches_library_report(tmp_path):
    skill = _write_skill(tmp_path)
    context = materialize_skill_context(_selection(skill))
    direct = real_library.materialize(
        real_library.SkillSelection.from_json(_selection(skill))
    )
    assert context.text == direct.text
    assert context.report.digest == direct.report.digest
    assert context.report.digest == hashlib.sha256(context.text.encode("utf-8")).hexdigest()
    assert context.report.estimated_tokens == direct.report.estimated_tokens


def test_unknown_report_schema_refused():
    report = _Report()
    report.schema = "plugins-kit.skill-material-report/v9"
    with pytest.raises(SkillContextSupportError) as info:
        skill_context_from(_materialized(report))
    _assert_too_old(info.value)
    assert "v9" in str(info.value)


def test_skewed_library_object_raises_too_old():
    report = _Report()
    skewed = SimpleNamespace(
        schema=report.schema, format_version="1", skills=(), estimated_tokens=1,
        token_budget=10, token_estimate="chars/4", to_json=lambda: {},
    )  # no ``digest``
    with pytest.raises(SkillContextSupportError) as info:
        skill_context_from(_materialized(skewed))
    _assert_too_old(info.value)


def test_library_refusal_is_skill_context_error_with_cause(tmp_path):
    skill = _write_skill(tmp_path, frontmatter="name: [unclosed\n")
    with pytest.raises(SkillContextError) as info:
        materialize_skill_context(_selection(skill))
    assert not isinstance(info.value, SkillContextSupportError)
    assert isinstance(info.value.__cause__, real_library.SkillMaterialError)
    assert str(info.value) == str(info.value.__cause__)


def test_mapping_selection_is_built_by_the_library(tmp_path):
    mapping = _selection(_write_skill(tmp_path))
    mapping["unexpected"] = 1
    with pytest.raises(SkillContextError) as info:
        materialize_skill_context(mapping)
    with pytest.raises(real_library.SkillMaterialError) as direct:
        real_library.SkillSelection.from_json(mapping)
    assert str(info.value) == str(direct.value)
    # A library SkillSelection object is accepted as it is.
    del mapping["unexpected"]
    built = real_library.SkillSelection.from_json(mapping)
    assert materialize_skill_context(built).report.skills == 1


def test_full_ref_delivers_declared_resources(tmp_path):
    skill = _write_skill(
        tmp_path,
        declared=["references/detail.md"],
        files={"references/detail.md": "Declared detail text."},
    )
    context = materialize_skill_context(_selection(skill))
    assert '<resource path="references/detail.md">' in context.text
    assert "Declared detail text." in context.text
    rendered = context.report.provenance["skills"][0]["resources"]
    assert [r["path"] for r in rendered] == ["references/detail.md"]
    assert rendered[0]["declared"] is True


# -- the binding: absent and too old -------------------------------------------


def test_absent_bootstrap_lib_is_state_absent_naming_install(monkeypatch, tmp_path):
    skill = _write_skill(tmp_path)
    monkeypatch.setitem(sys.modules, "bootstrap_lib", None)
    with pytest.raises(SkillContextSupportError) as info:
        materialize_skill_context(_selection(skill))
    assert info.value.state == "absent"
    assert INSTALL in str(info.value)
    assert "system" in str(info.value)  # the alternative: your own system text


def test_missing_module_is_state_too_old_naming_update(monkeypatch, tmp_path):
    error = _too_old_via(monkeypatch, tmp_path, None)
    _assert_too_old(error)
    assert INSTALL not in str(error)


def test_marker_without_schema_is_too_old(monkeypatch, tmp_path):
    fake = _fake_library(SUPPORTED_REPORT_SCHEMAS=frozenset({"some-other/v1"}))
    _assert_too_old(_too_old_via(monkeypatch, tmp_path, fake))


def test_missing_from_json_is_too_old(monkeypatch, tmp_path):
    class SelectionWithoutFromJson:
        pass

    fake = _fake_library(SkillSelection=SelectionWithoutFromJson)
    _assert_too_old(_too_old_via(monkeypatch, tmp_path, fake))


class _NoParameter:
    @staticmethod
    def from_json():
        raise AssertionError("never called")


class _TwoRequired:
    @staticmethod
    def from_json(mapping, strict):
        raise AssertionError("never called")


@pytest.mark.parametrize("selection_class", [_NoParameter, _TwoRequired])
def test_from_json_with_incompatible_signature_is_too_old(
    monkeypatch, tmp_path, selection_class
):
    fake = _fake_library(SkillSelection=selection_class)
    _assert_too_old(_too_old_via(monkeypatch, tmp_path, fake))


def test_materialize_without_base_dir_is_too_old(monkeypatch, tmp_path):
    def materialize(selection):
        raise AssertionError("never called")

    fake = _fake_library(materialize=materialize)
    _assert_too_old(_too_old_via(monkeypatch, tmp_path, fake))


def test_missing_exception_class_is_too_old(monkeypatch, tmp_path):
    fake = _fake_library(PyYamlUnavailableError=_DELETE)
    _assert_too_old(_too_old_via(monkeypatch, tmp_path, fake))


class _ReportWithoutToJson:
    pass


class _ReportToJsonNeedsMore:
    def to_json(self, extra):
        raise AssertionError("never called")


@pytest.mark.parametrize("report_class", [_ReportWithoutToJson, _ReportToJsonNeedsMore])
def test_report_to_json_with_incompatible_signature_is_too_old(
    monkeypatch, tmp_path, report_class
):
    fake = _fake_library(SkillMaterialReport=report_class)
    _assert_too_old(_too_old_via(monkeypatch, tmp_path, fake))


def test_to_json_raising_type_error_at_the_call_is_too_old():
    report = _Report(error=TypeError("to_json() missing 1 required argument"))
    with pytest.raises(SkillContextSupportError) as info:
        skill_context_from(_materialized(report))
    _assert_too_old(info.value)


def test_to_json_returning_a_non_mapping_is_too_old():
    report = _Report()
    report.to_json = lambda: [["schema", REPORT_SCHEMA]]
    with pytest.raises(SkillContextSupportError) as info:
        skill_context_from(_materialized(report))
    _assert_too_old(info.value)


@pytest.mark.parametrize(
    "document",
    [
        {"bad": object()},
        {"skills": [{"resources": [{"x": object()}]}]},
        {"skills": ({"name": "alpha"},)},
        {"skills": [{1: "non-string key"}]},
    ],
    ids=["top-level-object", "nested-object", "tuple", "non-string-key"],
)
def test_to_json_with_a_non_json_native_value_is_too_old(document):
    with pytest.raises(SkillContextSupportError) as info:
        skill_context_from(_materialized(_Report(document)))
    _assert_too_old(info.value)


def test_pyyaml_absent_is_support_error_state_no_pyyaml(monkeypatch, tmp_path):
    skill = _write_skill(tmp_path)
    monkeypatch.setitem(sys.modules, "yaml", None)
    with pytest.raises(SkillContextSupportError) as info:
        materialize_skill_context(_selection(skill))
    assert type(info.value) is SkillContextSupportError
    assert info.value.state == "no-pyyaml"
    assert "PyYAML" in str(info.value)
    assert isinstance(info.value.__cause__, real_library.PyYamlUnavailableError)
    assert UPDATE not in str(info.value) and INSTALL not in str(info.value)


def test_real_library_passes_the_probe():
    assert sc._skill_material() is real_library


def test_absent_and_too_old_messages_differ():
    assert sc._ABSENT_MESSAGE != sc._TOO_OLD_MESSAGE
    assert INSTALL in sc._ABSENT_MESSAGE and UPDATE not in sc._ABSENT_MESSAGE
    assert UPDATE in sc._TOO_OLD_MESSAGE and INSTALL not in sc._TOO_OLD_MESSAGE
    for message in (sc._ABSENT_MESSAGE, sc._TOO_OLD_MESSAGE, sc._NO_PYYAML_MESSAGE):
        assert "Nothing was dispatched" in message


def test_too_old_message_names_skill_material_bootstrap():
    assert f"bootstrap >= {SKILL_MATERIAL_BOOTSTRAP}" in sc._TOO_OLD_MESSAGE
    assert "left behind by an uninstall" in sc._TOO_OLD_MESSAGE


def _version(text):
    return tuple(int(part) for part in text.split("."))


def test_skill_material_bootstrap_not_above_repo_version():
    manifest = REPO_ROOT / "plugins" / "bootstrap" / ".claude-plugin" / "plugin.json"
    repo_version = json.loads(manifest.read_text(encoding="utf-8"))["version"]
    assert _version(SKILL_MATERIAL_BOOTSTRAP) <= _version(repo_version)


# -- remaining rows --------------------------------------------------------------


class _SyntaxErrorFinder(importlib.abc.MetaPathFinder):
    """A half-synced copy: finding the module raises SyntaxError."""

    def find_spec(self, fullname, path, target=None):
        if fullname == LIBRARY:
            raise SyntaxError("invalid syntax (skill_material.py, line 1)")
        return None


def test_syntax_error_in_library_propagates(monkeypatch, tmp_path):
    skill = _write_skill(tmp_path)
    monkeypatch.delitem(sys.modules, LIBRARY)
    monkeypatch.delattr(bootstrap_lib, "skill_material", raising=False)
    monkeypatch.setattr(sys, "meta_path", [_SyntaxErrorFinder()] + sys.meta_path)
    with pytest.raises(SyntaxError):
        materialize_skill_context(_selection(skill))


def test_support_messages_name_no_manifest_file():
    for message in (sc._ABSENT_MESSAGE, sc._TOO_OLD_MESSAGE, sc._NO_PYYAML_MESSAGE):
        for manifest in ("bootstrap.json", "plugin.json", "pyproject.toml", "shared_lib_imports"):
            assert manifest not in message, (manifest, message)


def test_llm_scripting_kit_lib_never_imports_skills_kit_lib():
    offenders = []
    for path in sorted(LSK_LIB.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.ImportFrom) and not node.level:
                names = [node.module or ""]
            elif isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            if any(name.split(".")[0] == "skills_kit_lib" for name in names):
                offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == []


def test_skill_context_rejects_non_str_text():
    report = SkillContextReport(
        resolver="r", report_schema=REPORT_SCHEMA, format_version="1", digest="d",
        estimated_tokens=1, token_budget=1, token_estimate="chars/4", skills=0,
        provenance={},
    )
    with pytest.raises(TypeError):
        SkillContext(text=b"bytes", report=report)
    with pytest.raises(TypeError):
        SkillContext(text="x", report={"digest": "d"})
