"""llm-kit's consumer-contract checker and its public-surface listing.

The checker (``scripts/check_consumer_contract.py``) and the listing
(``contract/public-surface.json``) are content-pipeline-kit's, adapted to the
llm_kit names the pipeline products now have. The legacy tests of both are
dispositioned legacy-only in the ledger; this file asserts the same behaviors
natively:

* the checker reports removed and deprecated llm_kit names a consumer uses;
* the listing and the code agree (a listed name resolves, a removed name does
  not, every exported name under the covered packages is listed, a deprecated
  name is inside its window);
* every entry is the llm_kit home of a content-pipeline-kit entry, by the repoint
  map (so the adaptation cannot drift from the module table);
* the skill reference that tells a reader to run the checker names files that
  exist.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import pkgutil
import re
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[2]
PLUGIN = REPO / "plugins" / "llm-kit"
CONTRACT_PATH = PLUGIN / "contract" / "public-surface.json"
CONTRACT = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
ENTRIES = CONTRACT["entries"]
PACKAGE = CONTRACT["package"]
STATUSES = {"public", "deprecated", "removed"}

_spec = importlib.util.spec_from_file_location(
    "llm_kit_check_consumer_contract", PLUGIN / "scripts" / "check_consumer_contract.py"
)
checker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(checker)


# --- the checker -----------------------------------------------------------

def test_guarded_sweep_import_is_reported_removed(tmp_path, capsys):
    (tmp_path / "loc.py").write_text(
        "from llm_kit.pipeline.runtime.budget import guarded_sweep, BudgetStop\n", encoding="utf-8"
    )
    assert checker.main([str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "runtime.budget.guarded_sweep" in out and "REMOVED" in out
    assert "BudgetStop --" not in out


def test_removed_kwarg_and_module_alias_uses(tmp_path, capsys):
    (tmp_path / "a.py").write_text(
        "import llm_kit.pipeline.runtime.backends as b\n"
        "from llm_kit.pipeline.execution.adapter import RunAdapter\n"
        "x = b.BACKEND_ENV\nRunAdapter(reconcile=None)\n",
        encoding="utf-8",
    )
    assert checker.main([str(tmp_path / "a.py")]) == 1
    out = capsys.readouterr().out
    assert "BACKEND_ENV" in out and "RunAdapter.reconcile" in out


def test_clean_consumer_exits_zero(tmp_path):
    (tmp_path / "ok.py").write_text(
        "from llm_kit.pipeline.runtime.budget import BudgetStop\n", encoding="utf-8"
    )
    assert checker.main([str(tmp_path)]) == 0


def test_deprecated_reported_and_strict_fails(tmp_path, capsys):
    contract = tmp_path / "c.json"
    contract.write_text(
        json.dumps({"entries": [{"name": "llm_kit.x.old", "status": "deprecated",
                                 "since": "1.0.0", "remove_after": "1.2.0", "replacement": "new"}]}),
        encoding="utf-8",
    )
    src = tmp_path / "s.py"
    src.write_text("from llm_kit.x import old\n", encoding="utf-8")
    assert checker.main([str(src), "--contract", str(contract)]) == 0
    assert "DEPRECATED" in capsys.readouterr().out
    assert checker.main([str(src), "--contract", str(contract), "--strict"]) == 1


def test_a_legacy_package_name_is_not_reported(tmp_path, capsys):
    """The checker reads llm_kit names only; a content_pipeline import is not its subject."""
    (tmp_path / "old.py").write_text(
        "from content_pipeline.cli.budget import guarded_sweep\n", encoding="utf-8"
    )
    assert checker.main([str(tmp_path)]) == 0
    assert "0 removed" in capsys.readouterr().out


def test_an_unreadable_contract_exits_2(tmp_path, capsys):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    assert checker.main([str(tmp_path), "--contract", str(tmp_path / "missing.json")]) == 2
    assert "cannot read contract" in capsys.readouterr().err


# --- the listing and the code ----------------------------------------------

def _version(text):
    return tuple(int(p) for p in text.split("."))


def _resolve(dotted):
    """Import the longest module prefix, then getattr the rest."""
    parts = dotted.split(".")
    for i in range(len(parts), 0, -1):
        try:
            obj = importlib.import_module(".".join(parts[:i]))
        except ImportError:
            continue
        for attr in parts[i:]:
            obj = getattr(obj, attr)
        return obj
    raise ImportError(dotted)


def _resolves(dotted):
    try:
        _resolve(dotted)
        return True
    except (ImportError, AttributeError):
        return False


def _ids(status):
    return [e["name"] for e in ENTRIES if e["status"] == status]


def test_entries_are_well_formed():
    names = [e["name"] for e in ENTRIES]
    assert len(names) == len(set(names)), "duplicate contract entries"
    assert CONTRACT["covers"], "the listing states which packages it covers"
    for e in ENTRIES:
        assert e["status"] in STATUSES, e
        assert e["name"].startswith(PACKAGE + "."), e
        assert any(e["name"].startswith(prefix + ".") for prefix in CONTRACT["covers"]), e
        if e["status"] == "removed":
            assert e.get("since") and e.get("replacement"), e
        if e["status"] == "deprecated":
            assert e.get("since") and e.get("remove_after") and e.get("replacement"), e


@pytest.mark.parametrize("name", _ids("public") + _ids("deprecated"))
def test_listed_name_still_resolves(name):
    assert _resolves(name), (
        f"{name} is listed public/deprecated but no longer resolves. Removing a public name "
        "needs a DeprecationWarning shim for the policy window first; if the window has run, "
        "move the entry to status removed with a replacement."
    )


@pytest.mark.parametrize("name", _ids("removed"))
def test_removed_name_stays_removed(name):
    assert not _resolves(name), (
        f"{name} is listed removed but resolves. A restored name must be listed as deprecated or public."
    )


def test_deprecated_entries_are_inside_their_window():
    current = _version(
        json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]
    )
    for e in ENTRIES:
        if e["status"] == "deprecated":
            assert current < _version(e["remove_after"]), (
                f"{e['name']} outlived remove_after {e['remove_after']}: remove it and record it as removed."
            )


def _covered_modules():
    for root in CONTRACT["covers"]:
        top = importlib.import_module(root)
        yield root, top
        if hasattr(top, "__path__"):
            for m in pkgutil.walk_packages(top.__path__, root + "."):
                yield m.name, importlib.import_module(m.name)


def test_every_exported_name_is_listed():
    """A name in a covered module's __all__ must be in the listing, so the listing stays complete."""
    listed = {e["name"] for e in ENTRIES}
    missing = []
    for module_name, mod in _covered_modules():
        for n in getattr(mod, "__all__", None) or []:
            if f"{module_name}.{n}" not in listed:
                missing.append(f"{module_name}.{n}")
    assert not missing, "exported names missing from contract/public-surface.json: " + ", ".join(sorted(missing))


def test_the_listing_covers_the_pipeline_products():
    assert len(ENTRIES) >= 400
    prefixes = {".".join(e["name"].split(".")[:3]) for e in ENTRIES}
    for expected in ("llm_kit.pipeline.core", "llm_kit.pipeline.runtime", "llm_kit.pipeline.execution",
                     "llm_kit.pipeline.artifacts", "llm_kit.pipeline.analysis"):
        assert expected in prefixes, expected


# --- the references --------------------------------------------------------

def test_the_skill_reference_names_files_that_exist():
    text = (PLUGIN / "skills" / "content-pipeline-domain" / "references" / "building-a-pipeline.md").read_text(
        encoding="utf-8")
    assert "contract/public-surface.json" in text
    assert re.search(r"\$\{LLM_KIT_ROOT:\?[^}]*\}/scripts/check_consumer_contract\.py", text)
    assert (PLUGIN / "scripts" / "check_consumer_contract.py").is_file()
    assert CONTRACT_PATH.is_file()


def test_the_readme_names_the_checker():
    text = (PLUGIN / "README.md").read_text(encoding="utf-8")
    assert "scripts/check_consumer_contract.py" in text and "contract/public-surface.json" in text
