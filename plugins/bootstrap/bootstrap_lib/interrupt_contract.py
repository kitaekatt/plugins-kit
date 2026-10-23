"""Module alias of ``katsoft_contracts.interrupt_contract``.

``sys.modules[__name__]`` becomes the canonical module, so
``bootstrap_lib.interrupt_contract is katsoft_contracts.interrupt_contract`` and every name,
private ones included, resolves on it. The source of the contract is
packages/katsoft-contracts/src/katsoft_contracts/interrupt_contract.py. See
``bootstrap_lib._contracts_alias`` for the refusal when the distribution
is absent.
"""
from bootstrap_lib._contracts_alias import alias as _alias

_alias(__name__)
