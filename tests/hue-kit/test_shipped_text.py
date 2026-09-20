"""Shipped hue-kit text must not cite dead references, dated authorship
decisions, or a stale PATH claim, and a handful of facts it states (the
README dependency list, the devicetype it prints, the bootstrap floor, the
CLI verb list, and the `groups`/`--force` guard) must match the code."""

import ast
import json
import re
import tomllib
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_PLUGIN = _REPO_ROOT / "plugins" / "hue-kit"

# examples/index.html embeds a frozen copy of the scripts + config as a
# worked example; it is excluded from every text-content check below.
_EXCLUDED = {_PLUGIN / "examples" / "index.html"}

# Author-decision dates are the shape "Christina[,]? YYYY-MM-DD" or
# "(Christina, YYYY-MM-DD)" -- a citation naming who decided something and
# when, which goes stale the moment the decision stops being fresh. Plain
# attribution (a README provenance line, a plugin.json/SKILL.md author
# field) carries no date and is not one of these.
_DATED_AUTHOR_RE = re.compile(r"Christina[,]?\s*20\d\d-\d\d-\d\d")


def _text_files():
    for path in sorted(_PLUGIN.rglob("*")):
        if not path.is_file() or path in _EXCLUDED:
            continue
        if "__pycache__" in path.parts:
            continue
        try:
            yield path, path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue


@pytest.mark.parametrize("needle", ["naming-conventions.md", "scene-schema"])
def test_no_dangling_file_reference(needle):
    hits = [str(p.relative_to(_REPO_ROOT)) for p, text in _text_files()
            if needle in text]
    assert hits == [], f"{needle!r} still cited in: {hits}"


def test_no_dated_author_decision():
    hits = [(str(p.relative_to(_REPO_ROOT)), m.group(0))
            for p, text in _text_files()
            for m in _DATED_AUTHOR_RE.finditer(text)]
    assert hits == [], f"dated author decision still cited: {hits}"


def test_no_retraction_history():
    hits = [str(p.relative_to(_REPO_ROOT)) for p, text in _text_files()
            if "corrected 2026" in text]
    assert hits == [], f"dated retraction note still present in: {hits}"


def test_no_reference_cites_skill_md():
    """A reference doc pointing back at its own skill's SKILL.md is a
    back-reference from the general into the specific; state the rule
    directly in the reference instead."""
    refs_dir = _PLUGIN / "skills" / "hue-domain" / "references"
    hits = [str(p.relative_to(_REPO_ROOT)) for p in sorted(refs_dir.glob("*.md"))
            if "SKILL.md" in p.read_text(encoding="utf-8")]
    assert hits == []


def _pyproject_dependency_names() -> list[str]:
    data = tomllib.loads((_PLUGIN / "pyproject.toml").read_text(encoding="utf-8"))
    names = []
    for dep in data["project"]["dependencies"]:
        # strip any version specifier / marker; these entries are bare names
        name = re.split(r"[<>=!~; ]", dep.strip(), 1)[0]
        names.append(name.lower())
    return sorted(names)


def test_readme_dependency_list_matches_pyproject():
    readme = (_PLUGIN / "README.md").read_text(encoding="utf-8")
    expected = _pyproject_dependency_names()
    # pyproject's dependency is "pyyaml"; README (and the venv it describes)
    # spells the import/package colloquially as "pyyaml" too.
    found = [name for name in expected if name in readme.lower()]
    assert found == expected, (
        f"README is missing dependency name(s): "
        f"{sorted(set(expected) - set(found))}")


def _cli_devicetype_constant() -> str:
    src = (_PLUGIN / "scripts" / "hue_kit_cli.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if (isinstance(k, ast.Constant) and k.value == "devicetype"
                        and isinstance(v, ast.Constant)):
                    return v.value
    raise AssertionError("no devicetype literal found in hue_kit_cli.py")


def test_readme_devicetype_matches_cli_constant():
    readme = (_PLUGIN / "README.md").read_text(encoding="utf-8")
    constant = _cli_devicetype_constant()
    assert f'"devicetype":"{constant}"' in readme, (
        f"README's curl example devicetype does not match the CLI constant "
        f"{constant!r}")


def test_bootstrap_json_requires_bootstrap_floor():
    data = json.loads((_PLUGIN / "bootstrap.json").read_text(encoding="utf-8"))
    version = tuple(int(x) for x in data["requires_bootstrap"].split("."))
    assert version >= (0, 72, 0), (
        f"requires_bootstrap {data['requires_bootstrap']} is below the "
        "0.72.0 floor (the release that added <PLUGIN>_ROOT)")


def _cli_subparser_verbs() -> list[str]:
    """Every verb `hue_kit_cli.py`'s argparse registers, derived from the
    source rather than imported (importing re-execs under the plugin venv)."""
    src = (_PLUGIN / "scripts" / "hue_kit_cli.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    verbs = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_parser"
                and node.args
                and isinstance(node.args[0], ast.Constant)):
            verbs.append(node.args[0].value)
    assert verbs, "no add_parser calls found -- parser derivation is broken"
    return verbs


def test_claude_md_verb_list_names_every_subparser():
    claude_md = (_PLUGIN / "CLAUDE.md").read_text(encoding="utf-8")
    verbs = _cli_subparser_verbs()
    missing = [v for v in verbs if not re.search(rf"\b{re.escape(v)}\b", claude_md)]
    assert missing == [], f"plugin CLAUDE.md never names verb(s): {missing}"


def test_skill_md_groups_operation_mentions_force():
    skill_md = (_PLUGIN / "skills" / "hue-domain" / "SKILL.md").read_text(
        encoding="utf-8")
    # Find the `groups` capability entry's description block, up to the next
    # "- id:" entry or the end of the capabilities list.
    m = re.search(r"- id: groups\b.*?(?=\n {4}- id:|\Z)", skill_md, re.DOTALL)
    assert m is not None, "SKILL.md has no `groups` capability entry"
    assert "--force" in m.group(0), (
        "SKILL.md's groups operation does not mention --force")


def test_scene_layers_md_groups_line_mentions_force():
    doc = (_PLUGIN / "skills" / "hue-domain" / "references"
           / "scene-layers.md").read_text(encoding="utf-8")
    m = re.search(r"`hue-kit groups.*?(?=\n- |\Z)", doc, re.DOTALL)
    assert m is not None, "scene-layers.md has no `hue-kit groups` line"
    assert "--force" in m.group(0)
