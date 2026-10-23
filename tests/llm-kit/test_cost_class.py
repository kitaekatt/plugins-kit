"""Cost classification (llm_kit.registry.cost): checks C1-C6 of the S1 plan.

C20 (the import graph) lives in test_import_graph.py; this file pins the rules.
"""
from __future__ import annotations

import pytest

from llm_kit.frontdoor import acceptance
from llm_kit.pipeline.execution.drivers import claude_bg
from llm_kit.registry import cost
from llm_kit.registry.cost import (
    COST_CLASSES,
    LOCAL,
    METERED,
    SUBSCRIPTION,
    UNKNOWN,
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


def transport(eid, *, billing=None, owner=None, group=None, frontdoor=False, model="m"):
    return EndpointEntry(
        id=eid,
        base_url="http://h/v1",
        model=model,
        billing_mode=billing,
        billing_owner=owner,
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
    assert verdict.cost_class == METERED
    assert "why not" in verdict.reason


@pytest.mark.parametrize("name", ["claude", "codex"])
def test_harness_missing_auth_key_is_metered(name):
    entry = harness("h", name)
    assert cost_class(entry, harness_auth={}) == METERED
    assert cost_class(entry) == METERED
    # an auth record for the OTHER harness does not apply
    other = "codex" if name == "claude" else "claude"
    assert cost_class(entry, harness_auth={other: auth(SUBSCRIPTION)}) == METERED


@pytest.mark.parametrize("name", ["claude", "codex"])
def test_harness_subscription_is_subscription(name):
    entry = harness("h", name)
    assert cost_class(entry, harness_auth={name: auth(SUBSCRIPTION)}) == SUBSCRIPTION


def test_other_harness_is_metered_even_with_subscription_record():
    entry = harness("h", "opencode")
    assert cost_class(entry, harness_auth={"opencode": auth(SUBSCRIPTION)}) == METERED


# --- C2 ---------------------------------------------------------------------

def test_harness_name_case():
    entry = harness("h", "Codex")
    assert cost_class(entry, harness_auth={"codex": auth(SUBSCRIPTION)}) == SUBSCRIPTION
    entry = harness("h", "CLAUDE")
    assert cost_class(entry, harness_auth={"claude": auth(SUBSCRIPTION)}) == SUBSCRIPTION


# --- C3 ---------------------------------------------------------------------

def test_frontdoor_all_tiers():
    door = transport("door", billing="unmetered", frontdoor=True, model="g")
    metered_tier = transport("t2", group="g")  # no billing declared
    free_tier = transport("t1", billing="unmetered", owner="self", group="g")
    entries = {e.id: e for e in (door, free_tier, metered_tier)}
    verdict = classify(door, entries=entries)
    assert verdict.cost_class == METERED  # the entry's own billing does not count
    assert "t2" in verdict.reason
    entries = {e.id: e for e in (door, free_tier)}
    assert cost_class(door, entries=entries) == LOCAL
    # a tier of another group does not matter
    stray = transport("t9", group="other")
    entries = {e.id: e for e in (door, free_tier, stray)}
    assert cost_class(door, entries=entries) == LOCAL


def test_frontdoor_no_tiers_is_metered():
    door = transport("door", billing="unmetered", frontdoor=True, model="g")
    for entries in (None, {}, {"door": door}):
        verdict = classify(door, entries=entries)
        assert verdict.cost_class == METERED
        assert "no tiers declared for group g" in verdict.reason


def test_plain_transport():
    assert cost_class(transport("a", billing="unmetered", owner="self")) == LOCAL
    assert cost_class(transport("a")) == METERED
    assert cost_class(transport("a", billing="provider-reported")) == METERED


# --- C4 ---------------------------------------------------------------------

def test_none_entry_and_unknown_kind_metered():
    verdict = classify(None)
    assert verdict.cost_class == METERED
    assert "no registry entry" in verdict.reason
    odd = EndpointEntry(id="x", base_url=None, model="m", kind="mystery", billing_mode="unmetered")
    assert cost_class(odd) == METERED
    assert cost_class(odd, harness_auth={"claude": auth(SUBSCRIPTION)}) == METERED


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
    assert normalize_allow(["local", "subscription", "local"]) == frozenset({"local", "subscription"})
    assert normalize_allow(c for c in ("local", "metered")) == frozenset({"local", "metered"})
    assert normalize_allow([]) == frozenset()
    with pytest.raises(ValueError, match="paid"):
        normalize_allow(["local", "paid"])
    with pytest.raises(ValueError):
        normalize_allow("local")


# --- the renamed classes, and the reason that always travels with a class ----

def test_the_classes_are_local_subscription_metered_with_no_old_names():
    assert COST_CLASSES == ("local", "subscription", "metered")
    for retired in ("free", "usage", "spend"):
        with pytest.raises(ValueError, match=retired):
            normalize_allow([retired])
    for retired in ("FREE", "USAGE", "SPEND"):
        assert not hasattr(cost, retired)


def test_a_metered_reason_does_not_claim_a_charge_occurred():
    cases = [
        classify(None),
        classify(transport("a")),
        classify(transport("a", billing="provider-reported")),
        classify(harness("h", "opencode")),
        classify(harness("h", "claude")),
        classify(harness("h", "claude"), harness_auth={"claude": auth(UNKNOWN, "no signal")}),
        classify(transport("d", frontdoor=True, group="g")),
        classify(
            transport("d", frontdoor=True, group="g"),
            entries={"t": transport("t", billing="provider-reported", group="g")},
        ),
    ]
    for verdict in cases:
        assert verdict.cost_class == METERED
        assert verdict.reason
        lowered = verdict.reason.lower()
        assert "charged" not in lowered and "spent" not in lowered and "paid" not in lowered


def test_every_verdict_carries_a_reason():
    verdicts = [
        classify(transport("a", billing="unmetered", owner="self")),
        classify(harness("h", "codex"), harness_auth={"codex": auth(SUBSCRIPTION, "chatgpt login")}),
        classify(None),
    ]
    assert [v.cost_class for v in verdicts] == [LOCAL, SUBSCRIPTION, METERED]
    assert all(v.reason for v in verdicts)
