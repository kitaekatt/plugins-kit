"""bootstrap_lib's contract modules ARE the katsoft_contracts modules.

``bootstrap_lib.<contract>`` is a module alias (``sys.modules`` replaced), not
a copy and not a star re-export, so identity is exact and private names are
reachable. A missing ``katsoft-contracts`` distribution refuses with a message
that names it and does not blame a ``bootstrap_lib`` that is too old.
"""
import importlib
import subprocess
import sys

import pytest

import contract_support as support

NAMES = ("execution_event", "interrupt_contract", "model_declaration")


@pytest.mark.parametrize("name", NAMES)
def test_alias_identity(name):
    alias = importlib.import_module(f"bootstrap_lib.{name}")
    canonical = importlib.import_module(f"katsoft_contracts.{name}")
    assert alias is canonical
    assert sys.modules[f"bootstrap_lib.{name}"] is canonical


@pytest.mark.parametrize("name", NAMES)
def test_every_name_is_the_same_object_including_private_ones(name):
    from bootstrap_lib import execution_event, interrupt_contract, model_declaration
    alias = {"execution_event": execution_event, "interrupt_contract": interrupt_contract,
             "model_declaration": model_declaration}[name]
    canonical = importlib.import_module(f"katsoft_contracts.{name}")
    names = [n for n in vars(canonical) if not (n.startswith("__") and n.endswith("__"))]
    assert names
    assert any(n.startswith("_") for n in names)
    for n in names:
        assert getattr(alias, n) is getattr(canonical, n)


def test_a_private_name_is_reachable_through_the_alias():
    from bootstrap_lib import interrupt_contract
    assert interrupt_contract._KIND_RE.pattern  # noqa: SLF001


def test_attribute_access_and_from_import_agree():
    import bootstrap_lib.model_declaration as via_import
    from bootstrap_lib import model_declaration as via_from
    from bootstrap_lib.model_declaration import CORE_IDS
    assert via_import is via_from
    assert CORE_IDS is via_from.CORE_IDS


def test_bootstrap_lib_holds_no_second_copy_of_the_contract_source():
    """The alias files are thin: the contract source lives in one place."""
    for name in NAMES:
        text = (support.BOOTSTRAP_LIB_DIR / f"{name}.py").read_text(encoding="utf-8")
        assert len(text.splitlines()) < 20, f"{name}.py is not a thin alias"
        assert "def " not in text and "class " not in text


_ABSENT = """
import sys
sys.path.insert(0, {root!r})
sys.modules["katsoft_contracts"] = None
try:
    import bootstrap_lib.{name}
except ModuleNotFoundError as exc:
    print("NAME=" + str(exc.name))
    print("MSG=" + str(exc))
    sys.exit(7)
sys.exit(0)
"""


@pytest.mark.parametrize("name", NAMES)
def test_missing_contracts_distribution_is_refused_by_name(name):
    """The fell-through case: bootstrap_lib resolves, katsoft-contracts does not."""
    result = subprocess.run(
        [sys.executable, "-I", "-c",
         _ABSENT.format(root=str(support.BOOTSTRAP_LIB_DIR.parent), name=name)],
        capture_output=True, text=True)
    assert result.returncode == 7, result.stdout + result.stderr
    lines = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    assert lines["NAME"] == "katsoft_contracts"
    assert "katsoft-contracts" in lines["MSG"]
    assert f"bootstrap_lib.{name}" in lines["MSG"]
    assert "not a bootstrap_lib that is too old" in lines["MSG"]
    assert "update bootstrap" not in lines["MSG"]
