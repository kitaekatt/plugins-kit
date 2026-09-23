"""Tests for quota-aware selection over a caller's preference order.

``choose_endpoint`` is a thin caller of :func:`llm_scripting_kit.declaration.describe`
(migration step 3, L3): an out-of-quota entry is removed from the chain, and
the usable ones are ordered by PACE (D5), not by the two-band rank the module
used to apply. ``rank_candidates`` keeps its two-band behaviour as a
deprecated compatibility name until awesome-kit moves to ``order_by_pace``.
"""

import pytest

from llm_scripting_kit import (
    STATUS_AVAILABLE,
    STATUS_NO_DATA,
    STATUS_OUT_OF_QUOTA,
    STATUS_UNDER_QUOTA,
    ConserveSpec,
    NoUsableRoutingTarget,
    choose_endpoint,
    order_by_pace,
    rank_candidates,
)
from llm_scripting_kit import declaration
from llm_scripting_kit.model_endpoints import HARNESS_KIND, EndpointEntry
from llm_scripting_kit.quota_selection import Candidate
from llm_scripting_kit.usage_budget import Budget


def _entry(name, *, paced=True):
    return EndpointEntry(
        id=name,
        base_url=None,
        model=f"{name}-model",
        kind=HARNESS_KIND,
        harness="claude",
        conserve_usage=ConserveSpec(pool="seven_day") if paced else None,
    )


def _budget(status, remaining=None, window=None):
    return Budget(
        status=status, pool="seven_day", detail=status, resets_at=10,
        remaining=remaining, window_remaining=window,
    )


@pytest.fixture
def pinned(monkeypatch):
    """Pin verdicts (status) and fresh readings ((remaining, window)) per id."""
    state = {"verdicts": {}, "fresh": {}}

    monkeypatch.setattr(
        declaration, "pinned_evaluate",
        lambda entry_id, spec, harness, **_kw: _budget(state["verdicts"][entry_id]),
    )

    def fresh(entry_id, spec, harness):
        remaining, window = state["fresh"].get(entry_id, (None, None))
        return _budget(STATUS_AVAILABLE, remaining, window)

    monkeypatch.setattr(declaration, "_fresh_reading", fresh)
    return state


def _choose(pinned, preferences, verdicts, *, fresh=None, default=None):
    pinned["verdicts"] = verdicts
    pinned["fresh"] = fresh or {}
    entries = {name: _entry(name, paced=name in verdicts) for name in preferences}
    return choose_endpoint(preferences, default=default, entries=entries)


# --- removal: out of quota leaves the chain -------------------------------


def test_both_fine_takes_the_first_preference(pinned):
    result = _choose(pinned, ["opus", "sol"], {"opus": STATUS_AVAILABLE, "sol": STATUS_AVAILABLE})
    assert result.chosen == "opus"
    assert result.used_default is False


def test_first_out_of_quota_falls_to_the_second(pinned):
    result = _choose(pinned, ["opus", "sol"], {"opus": STATUS_OUT_OF_QUOTA, "sol": STATUS_AVAILABLE})
    assert result.chosen == "sol"
    assert [c.endpoint for c in result.disabled] == ["opus"]


def test_second_out_of_quota_keeps_the_first(pinned):
    result = _choose(pinned, ["opus", "sol"], {"opus": STATUS_AVAILABLE, "sol": STATUS_OUT_OF_QUOTA})
    assert result.chosen == "opus"
    assert [c.endpoint for c in result.disabled] == ["sol"]


def test_both_out_of_quota_uses_the_default(pinned):
    result = _choose(
        pinned, ["opus", "sol"],
        {"opus": STATUS_OUT_OF_QUOTA, "sol": STATUS_OUT_OF_QUOTA},
        default="openrouter",
    )
    assert result.chosen == "openrouter"
    assert result.used_default is True
    assert result.ranked == ()
    assert "out of quota" in result.reason


def test_an_out_of_quota_endpoint_is_never_in_the_chain(pinned):
    result = _choose(pinned, ["opus", "sol"], {"opus": STATUS_OUT_OF_QUOTA, "sol": STATUS_AVAILABLE})
    assert "opus" not in [c.endpoint for c in result.ranked]


# --- ordering: pace, not bands --------------------------------------------


def test_lower_pace_loses_to_a_higher_pace_peer(pinned):
    # opus is preferred but at 60% pace; sol at 160% moves ahead of it --
    # without opus being removed from the chain.
    result = _choose(
        pinned, ["opus", "sol"],
        {"opus": STATUS_UNDER_QUOTA, "sol": STATUS_AVAILABLE},
        fresh={"opus": (0.3, 0.5), "sol": (0.8, 0.5)},
    )
    assert result.chosen == "sol"
    assert [c.endpoint for c in result.ranked] == ["sol", "opus"]
    assert result.disabled == ()
    assert "higher pace" in result.reason


def test_equal_pace_keeps_the_stated_preference(pinned):
    result = _choose(
        pinned, ["opus", "sol"],
        {"opus": STATUS_UNDER_QUOTA, "sol": STATUS_UNDER_QUOTA},
        fresh={"opus": (0.3, 0.5), "sol": (0.3, 0.5)},
    )
    assert result.chosen == "opus"
    assert [c.endpoint for c in result.ranked] == ["opus", "sol"]


def test_the_owners_example_orders_by_pace():
    # D5, stated on plain values: the rule is order_by_pace.
    class E:
        def __init__(self, id, pace):
            self.id, self.pace = id, pace

    ordered = order_by_pace([
        E("qwen3.8-5090", None), E("opus", 0.76), E("astra", 1.20), E("qwen3.8-m5pro", None),
    ])
    assert [e.id for e in ordered] == ["qwen3.8-5090", "astra", "opus", "qwen3.8-m5pro"]


def test_an_endpoint_that_never_opted_in_keeps_its_place(pinned):
    entries = {"opus": _entry("opus", paced=False), "sol": _entry("sol", paced=False)}
    result = choose_endpoint(["sol", "opus"], entries=entries)
    assert result.chosen == "sol"
    assert all(c.budget is None for c in result.ranked)


def test_no_data_has_no_pace_and_keeps_its_place(pinned):
    result = _choose(
        pinned, ["opus", "sol"], {"opus": STATUS_NO_DATA, "sol": STATUS_AVAILABLE},
        fresh={"sol": (0.9, 0.5)},
    )
    assert result.chosen == "opus", "a pool that could not be read must not cost priority"


def test_pace_sorts_only_among_paced_positions(pinned):
    result = _choose(
        pinned, ["a", "b", "c", "d"],
        {"a": STATUS_UNDER_QUOTA, "b": STATUS_AVAILABLE, "c": STATUS_NO_DATA, "d": STATUS_AVAILABLE},
        fresh={"a": (0.2, 0.5), "b": (0.6, 0.5), "d": (0.9, 0.5)},
    )
    # c has no reading and keeps slot 2; a, b, d re-sort by pace into 0, 1, 3.
    assert [c.endpoint for c in result.ranked] == ["d", "b", "c", "a"]


def test_rank_candidates_keeps_its_two_band_behaviour_while_deprecated():
    candidates = [
        Candidate("a", 0, _budget(STATUS_UNDER_QUOTA)),
        Candidate("b", 1, _budget(STATUS_AVAILABLE)),
        Candidate("c", 2, _budget(STATUS_OUT_OF_QUOTA)),
        Candidate("d", 3, None),
    ]
    ranked, disabled = rank_candidates(candidates)
    assert [c.endpoint for c in ranked] == ["b", "d", "a"]
    assert [c.endpoint for c in disabled] == ["c"]


# --- edges ----------------------------------------------------------------


def test_nothing_usable_and_no_default_propagates_the_floor(pinned):
    with pytest.raises(NoUsableRoutingTarget) as excinfo:
        _choose(pinned, ["opus"], {"opus": STATUS_OUT_OF_QUOTA})
    assert excinfo.value.dispositions[0].disposition == "out-of-quota"


def test_an_unknown_endpoint_is_skipped_rather_than_raising(pinned):
    entries = {"sol": _entry("sol", paced=False)}
    result = choose_endpoint(["opsu", "sol"], entries=entries)
    assert result.chosen == "sol"
    assert result.disabled == ()  # hidden ids are skipped silently


def test_an_unknown_endpoint_is_never_called_out_of_quota(pinned):
    entries = {"sol": _entry("sol", paced=False)}
    result = choose_endpoint(["opsu", "sol"], entries=entries)
    assert "opsu" not in result.reason  # only the floor names a hidden id
    assert "out of quota" not in result.reason


def test_an_unknown_only_list_reports_configuration_not_quota(pinned):
    result = choose_endpoint(["opsu"], default="openrouter", entries={})
    assert result.chosen == "openrouter"
    assert "not configured (opsu)" in result.reason
    assert "out of quota" not in result.reason


def test_both_exclusion_causes_are_named_separately(pinned):
    pinned["verdicts"] = {"opus": STATUS_OUT_OF_QUOTA}
    result = choose_endpoint(["opus", "slo"], default="sol", entries={"opus": _entry("opus")})
    assert "out of quota (opus)" in result.reason
    assert "not configured (slo)" in result.reason


def test_an_empty_preference_list_chooses_the_default():
    result = choose_endpoint([], default="openrouter", entries={})
    assert result.chosen == "openrouter"
    assert result.used_default is True


def test_choose_endpoint_never_probes(pinned, monkeypatch):
    # The Python API kept its no-probe contract: reachability is answered from
    # a cache of "unknown" (fail-open), so nothing is spawned or fetched.
    monkeypatch.setattr(
        declaration, "check_many",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("probed")),
    )
    result = _choose(pinned, ["opus"], {"opus": STATUS_AVAILABLE})
    assert result.chosen == "opus"


def test_a_successful_selection_names_no_hidden_id(pinned):
    # Only the floor may name an unresolved id; a success names rendered ones.
    import json

    entries = {"sol": _entry("sol", paced=False)}
    result = choose_endpoint(["typo-id", "sol"], entries=entries)
    assert result.chosen == "sol"
    assert "typo-id" not in json.dumps(result.to_json())
