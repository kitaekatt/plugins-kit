"""Behavioral tests for content_pipeline.providers.registry.

Translates loc providers/_framework's register/get/invoke/tier cases onto the
generic tiered registry, plus the run_tier assembly behavior.
"""

import pytest

from content_pipeline.providers import registry
from content_pipeline.providers.registry import (
    GENERATION_TIER,
    SOURCE_TIER,
    ProviderAlreadyRegisteredError,
    ProviderError,
    UnknownProviderError,
)


@pytest.fixture
def clean_registry():
    registry.clear()
    yield
    registry.clear()


def test_register_and_resolve_roundtrip(clean_registry):
    def fn(*args):
        return {"ok": True}

    registry.register("glossary", fn, tier=SOURCE_TIER)
    assert registry.resolve("glossary") is fn
    assert registry.get_tier("glossary") == SOURCE_TIER


def test_double_register_raises(clean_registry):
    registry.register("p", lambda *a: {}, tier=SOURCE_TIER)
    with pytest.raises(ProviderAlreadyRegisteredError):
        registry.register("p", lambda *a: {}, tier=SOURCE_TIER)


def test_double_register_with_replace_succeeds(clean_registry):
    registry.register("p", lambda *a: {"v": 1}, tier=SOURCE_TIER)
    registry.register("p", lambda *a: {"v": 2}, tier=SOURCE_TIER, replace=True)
    assert registry.invoke("p") == {"v": 2}


def test_resolve_unknown_raises(clean_registry):
    with pytest.raises(UnknownProviderError):
        registry.resolve("nope")


def test_register_rejects_bad_name(clean_registry):
    with pytest.raises(ProviderError):
        registry.register("", lambda *a: {}, tier=SOURCE_TIER)


def test_register_rejects_non_callable(clean_registry):
    with pytest.raises(ProviderError):
        registry.register("p", 123, tier=SOURCE_TIER)  # type: ignore[arg-type]


def test_register_rejects_bad_tier(clean_registry):
    with pytest.raises(ProviderError):
        registry.register("p", lambda *a: {}, tier="bogus")


def test_invoke_forwards_args_and_returns_dict(clean_registry):
    registry.register("p", lambda a, b: {"sum": a + b}, tier=GENERATION_TIER)
    assert registry.invoke("p", 2, 3) == {"sum": 5}


def test_invoke_non_dict_raises(clean_registry):
    registry.register("p", lambda *a: "not a dict", tier=SOURCE_TIER)
    with pytest.raises(ProviderError, match="must return a dict"):
        registry.invoke("p")


def test_decorator_self_registration(clean_registry):
    @registry.provider("deco", tier=SOURCE_TIER)
    def my_provider(*args):
        return {"from": "decorator"}

    assert registry.resolve("deco") is my_provider
    assert my_provider("x") == {"from": "decorator"}  # still directly callable


def test_registered_names_sorted_and_filtered(clean_registry):
    registry.register("b_src", lambda *a: {}, tier=SOURCE_TIER)
    registry.register("a_gen", lambda *a: {}, tier=GENERATION_TIER)
    registry.register("c_src", lambda *a: {}, tier=SOURCE_TIER)
    assert registry.registered_names() == ("a_gen", "b_src", "c_src")
    assert registry.registered_names(tier=SOURCE_TIER) == ("b_src", "c_src")
    assert registry.registered_names(tier=GENERATION_TIER) == ("a_gen",)


def test_registered_names_is_snapshot(clean_registry):
    registry.register("p", lambda *a: {}, tier=SOURCE_TIER)
    names = registry.registered_names()
    registry.register("q", lambda *a: {}, tier=SOURCE_TIER)
    assert names == ("p",)  # earlier snapshot unaffected


def test_tiers_snapshot(clean_registry):
    registry.register("p", lambda *a: {}, tier=SOURCE_TIER)
    registry.register("q", lambda *a: {}, tier=GENERATION_TIER)
    assert registry.tiers() == {"p": SOURCE_TIER, "q": GENERATION_TIER}


def test_unregister(clean_registry):
    registry.register("p", lambda *a: {}, tier=SOURCE_TIER)
    registry.unregister("p")
    with pytest.raises(UnknownProviderError):
        registry.resolve("p")


def test_unregister_unknown_raises(clean_registry):
    with pytest.raises(UnknownProviderError):
        registry.unregister("nope")


def test_run_tier_assembles_ordered_brief(clean_registry):
    registry.register("zeta", lambda unit: {"z": unit}, tier=SOURCE_TIER)
    registry.register("alpha", lambda unit: {"a": unit}, tier=SOURCE_TIER)
    registry.register("gen", lambda unit: {"g": unit}, tier=GENERATION_TIER)

    brief = registry.run_tier(SOURCE_TIER, "UNIT")
    # Only source-tier providers, keyed by name, in sorted order.
    assert list(brief.keys()) == ["alpha", "zeta"]
    assert brief == {"alpha": {"a": "UNIT"}, "zeta": {"z": "UNIT"}}


def test_run_tier_forwards_variant_args(clean_registry):
    registry.register("g", lambda unit, variant: {"u": unit, "v": variant}, tier=GENERATION_TIER)
    brief = registry.run_tier(GENERATION_TIER, "UNIT", "zh")
    assert brief == {"g": {"u": "UNIT", "v": "zh"}}


def test_run_tier_bad_tier_raises(clean_registry):
    with pytest.raises(ProviderError):
        registry.run_tier("bogus")


def test_error_hierarchy(clean_registry):
    assert issubclass(UnknownProviderError, ProviderError)
    assert issubclass(ProviderAlreadyRegisteredError, ProviderError)
