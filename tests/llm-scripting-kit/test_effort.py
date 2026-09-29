"""The shared reasoning-effort vocabulary (``llm_scripting_kit.effort``)."""
from __future__ import annotations

import copy

import pytest

from llm_scripting_kit.effort import (
    CHAT_TEMPLATE_KWARGS,
    DELIVERING_STYLES,
    EFFORT_STYLES,
    NINFER,
    OUTCOME_CALLER_EXTRAS,
    OUTCOME_SUPPRESSED,
    OUTCOME_TRANSLATED,
    OUTCOME_UNDELIVERABLE,
    OUTCOME_UNSET,
    TOP_LEVEL,
    UNSUPPORTED,
    EffortDelivery,
    extract_effort,
    place_effort,
    plan_effort,
    remap_effort,
)


def test_vocabulary():
    assert EFFORT_STYLES == ("top-level", "ninfer", "chat_template_kwargs", "unsupported")
    assert DELIVERING_STYLES == ("top-level", "ninfer", "chat_template_kwargs")


@pytest.mark.parametrize(
    "effort,style,expected",
    [
        ("high", NINFER, "xhigh"),
        ("medium", NINFER, "medium"),
        ("xhigh", NINFER, "xhigh"),
        ("high", TOP_LEVEL, "high"),
        ("high", CHAT_TEMPLATE_KWARGS, "high"),
        ("high", None, "high"),
    ],
)
def test_remap_effort_only_maps_ninfer_high(effort, style, expected):
    assert remap_effort(effort, style) == expected


class TestEffortDelivery:
    @pytest.mark.parametrize(
        "style,deliverable,emits",
        [
            (TOP_LEVEL, True, "reasoning_effort"),
            (NINFER, True, "reasoning_effort"),
            (CHAT_TEMPLATE_KWARGS, True, "chat_template_kwargs.reasoning_effort"),
            (UNSUPPORTED, False, None),
            (None, False, None),
        ],
    )
    def test_deliverable_and_emits(self, style, deliverable, emits):
        delivery = EffortDelivery(style, "endpoint")
        assert delivery.deliverable is deliverable
        assert delivery.emits == emits

    def test_to_json_carries_the_remap_only_for_ninfer(self):
        assert EffortDelivery(NINFER, "routing").to_json() == {
            "deliverable": True,
            "emits": "reasoning_effort",
            "style": "ninfer",
            "source": "routing",
            "remap": {"high": "xhigh"},
        }
        assert "remap" not in EffortDelivery(TOP_LEVEL, "frontdoor").to_json()
        assert EffortDelivery(None).to_json() == {
            "deliverable": False, "emits": None, "style": None, "source": "none",
        }


class TestExtractAndPlace:
    def test_extract_prefers_non_null_top_level_and_leaves_nested(self):
        body = {"reasoning_effort": "low", "chat_template_kwargs": {"reasoning_effort": "high"}}
        assert extract_effort(body) == "low"
        assert body == {"chat_template_kwargs": {"reasoning_effort": "high"}}

    def test_extract_falls_back_to_nested_without_mutating_the_callers_dict(self):
        nested = {"reasoning_effort": "high", "x": 1}
        body = {"reasoning_effort": None, "chat_template_kwargs": nested}
        assert extract_effort(body) == "high"
        assert body == {"chat_template_kwargs": {"x": 1}}
        assert nested == {"reasoning_effort": "high", "x": 1}

    def test_place_top_level_and_ninfer(self):
        body: dict = {}
        assert place_effort(body, "high", NINFER) == "xhigh"
        assert body == {"reasoning_effort": "xhigh"}
        body = {}
        assert place_effort(body, "high", TOP_LEVEL) == "high"
        assert body == {"reasoning_effort": "high"}

    def test_place_chat_template_kwargs_copies_the_nested_dict(self):
        nested = {"enable_thinking": True}
        body = {"chat_template_kwargs": nested}
        assert place_effort(body, "medium", CHAT_TEMPLATE_KWARGS) == "medium"
        assert body == {"chat_template_kwargs": {"enable_thinking": True, "reasoning_effort": "medium"}}
        assert nested == {"enable_thinking": True}

    @pytest.mark.parametrize("style", [UNSUPPORTED, None, "bogus"])
    def test_place_is_a_no_op_for_a_non_delivering_style(self, style):
        body = {"a": 1}
        assert place_effort(body, "high", style) is None
        assert body == {"a": 1}


class TestPlanEffort:
    def test_ninfer_medium_goes_top_level(self):
        plan = plan_effort(None, "medium", NINFER)
        assert plan.extra_body == {"reasoning_effort": "medium"}
        assert (plan.applied, plan.outcome) == ("medium", OUTCOME_TRANSLATED)
        assert plan.effort_delivered

    def test_ninfer_high_is_remapped_to_xhigh(self):
        plan = plan_effort({}, "high", NINFER)
        assert plan.extra_body == {"reasoning_effort": "xhigh"}
        assert plan.applied == "xhigh"

    def test_top_level_high_is_sent_as_is(self):
        assert plan_effort({}, "high", TOP_LEVEL).extra_body == {"reasoning_effort": "high"}

    def test_chat_template_kwargs_merges_without_mutating_the_caller(self):
        extras = {"chat_template_kwargs": {"enable_thinking": True}, "top_k": 20}
        snapshot = copy.deepcopy(extras)
        plan = plan_effort(extras, "medium", CHAT_TEMPLATE_KWARGS)
        assert plan.extra_body == {
            "chat_template_kwargs": {"enable_thinking": True, "reasoning_effort": "medium"},
            "top_k": 20,
        }
        assert extras == snapshot
        assert plan.extra_body is not extras

    @pytest.mark.parametrize("style", [UNSUPPORTED, None])
    def test_non_delivering_style_is_undeliverable_and_sends_nothing(self, style):
        plan = plan_effort(None, "medium", style)
        assert plan.extra_body == {}
        assert (plan.applied, plan.outcome) == (None, OUTCOME_UNDELIVERABLE)
        assert not plan.effort_delivered

    def test_unset_effort_keeps_extras_verbatim(self):
        plan = plan_effort({"top_k": 20}, None, NINFER)
        assert plan.extra_body == {"top_k": 20}
        assert plan.outcome == OUTCOME_UNSET

    def test_caller_top_level_wins_verbatim_with_no_remap(self):
        plan = plan_effort({"reasoning_effort": "high"}, "medium", NINFER)
        assert plan.extra_body == {"reasoning_effort": "high"}  # NOT xhigh
        assert (plan.applied, plan.outcome) == (None, OUTCOME_CALLER_EXTRAS)
        plan = plan_effort({"reasoning_effort": "low"}, "medium", TOP_LEVEL)
        assert plan.extra_body == {"reasoning_effort": "low"}

    def test_caller_nested_wins_verbatim(self):
        extras = {"chat_template_kwargs": {"reasoning_effort": "low"}}
        plan = plan_effort(extras, "medium", NINFER)
        assert plan.extra_body == {"chat_template_kwargs": {"reasoning_effort": "low"}}
        assert plan.outcome == OUTCOME_CALLER_EXTRAS

    def test_explicit_none_top_level_suppresses(self):
        extras = {"reasoning_effort": None, "top_k": 20}
        plan = plan_effort(extras, "medium", NINFER)
        assert plan.extra_body == {"top_k": 20}
        assert (plan.applied, plan.outcome) == (None, OUTCOME_SUPPRESSED)
        assert extras == {"reasoning_effort": None, "top_k": 20}

    def test_explicit_none_nested_suppresses_and_drops_an_emptied_template(self):
        extras = {"chat_template_kwargs": {"reasoning_effort": None}}
        plan = plan_effort(extras, "medium", CHAT_TEMPLATE_KWARGS)
        assert plan.extra_body == {}
        assert plan.outcome == OUTCOME_SUPPRESSED
        assert extras == {"chat_template_kwargs": {"reasoning_effort": None}}

    def test_explicit_none_nested_keeps_other_template_keys(self):
        extras = {"chat_template_kwargs": {"reasoning_effort": None, "enable_thinking": False}}
        plan = plan_effort(extras, "medium", CHAT_TEMPLATE_KWARGS)
        assert plan.extra_body == {"chat_template_kwargs": {"enable_thinking": False}}

    def test_explicit_none_suppresses_even_without_an_effort(self):
        plan = plan_effort({"reasoning_effort": None}, None, None)
        assert plan.extra_body == {}
        assert plan.outcome == OUTCOME_SUPPRESSED

    # -- conflicting caller channels: TOP-LEVEL wins, nested duplicate removed --

    def test_both_channels_top_level_wins_and_nested_is_removed(self):
        extras = {
            "reasoning_effort": "low",
            "chat_template_kwargs": {"reasoning_effort": "high", "enable_thinking": True},
        }
        snapshot = copy.deepcopy(extras)
        plan = plan_effort(extras, "medium", CHAT_TEMPLATE_KWARGS)
        assert plan.extra_body == {
            "reasoning_effort": "low",
            "chat_template_kwargs": {"enable_thinking": True},
        }
        assert plan.outcome == OUTCOME_CALLER_EXTRAS
        assert extras == snapshot

    def test_both_channels_emptied_template_is_removed(self):
        extras = {"reasoning_effort": "low", "chat_template_kwargs": {"reasoning_effort": "high"}}
        assert plan_effort(extras, None, None).extra_body == {"reasoning_effort": "low"}

    def test_top_level_none_beats_a_nested_value_and_suppresses_both(self):
        extras = {"reasoning_effort": None, "chat_template_kwargs": {"reasoning_effort": "high"}}
        plan = plan_effort(extras, "medium", NINFER)
        assert plan.extra_body == {}
        assert plan.outcome == OUTCOME_SUPPRESSED

    def test_top_level_value_beats_a_nested_none(self):
        extras = {"reasoning_effort": "low", "chat_template_kwargs": {"reasoning_effort": None, "x": 1}}
        plan = plan_effort(extras, "medium", NINFER)
        assert plan.extra_body == {"reasoning_effort": "low", "chat_template_kwargs": {"x": 1}}
        assert plan.outcome == OUTCOME_CALLER_EXTRAS
