"""Tests for quota-aware selection over a caller's preference order.

The six-row table in `quota_selection`'s module docstring is the spec; every
row has a test here, named for the row it pins.
"""

import json

import pytest

from llm_scripting_kit import (
    STATUS_AVAILABLE,
    STATUS_NO_DATA,
    STATUS_OUT_OF_QUOTA,
    STATUS_UNDER_QUOTA,
    ConserveSpec,
    choose_endpoint,
    rank_candidates,
)
from llm_scripting_kit import quota_selection
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


def _budget(status):
    return Budget(status=status, pool="seven_day", detail=status, resets_at=10)


def _choose(preferences, verdicts, *, default=None, paced=None):
    """Run a selection with each named endpoint pinned to a given status.

    `verdicts` maps endpoint -> status; an endpoint absent from it is a
    configured entry that never opted in (no budget at all).
    """
    paced = verdicts if paced is None else paced
    entries = {name: _entry(name, paced=name in paced) for name in preferences}
    original = quota_selection.pinned_evaluate
    try:
        quota_selection.pinned_evaluate = (
            lambda entry_id, spec, harness: _budget(verdicts[entry_id])
        )
        return choose_endpoint(preferences, default=default, entries=entries)
    finally:
        quota_selection.pinned_evaluate = original


# --- the six rows of the spec table ---------------------------------------


def test_both_fine_takes_the_first_preference():
    result = _choose(["opus", "sol"], {"opus": STATUS_AVAILABLE, "sol": STATUS_AVAILABLE})
    assert result.chosen == "opus"
    assert result.used_default is False


def test_first_out_of_quota_falls_to_the_second():
    result = _choose(["opus", "sol"], {"opus": STATUS_OUT_OF_QUOTA, "sol": STATUS_AVAILABLE})
    assert result.chosen == "sol"
    assert [c.endpoint for c in result.disabled] == ["opus"]


def test_second_out_of_quota_keeps_the_first():
    result = _choose(["opus", "sol"], {"opus": STATUS_AVAILABLE, "sol": STATUS_OUT_OF_QUOTA})
    assert result.chosen == "opus"
    assert [c.endpoint for c in result.disabled] == ["sol"]


def test_both_out_of_quota_uses_the_default():
    result = _choose(
        ["opus", "sol"],
        {"opus": STATUS_OUT_OF_QUOTA, "sol": STATUS_OUT_OF_QUOTA},
        default="openrouter",
    )
    assert result.chosen == "openrouter"
    assert result.used_default is True
    assert result.ranked == ()
    assert "out of quota" in result.reason


def test_under_quota_loses_to_an_available_peer():
    # The de-prioritize row: opus is preferred but behind pace, so a peer that
    # is not behind pace wins -- without opus being removed from the chain.
    result = _choose(["opus", "sol"], {"opus": STATUS_UNDER_QUOTA, "sol": STATUS_AVAILABLE})
    assert result.chosen == "sol"
    assert [c.endpoint for c in result.ranked] == ["sol", "opus"]
    assert result.disabled == ()


def test_both_under_quota_falls_back_to_the_stated_preference():
    # Neither is disabled, so the caller's own order decides again.
    result = _choose(["opus", "sol"], {"opus": STATUS_UNDER_QUOTA, "sol": STATUS_UNDER_QUOTA})
    assert result.chosen == "opus"
    assert [c.endpoint for c in result.ranked] == ["opus", "sol"]


# --- the rules behind the table -------------------------------------------


def test_an_under_quota_endpoint_is_still_in_the_chain():
    # The distinction the whole feature rests on: de-prioritized is not
    # dropped, so a caller with a retry loop can still reach it.
    result = _choose(["opus", "sol"], {"opus": STATUS_UNDER_QUOTA, "sol": STATUS_AVAILABLE})
    assert "opus" in [c.endpoint for c in result.ranked]


def test_an_out_of_quota_endpoint_is_never_in_the_chain():
    result = _choose(["opus", "sol"], {"opus": STATUS_OUT_OF_QUOTA, "sol": STATUS_AVAILABLE})
    assert "opus" not in [c.endpoint for c in result.ranked]


def test_an_endpoint_that_never_opted_in_ranks_available():
    # Opting in is what asks for pacing; an endpoint that did not is never
    # de-prioritized by it.
    entries = {"opus": _entry("opus", paced=False), "sol": _entry("sol", paced=False)}
    result = choose_endpoint(["sol", "opus"], entries=entries)
    assert result.chosen == "sol"
    assert all(c.budget is None for c in result.ranked)


def test_no_data_does_not_deprioritize():
    result = _choose(["opus", "sol"], {"opus": STATUS_NO_DATA, "sol": STATUS_AVAILABLE})
    assert result.chosen == "opus", "a pool that could not be read must not cost priority"


def test_preference_order_is_the_tiebreak_within_a_band():
    result = _choose(
        ["a", "b", "c", "d"],
        {
            "a": STATUS_UNDER_QUOTA,
            "b": STATUS_AVAILABLE,
            "c": STATUS_UNDER_QUOTA,
            "d": STATUS_AVAILABLE,
        },
    )
    # Available in stated order, then under-quota in stated order.
    assert [c.endpoint for c in result.ranked] == ["b", "d", "a", "c"]


def test_ranking_is_pure_and_stable():
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


def test_nothing_usable_and_no_default_chooses_nothing():
    result = _choose(["opus"], {"opus": STATUS_OUT_OF_QUOTA})
    assert result.chosen is None
    assert result.used_default is False
    assert "no default" in result.reason
    assert "out of quota (opus)" in result.reason


def test_an_unknown_endpoint_is_skipped_rather_than_raising():
    # One typo must not take down a fallback chain that is otherwise fine.
    entries = {"sol": _entry("sol", paced=False)}
    result = choose_endpoint(["opsu", "sol"], entries=entries)
    assert result.chosen == "sol"
    assert [c.endpoint for c in result.disabled] == ["opsu"]


def test_an_unknown_endpoint_is_never_called_out_of_quota():
    # A typo is a configuration error, not an account fact. Saying "out of
    # quota" here sends the reader to look at their usage for a misspelling --
    # and this sentence is what the `choose` verb prints.
    entries = {"sol": _entry("sol", paced=False)}
    result = choose_endpoint(["opsu", "sol"], entries=entries)
    assert "not configured" in result.reason
    assert "out of quota" not in result.reason


def test_an_unknown_only_list_reports_configuration_not_quota():
    result = choose_endpoint(["opsu"], default="openrouter", entries={})
    assert result.chosen == "openrouter"
    assert "not configured (opsu)" in result.reason
    assert "out of quota" not in result.reason


def test_both_exclusion_causes_are_named_separately():
    entries = {"opus": _entry("opus")}
    original = quota_selection.pinned_evaluate
    try:
        quota_selection.pinned_evaluate = (
            lambda entry_id, spec, harness: _budget(STATUS_OUT_OF_QUOTA)
        )
        result = choose_endpoint(["opus", "slo"], entries=entries)
    finally:
        quota_selection.pinned_evaluate = original
    assert "out of quota (opus)" in result.reason
    assert "not configured (slo)" in result.reason


def test_an_empty_preference_list_chooses_the_default():
    result = choose_endpoint([], default="openrouter", entries={})
    assert result.chosen == "openrouter"
    assert result.used_default is True


# --- the CLI surface ------------------------------------------------------


def test_choose_verb_prints_the_winner_and_its_reason(monkeypatch, capsys):
    from llm_scripting_kit import cli

    monkeypatch.setattr(
        cli,
        "choose_endpoint",
        lambda prefs, **kw: quota_selection.QuotaSelection(
            chosen="sol", ranked=(), disabled=(), reason="'sol'; preferred over opus"
        ),
    )
    assert cli.main(["choose", "--prefer", "opus,sol"]) == cli.EXIT_OK
    captured = capsys.readouterr()
    assert captured.out.strip() == "sol"
    assert "preferred over opus" in captured.err


def test_choose_verb_exits_nonzero_when_nothing_is_usable(monkeypatch, capsys):
    from llm_scripting_kit import cli

    monkeypatch.setattr(
        cli,
        "choose_endpoint",
        lambda prefs, **kw: quota_selection.QuotaSelection(
            chosen=None, ranked=(), disabled=(), reason="every candidate is out of quota"
        ),
    )
    # A zero exit here would report a successful choice that was never made.
    assert cli.main(["choose", "--prefer", "opus,sol"]) == cli.EXIT_FAILURE


def test_choose_verb_json_carries_the_whole_chain(monkeypatch, capsys):
    from llm_scripting_kit import cli

    monkeypatch.setattr(
        cli,
        "choose_endpoint",
        lambda prefs, **kw: quota_selection.QuotaSelection(
            chosen="sol",
            ranked=(Candidate("sol", 1, None), Candidate("opus", 0, _budget(STATUS_UNDER_QUOTA))),
            disabled=(Candidate("fable", 2, _budget(STATUS_OUT_OF_QUOTA)),),
            reason="x",
        ),
    )
    assert cli.main(["choose", "--prefer", "opus,sol,fable", "--json"]) == cli.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["chosen"] == "sol"
    assert [c["endpoint"] for c in payload["ranked"]] == ["sol", "opus"]
    assert payload["disabled"][0]["budget"]["status"] == STATUS_OUT_OF_QUOTA
