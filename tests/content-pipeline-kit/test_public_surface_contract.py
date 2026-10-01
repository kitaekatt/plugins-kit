"""Guard: the public-surface contract and the code agree.

A public name cannot leave the code without moving to `deprecated` or `removed`
in contract/public-surface.json, and the policy window applies
(docs/reference/deprecation-policy.md).
"""

import importlib
import json
import pkgutil
import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "content-pipeline-kit"
CONTRACT = json.loads((PLUGIN / "contract" / "public-surface.json").read_text(encoding="utf-8"))
ENTRIES = CONTRACT["entries"]
PACKAGE = CONTRACT["package"]
STATUSES = {"public", "deprecated", "removed"}


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
    for e in ENTRIES:
        assert e["status"] in STATUSES, e
        assert e["name"].startswith(PACKAGE + "."), e
        if e["status"] == "removed":
            assert e.get("since") and e.get("replacement"), e
        if e["status"] == "deprecated":
            assert e.get("since") and e.get("remove_after") and e.get("replacement"), e


@pytest.mark.parametrize("name", _ids("public") + _ids("deprecated"))
def test_listed_name_still_resolves(name):
    assert _resolves(name), (
        f"{name} is listed public/deprecated but no longer resolves. Removing a "
        "public name needs a DeprecationWarning shim for the policy window first; "
        "if the window has run, move the entry to status removed with a replacement."
    )


@pytest.mark.parametrize("name", _ids("removed"))
def test_removed_name_stays_removed(name):
    assert not _resolves(name), (
        f"{name} is listed removed but resolves. A restored name must be listed "
        "as deprecated or public."
    )


def test_deprecated_entries_are_inside_their_window():
    current = _version(
        json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]
    )
    for e in ENTRIES:
        if e["status"] == "deprecated":
            assert current < _version(e["remove_after"]), (
                f"{e['name']} outlived remove_after {e['remove_after']}: remove it "
                "and record it as removed."
            )


def test_every_exported_name_is_listed():
    """A name in a module's __all__ must be in the contract, so the listing stays complete."""
    sys.path.insert(0, str(PLUGIN / "lib"))
    pkg = importlib.import_module(PACKAGE)
    listed = {e["name"] for e in ENTRIES}
    missing = []
    for m in pkgutil.walk_packages(pkg.__path__, PACKAGE + "."):
        mod = importlib.import_module(m.name)
        for n in getattr(mod, "__all__", None) or []:
            if f"{m.name}.{n}" not in listed:
                missing.append(f"{m.name}.{n}")
    assert not missing, (
        "exported names missing from contract/public-surface.json (add them as public): "
        + ", ".join(sorted(missing))
    )
