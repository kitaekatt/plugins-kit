"""Make a ``bootstrap_lib`` contract module the ``katsoft_contracts`` module itself.

``bootstrap_lib.execution_event``, ``bootstrap_lib.interrupt_contract`` and
``bootstrap_lib.model_declaration`` are aliases of the module of the same name
in the ``katsoft-contracts`` distribution: ``sys.modules[<bootstrap_lib name>]``
is replaced by the canonical module object, so identity is exact and private
names stay reachable. Nothing is copied or re-exported.

An interpreter that holds ``bootstrap_lib`` without ``katsoft-contracts`` (a
script whose venv re-exec fell through to a bare interpreter, or a venv bootstrap
has not synced) cannot resolve the alias. That is a missing distribution, not a
``bootstrap_lib`` that is too old, and the refusal names the distribution.

Standard library only. The bootstrap engine, hooks and scripts never import this
module or the three aliases (tests/bootstrap/test_standalone_closure.py).
"""
from __future__ import annotations

import importlib
import sys

CONTRACTS_DIST = "katsoft-contracts"
CONTRACTS_PACKAGE = "katsoft_contracts"
CONTRACTS_REQUIREMENT = "katsoft-contracts>=1.0,<2"


def alias(name: str):
    """Replace ``sys.modules[name]`` by the canonical module and return it.

    ``name`` is the dotted ``bootstrap_lib`` module name (``__name__`` of the
    caller). Raises ``ModuleNotFoundError`` with ``name == "katsoft_contracts"``
    when the distribution is not importable in this interpreter.
    """
    target = f"{CONTRACTS_PACKAGE}.{name.rpartition('.')[2]}"
    try:
        module = importlib.import_module(target)
    except ModuleNotFoundError as exc:
        if exc.name not in (CONTRACTS_PACKAGE, target):
            raise
        raise ModuleNotFoundError(
            f"{name} is an alias of {target}, and the {CONTRACTS_DIST} "
            f"distribution is not installed in this interpreter ({sys.executable}). "
            f"This is a missing {CONTRACTS_DIST}, not a bootstrap_lib that is too old: "
            f"run the script under its plugin's bootstrap-provisioned venv, and "
            f"declare '{CONTRACTS_REQUIREMENT}' in that plugin's pyproject.toml so "
            "bootstrap installs it.",
            name=CONTRACTS_PACKAGE,
        ) from exc
    sys.modules[name] = module
    return module
