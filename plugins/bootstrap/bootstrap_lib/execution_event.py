"""Module alias of ``katsoft_contracts.execution_event``.

``sys.modules[__name__]`` becomes the canonical module, so
``bootstrap_lib.execution_event is katsoft_contracts.execution_event`` and every name,
private ones included, resolves on it. The source of the contract is
packages/katsoft-contracts/src/katsoft_contracts/execution_event.py. See
``bootstrap_lib._contracts_alias`` for the refusal when the distribution
is absent.
"""
from bootstrap_lib._contracts_alias import alias as _alias

_alias(__name__)
