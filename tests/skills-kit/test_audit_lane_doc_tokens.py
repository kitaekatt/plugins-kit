"""Pinning: audit-lane.md names the values the code accepts, so the document
cannot drift from the scripts it tells an agent to run.

- the `--primitive` list matches what `resolve_standards.py` accepts;
- the `--sources` flag and its default match `references_audit.py`;
- the `<venvPython>` locator names the path bootstrap provisions;
- the nested-`scriptPath` rule and the `maxConcurrent` argument are stated.
"""

import importlib.util
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN = REPO_ROOT / "plugins" / "skills-kit"
MD_DOMAIN = PLUGIN / "skills" / "md-domain"
AUDIT_LANE = MD_DOMAIN / "references" / "lanes" / "audit-lane.md"

if str(PLUGIN) not in sys.path:
    sys.path.insert(0, str(PLUGIN))

from skills_kit_lib import standards_resolve  # noqa: E402


def _doc() -> str:
    return AUDIT_LANE.read_text(encoding="utf-8")


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _backticked(text: str) -> list[str]:
    return re.findall(r"`([^`]+)`", text)


def test_primitive_list_matches_what_resolve_standards_accepts():
    m = re.search(r"^Valid `--primitive` values: (.+)\.[ \t]*\r?$", _doc(), re.M)
    assert m, "audit-lane.md no longer lists the valid `--primitive` values"
    documented = set(_backticked(m.group(1)))
    script = _load("resolve_standards_mod", PLUGIN / "scripts" / "resolve_standards.py")
    assert documented == set(script._PRIMITIVE_CHOICES)
    expected = {
        s for s in standards_resolve.APPLIES_TO_SUBJECTS
        if s not in standards_resolve.APPLIES_TO_NOT_CONSUMED
    }
    assert documented == expected


def test_rejected_composition_ids_are_named_as_invalid():
    doc = _doc()
    for name in (*standards_resolve.APPLIES_TO_NOT_CONSUMED,
                 *standards_resolve.APPLIES_TO_ALIASES):
        assert f"`{name}`" in doc, f"{name} is rejected by --primitive but unnamed"


def test_sources_flag_and_default_match_the_scanner():
    ra = _load("references_audit_doc_mod",
               MD_DOMAIN / "scripts" / "references_audit.py")
    doc = " ".join(_doc().split())
    assert f"`--sources {'|'.join(ra.SOURCES_CHOICES)}`" in doc
    default = ra.SOURCES_CHOICES[0]
    assert f"the default, `{default}`," in doc
    parser_default = re.search(
        r'"--sources",.*?default="([a-z]+)"',
        (MD_DOMAIN / "scripts" / "references_audit.py").read_text(encoding="utf-8"),
        re.S,
    )
    assert parser_default and parser_default.group(1) == default


def test_venv_python_locator_names_the_provisioned_path():
    doc = _doc()
    manifest = json.loads((PLUGIN / "bootstrap.json").read_text(encoding="utf-8"))
    assert manifest["plugins"][0]["ref"] == "plugins-kit:skills-kit"
    base = '"${CLAUDE_BOOTSTRAP_DATA_ROOT:-$HOME/.claude/plugins/data}'
    assert f"{base}/plugins-kit/skills-kit/.venv/Scripts/python.exe\"" in doc
    assert f"{base}/plugins-kit/skills-kit/.venv/bin/python\"" in doc
    assert "presence check" in doc and "does not locate an interpreter" in doc


def test_nested_workflow_script_path_is_ruled_out():
    doc = " ".join(_doc().split())
    assert "nested `workflow({scriptPath})` call" in doc
    assert "its own top-level Workflow call" in doc


def test_max_concurrent_is_documented_as_an_optional_positive_integer():
    doc = " ".join(_doc().split())
    assert "`maxConcurrent`" in doc
    assert "positive integer" in doc
    assert "throws before any agent launches" in doc
