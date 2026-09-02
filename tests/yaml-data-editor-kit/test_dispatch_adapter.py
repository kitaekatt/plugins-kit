from pathlib import Path

from yaml_data_editor_kit.dispatch.adapter import SYSTEM_PROMPT, adapter_for, prompt_for
from yaml_data_editor_kit.dispatch.state import write_plan


def test_adapter_identity_is_digest_derived(tmp_path: Path) -> None:
    plan = write_plan(tmp_path / "plan.yaml", run_id="dispatch-1", corpus_path=tmp_path, comment_store_path=tmp_path / "comments", units=[{"id": "unit-a", "payload": {"anchor": "a", "anchored_slice": {"x": 1}, "comments": ["Do it"], "content_hash": "h"}}])
    adapter = adapter_for(plan)
    assert adapter.adapter_version == plan.adapter_version
    assert adapter.unit_for("unit-a").id == "unit-a"


def test_prompt_matches_inline_shape() -> None:
    assert "anchor: a" in prompt_for({"anchor": "a", "anchored_slice": {"x": 1}, "comments": ["Do it"], "content_hash": "h"})
    assert SYSTEM_PROMPT == "Transform the anchored slice according to the comments. Return only the result."
