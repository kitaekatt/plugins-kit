"""Honest cost classes: `local` needs declared ownership; a harness takes its endpoint's class.

`billing.mode: unmetered` states marginal cost (USD 0), not who owns the hardware, and an
opencode harness entry holds an opaque model string, so neither can establish `local` alone
(consent-design.md, Sol amendment 1). Two registry fields carry what the user knows:

* ``billing: {mode: unmetered, owner: self}`` on a transport entry;
* ``endpoint: <transport entry id>`` on an opencode (non-claude, non-codex) harness entry.

test_cost_class.py pins the rule order; this file pins the two additions and the registry
reader that feeds them.
"""
from __future__ import annotations

import pytest

from llm_kit.registry.cost import (
    LOCAL,
    METERED,
    SUBSCRIPTION,
    HarnessAuth,
    classify,
    cost_class,
)
from llm_kit.registry.model_endpoints import (
    BILLING_OWNERS,
    HARNESS_KIND,
    TRANSPORT_KIND,
    EndpointEntry,
    EndpointMetadataError,
    RoutingConfig,
    load_endpoint_registry,
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


def opencode(eid, endpoint=None, harness="opencode"):
    return EndpointEntry(
        id=eid, base_url=None, model="provider/model", kind=HARNESS_KIND, harness=harness, endpoint=endpoint
    )


def table(*entries):
    return {e.id: e for e in entries}


def owned(eid, **kw):
    return transport(eid, billing="unmetered", owner="self", **kw)


# --- ownership: `local` needs more than an unmetered billing mode --------------------------------


def test_unmetered_without_ownership_is_metered_and_says_so():
    verdict = classify(transport("free-tier", billing="unmetered"))
    assert verdict.cost_class == METERED
    assert "billing.owner: self" in verdict.reason
    assert "(billing unverified)" in verdict.reason


def test_unmetered_with_ownership_is_local():
    verdict = classify(owned("mine"))
    assert verdict.cost_class == LOCAL
    assert "owner self" in verdict.reason


@pytest.mark.parametrize("billing", [None, "provider-reported"])
def test_ownership_alone_does_not_make_a_transport_local(billing):
    verdict = classify(transport("a", billing=billing, owner="self"))
    assert verdict.cost_class == METERED
    assert "not declared unmetered" in verdict.reason


def test_a_frontdoor_needs_every_tier_owned():
    door = transport("door", frontdoor=True, model="g")
    mine = owned("t1", group="g")
    unmarked = transport("t2", billing="unmetered", group="g")
    verdict = classify(door, entries=table(door, mine, unmarked))
    assert verdict.cost_class == METERED
    assert "t2" in verdict.reason and "billing.owner: self" in verdict.reason
    assert cost_class(door, entries=table(door, mine)) == LOCAL
    # the door's own ownership mark does not count, like its own billing
    marked_door = transport("door", frontdoor=True, model="g", billing="unmetered", owner="self")
    assert cost_class(marked_door, entries=table(marked_door, unmarked)) == METERED


# --- a harness takes the class of the endpoint it runs on ------------------------------------------


def test_opencode_harness_on_an_owned_endpoint_is_local_and_names_it():
    entries = table(owned("qwen-5090"), opencode("qwen-5090-harness", "qwen-5090"))
    verdict = classify(entries["qwen-5090-harness"], entries=entries)
    assert verdict.cost_class == LOCAL
    assert "qwen-5090" in verdict.reason


def test_harness_inherits_a_metered_endpoint():
    entries = table(transport("paid-or"), opencode("h", "paid-or"))
    verdict = classify(entries["h"], entries=entries)
    assert verdict.cost_class == METERED
    assert "paid-or" in verdict.reason


def test_harness_on_unmetered_but_unowned_endpoint_is_metered():
    entries = table(transport("free-tier", billing="unmetered"), opencode("h", "free-tier"))
    verdict = classify(entries["h"], entries=entries)
    assert verdict.cost_class == METERED
    assert "free-tier" in verdict.reason and "billing.owner: self" in verdict.reason


def test_harness_on_a_frontdoor_counts_every_tier():
    door = transport("door", frontdoor=True, model="g")
    all_mine = table(door, owned("t1", group="g"), opencode("h", "door"))
    assert cost_class(all_mine["h"], entries=all_mine) == LOCAL
    spills = table(door, owned("t1", group="g"), transport("t2-paid", group="g"), opencode("h", "door"))
    verdict = classify(spills["h"], entries=spills)
    assert verdict.cost_class == METERED
    assert "t2-paid" in verdict.reason and "door" in verdict.reason


@pytest.mark.parametrize("entries_of", [lambda h: None, lambda h: {}, lambda h: table(h)])
def test_harness_with_an_unresolvable_endpoint_is_metered_naming_it(entries_of):
    harness = opencode("h", "nowhere-endpoint")
    verdict = classify(harness, entries=entries_of(harness))
    assert verdict.cost_class == METERED
    assert "nowhere-endpoint" in verdict.reason
    assert "(billing unverified)" in verdict.reason


def test_harness_whose_endpoint_is_a_harness_is_metered():
    entries = table(opencode("a", "b"), opencode("b", "a"), opencode("self-ref", "self-ref"))
    for eid in ("a", "b", "self-ref"):
        verdict = classify(entries[eid], entries=entries)
        assert verdict.cost_class == METERED
        assert "not a transport" in verdict.reason


def test_opencode_harness_without_an_endpoint_is_metered_and_says_why():
    verdict = classify(opencode("h"), entries=table(owned("e")))
    assert verdict.cost_class == METERED
    assert "declares no endpoint" in verdict.reason


def test_endpoint_does_not_override_authentication_for_claude_and_codex():
    entries = table(owned("local-endpoint"))
    for name in ("claude", "codex"):
        harness = opencode("h", "local-endpoint", harness=name)
        assert cost_class(harness, entries=entries, harness_auth={}) == METERED
        sub = {name: HarnessAuth(SUBSCRIPTION, "login")}
        assert cost_class(harness, entries=entries, harness_auth=sub) == SUBSCRIPTION


# --- the registry reader: parse the two fields ------------------------------------------------------

_FLEET = """\
version: 1
default: qwen38
models:
  qwen38-m5:
    base_url: http://m5.example.test:8000/v1
    model: qwen3.8-27b
    billing: {mode: unmetered, owner: self}
    routing: {group: qwen3.8, order: 2}
  qwen38-5090:
    base_url: http://gpu.example.test:8080/v1
    model: qwen3.8-27b
    billing: {mode: unmetered, owner: self}
    routing: {group: qwen3.8, order: 1}
  qwen36-5090:
    base_url: http://gpu.example.test:8080/v1
    model: qwen3.6
    billing: {mode: unmetered, owner: self}
    routing: {group: qwen3.6, order: 1}
  provider-free-tier:
    base_url: https://free.example.test/v1
    model: some-free-model
    billing: {mode: unmetered}
  deepseek-flash-or:
    base_url: https://openrouter.example.test/api/v1
    model: deepseek-flash
    key_env: SOME_KEY
    routing: {group: qwen3.8, order: 3}
  qwen38:
    base_url: http://gpu.example.test:4000/v1
    model: qwen3.8
    frontdoor: true
  qwen36:
    base_url: http://gpu.example.test:4001/v1
    model: qwen3.6
    frontdoor: true
  qwen38-5090-harness:
    harness: opencode
    model: qwen5090-local/qwen38-5090x
    endpoint: qwen38-5090
  qwen38-m5-harness:
    harness: opencode
    model: qwen38-local/qwen3.8-27b
    endpoint: qwen38-m5
  qwen38-harness:
    harness: opencode
    model: frontdoor-local/qwen3.8
    endpoint: qwen38
  qwen36-harness:
    harness: opencode
    model: frontdoor-local/qwen3.6
    endpoint: qwen36
  free-harness:
    harness: opencode
    model: free/some-free-model
    endpoint: provider-free-tier
  deepseek-flash:
    harness: opencode
    model: openrouter/deepseek-flash
  lost-harness:
    harness: opencode
    model: x/y
    endpoint: no-such-entry
"""


def _load(tmp_path, text):
    path = tmp_path / "registry.yaml"
    path.write_text(text, encoding="utf-8")
    return load_endpoint_registry({"MODEL_ENDPOINTS_REGISTRY": str(path)})


def test_the_reader_parses_ownership_and_endpoint(tmp_path):
    reg = _load(tmp_path, _FLEET)
    assert reg.entries["qwen38-5090"].billing_owner == "self"
    assert reg.entries["provider-free-tier"].billing_mode == "unmetered"
    assert reg.entries["provider-free-tier"].billing_owner is None
    assert reg.entries["qwen38-5090-harness"].endpoint == "qwen38-5090"
    assert reg.entries["deepseek-flash"].endpoint is None
    assert reg.notes == []
    assert BILLING_OWNERS == ("self",)


def test_a_fleet_registry_classifies_honestly(tmp_path):
    reg = _load(tmp_path, _FLEET)
    classes = {eid: cost_class(e, entries=reg.entries) for eid, e in reg.entries.items()}
    assert classes == {
        "qwen38-m5": LOCAL,
        "qwen38-5090": LOCAL,
        "qwen36-5090": LOCAL,
        "provider-free-tier": METERED,  # unmetered, owned by a provider
        "deepseek-flash-or": METERED,
        "qwen38": METERED,  # a paid spill tier is behind this front door
        "qwen36": LOCAL,  # every tier of its group is owned
        "qwen38-5090-harness": LOCAL,
        "qwen38-m5-harness": LOCAL,
        "qwen38-harness": METERED,  # inherits the front door
        "qwen36-harness": LOCAL,
        "free-harness": METERED,
        "deepseek-flash": METERED,
        "lost-harness": METERED,
    }
    assert "no-such-entry" in classify(reg.entries["lost-harness"], entries=reg.entries).reason
    assert "deepseek-flash-or" in classify(reg.entries["qwen38-harness"], entries=reg.entries).reason


def test_a_registry_written_before_ownership_marks_classifies_metered(tmp_path):
    """The break is deliberate: unmetered alone no longer proves `local`."""
    text = _FLEET.replace(", owner: self", "")
    reg = _load(tmp_path, text)
    for eid in ("qwen38-m5", "qwen38-5090", "qwen36-5090", "qwen36"):
        verdict = classify(reg.entries[eid], entries=reg.entries)
        assert verdict.cost_class == METERED, eid
        assert "billing.owner: self" in verdict.reason, eid


@pytest.mark.parametrize("owner", ["provider", "", 5, True])
def test_an_invalid_owner_is_noted_and_never_local(tmp_path, owner):
    text = _FLEET.replace("billing: {mode: unmetered, owner: self}", f"billing: {{mode: unmetered, owner: {owner!r}}}")
    reg = _load(tmp_path, text)
    entry = reg.entries["qwen38-5090"]
    assert entry.billing_owner is None
    assert any("invalid 'owner'" in note and "qwen38-5090" in note for note in reg.notes)
    assert cost_class(entry, entries=reg.entries) == METERED


@pytest.mark.parametrize("endpoint", ["''", "5", "[a]", "{a: b}", "true"])
def test_an_invalid_harness_endpoint_raises(tmp_path, endpoint):
    text = _FLEET.replace("endpoint: qwen38-5090\n", f"endpoint: {endpoint}\n", 1)
    with pytest.raises(EndpointMetadataError, match="qwen38-5090-harness.*invalid 'endpoint'"):
        _load(tmp_path, text)


def test_endpoint_on_claude_or_codex_or_a_transport_is_noted_and_ignored(tmp_path):
    text = _FLEET + (
        "  claude-opus:\n    harness: claude\n    model: opus\n    endpoint: qwen38-5090\n"
        "  stray-transport:\n    base_url: http://h/v1\n    model: m\n    endpoint: qwen38-5090\n"
    )
    reg = _load(tmp_path, text)
    assert reg.entries["claude-opus"].endpoint is None
    assert reg.entries["stray-transport"].endpoint is None
    notes = "\n".join(reg.notes)
    assert "claude-opus" in notes and "follows its authentication" in notes
    assert "stray-transport" in notes and "only a harness entry runs on an endpoint" in notes
    assert reg.entries["claude-opus"].kind == HARNESS_KIND
    assert reg.entries["stray-transport"].kind == TRANSPORT_KIND


# --- layered config entries and `models describe` carry the same fields ---------------------------


def test_layered_config_entries_parse_ownership_and_endpoint():
    from llm_kit.registry import models

    notes: list[str] = []
    door_tier = models._config_model_entry(
        "mine", {"base_url": "http://h/v1", "model": "m", "billing": {"mode": "unmetered", "owner": "self"}}, notes
    )
    harness = models._config_model_entry(
        "h", {"harness": "opencode", "model": "p/m", "endpoint": "mine"}, notes
    )
    assert door_tier.billing_owner == "self" and harness.endpoint == "mine"
    entries = table(door_tier, harness)
    assert cost_class(harness, entries=entries) == LOCAL
    unowned = models._config_model_entry(
        "free", {"base_url": "http://h/v1", "model": "m", "billing": {"mode": "unmetered"}}, notes
    )
    assert cost_class(unowned) == METERED
    with pytest.raises(EndpointMetadataError, match="invalid 'endpoint'"):
        models._config_model_entry("bad", {"harness": "opencode", "model": "m", "endpoint": 5}, notes)
    assert notes == []


def test_describe_json_carries_owner_and_harness_endpoint_only_when_declared():
    from llm_kit.cli.models import _entry_json

    mine = _entry_json(owned("mine"))
    assert mine["billing"] == {"mode": "unmetered", "owner": "self"}
    assert _entry_json(transport("free", billing="unmetered"))["billing"] == {"mode": "unmetered"}
    assert "billing" not in _entry_json(transport("plain"))
    assert _entry_json(opencode("h", "mine"))["endpoint"] == "mine"
    assert "endpoint" not in _entry_json(opencode("h"))
