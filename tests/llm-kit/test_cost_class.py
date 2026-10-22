"""Cost classification (llm_kit.registry.cost): checks C1-C6 of the S1 plan.

C20 (the import graph) lives in test_import_graph.py; this file pins the rules.
"""
from __future__ import annotations

import pytest

from llm_kit.frontdoor import acceptance
from llm_kit.pipeline.execution.drivers import claude_bg
from llm_kit.registry import cost
from llm_kit.registry.cost import (
    FREE,
    METERED,
    SPEND,
    SUBSCRIPTION,
    UNKNOWN,
    USAGE,
    HarnessAuth,
    classify,
    cost_class,
    is_unmetered,
    normalize_allow,
)
from llm_kit.registry.model_endpoints import (
    HARNESS_KIND,
    TRANSPORT_KIND,
    EndpointEntry,
    RoutingConfig,
)


def transport(eid, *, billing=None, group=None, frontdoor=False, model="m"):
    return EndpointEntry(
        id=eid,
        base_url="http://h/v1",
        model=model,
        billing_mode=billing,
        frontdoor=frontdoor,
        routing=RoutingConfig(group=group) if group else None,
    )


def harness(eid, name):
    return EndpointEntry(id=eid, base_url=None, model="m", kind=HARNESS_KIND, harness=name)


def auth(state, reason="r"):
    return HarnessAuth(state=state, reason=reason)


# --- C1 ---------------------------------------------------------------------

@pytest.mark.parametrize("name", ["claude", "codex"])
@pytest.mark.parametrize("state", [METERED, UNKNOWN])
def test_harness_requires_subscription(name, state):
    entry = harness("h", name)
    verdict = classify(entry, harness_auth={name: auth(state, "why not")})
    assert verdict.cost_class == SPEND
    assert "why not" in verdict.reason


@pytest.mark.parametrize("name", ["claude", "codex"])
def test_harness_missing_auth_key_is_spend(name):
    entry = harness("h", name)
    assert cost_class(entry, harness_auth={}) == SPEND
    assert cost_class(entry) == SPEND
    # an auth record for the OTHER harness does not apply
    other = "codex" if name == "claude" else "claude"
    assert cost_class(entry, harness_auth={other: auth(SUBSCRIPTION)}) == SPEND


@pytest.mark.parametrize("name", ["claude", "codex"])
def test_harness_subscription_is_usage(name):
    entry = harness("h", name)
    assert cost_class(entry, harness_auth={name: auth(SUBSCRIPTION)}) == USAGE


def test_other_harness_is_spend_even_with_subscription_record():
    entry = harness("h", "opencode")
    assert cost_class(entry, harness_auth={"opencode": auth(SUBSCRIPTION)}) == SPEND


# --- C2 ---------------------------------------------------------------------

def test_harness_name_case():
    entry = harness("h", "Codex")
    assert cost_class(entry, harness_auth={"codex": auth(SUBSCRIPTION)}) == USAGE
    entry = harness("h", "CLAUDE")
    assert cost_class(entry, harness_auth={"claude": auth(SUBSCRIPTION)}) == USAGE


# --- C3 ---------------------------------------------------------------------

def test_frontdoor_all_tiers():
    door = transport("door", billing="unmetered", frontdoor=True, model="g")
    metered_tier = transport("t2", group="g")  # no billing declared
    free_tier = transport("t1", billing="unmetered", group="g")
    entries = {e.id: e for e in (door, free_tier, metered_tier)}
    verdict = classify(door, entries=entries)
    assert verdict.cost_class == SPEND  # the entry's own billing does not count
    assert "t2" in verdict.reason
    entries = {e.id: e for e in (door, free_tier)}
    assert cost_class(door, entries=entries) == FREE
    # a tier of another group does not matter
    stray = transport("t9", group="other")
    entries = {e.id: e for e in (door, free_tier, stray)}
    assert cost_class(door, entries=entries) == FREE


def test_frontdoor_no_tiers_is_spend():
    door = transport("door", billing="unmetered", frontdoor=True, model="g")
    for entries in (None, {}, {"door": door}):
        verdict = classify(door, entries=entries)
        assert verdict.cost_class == SPEND
        assert "no tiers declared for group g" in verdict.reason


def test_plain_transport():
    assert cost_class(transport("a", billing="unmetered")) == FREE
    assert cost_class(transport("a")) == SPEND
    assert cost_class(transport("a", billing="provider-reported")) == SPEND


# --- C4 ---------------------------------------------------------------------

def test_none_entry_and_unknown_kind_spend():
    verdict = classify(None)
    assert verdict.cost_class == SPEND
    assert "no registry entry" in verdict.reason
    odd = EndpointEntry(id="x", base_url=None, model="m", kind="mystery", billing_mode="unmetered")
    assert cost_class(odd) == SPEND
    assert cost_class(odd, harness_auth={"claude": auth(SUBSCRIPTION)}) == SPEND


# --- C5 ---------------------------------------------------------------------

def test_divert_vars_match_claude_bg():
    assert cost.BILLING_DIVERTING_VARS == claude_bg.BILLING_DIVERTING_VARS
    assert len(cost.BILLING_DIVERTING_VARS) == 8


# --- C6 ---------------------------------------------------------------------

def test_is_paid_delegates(monkeypatch):
    calls = []

    def spy(entry):
        calls.append(entry)
        return True

    monkeypatch.setattr(acceptance, "is_unmetered", spy)
    entry = transport("a")  # undeclared billing: really paid
    assert acceptance.is_paid(entry) is False  # follows the spy, not the field
    assert calls == [entry]


def test_is_paid_rule():
    assert acceptance.is_paid(transport("a")) is True
    assert acceptance.is_paid(transport("a", billing="provider-reported")) is True
    assert acceptance.is_paid(transport("a", billing="unmetered")) is False
    assert is_unmetered(transport("a", billing="unmetered"))
    assert not is_unmetered(transport("a"))


# --- normalize_allow --------------------------------------------------------

def test_normalize_allow():
    assert normalize_allow(None) is None
    assert normalize_allow(["free", "usage", "free"]) == frozenset({"free", "usage"})
    assert normalize_allow(c for c in ("free", "spend")) == frozenset({"free", "spend"})
    assert normalize_allow([]) == frozenset()
    with pytest.raises(ValueError, match="paid"):
        normalize_allow(["free", "paid"])
    with pytest.raises(ValueError):
        normalize_allow("free")
