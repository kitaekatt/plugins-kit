"""Migration step 12 removed the quota-selection compatibility layer.

``choose_endpoint`` (a thin caller of ``describe``) and ``rank_candidates``
(the two-band rank, replaced by ``declaration.order_by_pace``) were the only
reasons the ``quota_selection`` module existed. Callers use
``llm_scripting_kit.declaration.describe`` and ``order_by_pace``.
"""

import importlib

import pytest

import llm_scripting_kit


def test_quota_selection_module_is_gone():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("llm_scripting_kit.quota_selection")


@pytest.mark.parametrize(
    "name", ["choose_endpoint", "rank_candidates", "QuotaSelection", "Candidate"]
)
def test_compatibility_names_are_not_exported(name):
    assert not hasattr(llm_scripting_kit, name)
    assert name not in llm_scripting_kit.__all__


def test_the_replacements_stay_exported():
    for name in ("describe", "order_by_pace"):
        assert hasattr(llm_scripting_kit, name)
