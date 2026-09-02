"""Regressions for defects a code review found in the dispatch binding.

Each test here pins a fix whose absence was invisible to the suite that shipped
alongside it -- the multi-target lane in particular passed its own tests only
because a mock backend ignores the prompt it is given.
"""

from pathlib import Path

import pytest
import yaml

from yaml_data_editor_kit.dispatch.adapter import prompt_for
from yaml_data_editor_kit.dispatch.state import write_plan
from yaml_data_editor_kit.dispatch.units import plain_value, prompt_for_payload
from yaml_data_editor_kit.schema.corpus import ABSENT


_MULTI_TARGET = {
    "instruction": "Apply both comments.",
    "comment_ids": ["bolt-note", "theme-note"],
    "targets": [
        {
            "id": "record:product/bolt",
            "anchor": "product/bolt",
            "anchored_slice": {"id": "bolt", "name": "Bolt"},
            "comments": ["rename it"],
            "content_hash": "sha256:aa",
        },
        {
            "id": "document:settings",
            "anchor": "settings",
            "anchored_slice": {"theme": "plain"},
            "comments": ["brighten it"],
            "content_hash": "sha256:bb",
        },
    ],
}


def test_multi_target_prompt_carries_the_instruction_and_every_target() -> None:
    """The response must partition every target anchor, so the prompt has to show them."""
    rendered = yaml.safe_load(prompt_for_payload(_MULTI_TARGET))

    assert rendered["instruction"] == "Apply both comments."
    assert [target["anchor"] for target in rendered["targets"]] == [
        "product/bolt",
        "settings",
    ]
    assert rendered["targets"][0]["slice"] == {"id": "bolt", "name": "Bolt"}
    assert rendered["targets"][1]["comments"] == ["brighten it"]


def test_multi_target_prompt_is_not_a_single_target_prompt_of_nulls() -> None:
    """The single-target keys are absent from a multi-target payload; reading only
    those produced a prompt of nulls and an empty comment list."""
    rendered = yaml.safe_load(prompt_for_payload(_MULTI_TARGET))

    assert "targets" in rendered
    assert rendered.get("anchor") is None or "anchor" not in rendered
    assert rendered.get("comments") != []


def test_worker_mount_prompt_matches_the_inline_prompt() -> None:
    """One question, whichever lane asks it."""
    assert prompt_for(_MULTI_TARGET) == prompt_for_payload(_MULTI_TARGET)


def test_single_target_prompt_is_unchanged() -> None:
    payload = {
        "anchor": "product/bolt",
        "anchored_slice": {"id": "bolt"},
        "comments": ["fix it"],
        "content_hash": "sha256:cc",
    }
    rendered = yaml.safe_load(prompt_for_payload(payload))

    assert rendered == {
        "anchor": "product/bolt",
        "slice": {"id": "bolt"},
        "comments": ["fix it"],
        "content_hash": "sha256:cc",
    }


def test_plan_write_serializes_an_absent_anchored_slice(tmp_path: Path) -> None:
    """An absent slice is a supported anchor, and the durable plan must survive it."""
    path = tmp_path / "run" / "dispatch-plan.yaml"

    plan = write_plan(
        path,
        corpus_path=tmp_path / "corpus",
        comment_store_path=tmp_path / "comments",
        units=[{"id": "unit-a", "payload": {"anchor": "settings/missing", "anchored_slice": ABSENT}}],
    )

    assert plan.digest
    assert path.exists()


def test_plain_value_passes_non_text_keys_through_by_default() -> None:
    """The schema layer accepts non-text keys, so rendering and plan writing must too."""
    assert plain_value({1: "a", "ok": "b"}) == {1: "a", "ok": "b"}


def test_plain_value_strict_rejects_a_non_text_key() -> None:
    """Strict mode is the planner's fallback trigger: a key it cannot serialize means
    the agentic grouping input is unusable."""
    with pytest.raises(TypeError, match="must be text"):
        plain_value({1: "a"}, strict=True)


def test_prompt_renders_a_slice_with_a_non_text_key() -> None:
    payload = {
        "anchor": "settings",
        "anchored_slice": {1: "a", "theme": "plain"},
        "comments": [],
        "content_hash": "sha256:dd",
    }
    assert "theme" in prompt_for_payload(payload)
