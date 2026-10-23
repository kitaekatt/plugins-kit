"""Module alias of ``katsoft_contracts.model_declaration``.

``sys.modules[__name__]`` becomes the canonical module, so
``bootstrap_lib.model_declaration is katsoft_contracts.model_declaration`` and every name,
private ones included, resolves on it. The source of the contract is
packages/katsoft-contracts/src/katsoft_contracts/model_declaration.py. See
``bootstrap_lib._contracts_alias`` for the refusal when the distribution
is absent.
"""
from bootstrap_lib._contracts_alias import alias as _alias

_alias(__name__)
