"""Fixtures for the katsoft-contracts tests.

The canonical package is imported from its source tree, never from an
installed copy: the first sys.path entry is packages/katsoft-contracts/src.
"""
import os
import sys
from types import SimpleNamespace

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.normpath(os.path.join(_HERE, os.pardir, os.pardir, "packages",
                                     "katsoft-contracts", "src"))
for _entry in (_SRC, _HERE):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)


@pytest.fixture(scope="session")
def canonical():
    from katsoft_contracts import execution_event, interrupt_contract, model_declaration
    return SimpleNamespace(execution_event=execution_event,
                           interrupt_contract=interrupt_contract,
                           model_declaration=model_declaration)


@pytest.fixture(scope="session")
def bootstrap_copy():
    from bootstrap_lib import execution_event, interrupt_contract, model_declaration
    return SimpleNamespace(execution_event=execution_event,
                           interrupt_contract=interrupt_contract,
                           model_declaration=model_declaration)
