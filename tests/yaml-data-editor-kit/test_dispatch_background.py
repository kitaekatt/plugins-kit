from pathlib import Path

import pytest
import yaml

from content_pipeline.llm.backends import MockBackend
from content_pipeline.execution.store import ExecutionStore
from yaml_data_editor_kit.comments import Comment, CommentStore
from yaml_data_editor_kit.dispatch import (
    BackgroundStagesRequiredError,
    DispatchRequest,
    PreparedBackgroundDispatch,
    dispatch,
    get_background_dispatch_status,
    finalize_background_dispatch,
    load_background_dispatch,
    prepare_background_dispatch,
)
from yaml_data_editor_kit.schema import load_corpus, load_profile


def _request(tmp_path: Path, write) -> DispatchRequest:
    write("profile/catalogue.yaml", """
dialect: type/1
id: product
identified_by: id
fields:
  id: {type: id}
  summary: {type: text}
---
dialect: source/1
of: product
layout: rows
path: content/products.yaml
""")
    write("content/products.yaml", "- {id: bolt, summary: fastener}\n")
    profile = load_profile(tmp_path / "profile")
    corpus = load_corpus(profile, tmp_path)
    comments = CommentStore.init(tmp_path / "comments")
    comments.write(Comment.create(profile, corpus, id="note", anchor="product/bolt/summary", text="Shorten.", created="2026-08-30"))
    return DispatchRequest(tmp_path, tmp_path / "comments", tmp_path / "run", driver="claude_bg")


def test_prepare_is_durable_and_load_reproduces_identity(tmp_path: Path, write) -> None:
    prepared = prepare_background_dispatch(_request(tmp_path, write))
    assert prepared.plan_path.exists()
    assert prepared.execution_store.exists()
    assert prepared.attributed_store.exists()
    loaded = load_background_dispatch(prepared.run_dir)
    assert loaded == prepared


def test_background_request_keeps_inline_error_compatibility(tmp_path: Path, write) -> None:
    request = _request(tmp_path, write)
    with pytest.raises(BackgroundStagesRequiredError) as raised:
        dispatch(request, backend=MockBackend(responses=["unused"]))
    assert isinstance(raised.value, NotImplementedError)
    assert "prepare_background_dispatch" in str(raised.value)


def test_status_reports_planned_units(tmp_path: Path, write) -> None:
    prepared = prepare_background_dispatch(_request(tmp_path, write))
    status = get_background_dispatch_status(prepared)
    assert status == get_background_dispatch_status(PreparedBackgroundDispatch(**prepared.__dict__))
    assert status.planned == 1
    assert status.states == {"record:product/bolt": "pending"}


def _two_unit_request(tmp_path: Path, write) -> DispatchRequest:
    write("profile/catalogue.yaml", """
dialect: type/1
id: product
identified_by: id
fields:
  id: {type: id}
  summary: {type: text}
---
dialect: source/1
of: product
layout: rows
path: content/products.yaml
""")
    write(
        "content/products.yaml",
        "- {id: bolt, summary: fastener}\n- {id: nut, summary: hardware}\n",
    )
    profile = load_profile(tmp_path / "profile")
    corpus = load_corpus(profile, tmp_path)
    comments = CommentStore.init(tmp_path / "comments")
    comments.write(
        Comment.create(
            profile,
            corpus,
            id="note-bolt",
            anchor="product/bolt/summary",
            text="Shorten the bolt summary.",
            created="2026-08-30",
        )
    )
    comments.write(
        Comment.create(
            profile,
            corpus,
            id="note-nut",
            anchor="product/nut/summary",
            text="Shorten the nut summary.",
            created="2026-08-30",
        )
    )
    return DispatchRequest(tmp_path, tmp_path / "comments", tmp_path / "run", driver="claude_bg")


@pytest.mark.xfail(
    reason=(
        "KNOWN GAP: a unit that goes stale between acceptance and finalize cannot be "
        "settled -- ACCEPTED is terminal in the execution store, so fail_unit refuses "
        "the transition. finalize raises StaleAtFinalizeError naming every stale unit "
        "instead of applying the healthy ones. Closing this needs a settle-an-accepted-"
        "unit transition in content-pipeline-kit, which is a state-machine change to a "
        "published plugin and is escalated rather than taken unilaterally."
    ),
    strict=True,
)
def test_finalize_rejects_one_stale_accepted_unit_and_remains_repeatable(
    tmp_path: Path, write
) -> None:
    prepared = prepare_background_dispatch(_two_unit_request(tmp_path, write))
    execution = ExecutionStore(prepared.execution_store)
    units = execution.list_units(prepared.run_id)

    for index, unit in enumerate(units):
        claim = execution.claim_unit(prepared.run_id, unit.unit_id, "test-worker-{}".format(index))
        execution.accept_unit(
            prepared.run_id,
            unit.unit_id,
            claim.fencing_token,
            text="short {} summary".format("bolt" if index == 0 else "nut"),
        )

    (tmp_path / "content/products.yaml").write_text(
        "- {id: bolt, summary: fastener}\n- {id: nut, summary: changed}\n",
        encoding="utf-8",
    )

    summary = finalize_background_dispatch(prepared)
    assert summary.rejected == 1
    assert summary.stale == 1
    assert summary.applied == 1
    assert summary.statuses["record:product/nut"] == "failed"
    assert summary.statuses["record:product/bolt"] == "applied"

    records = yaml.safe_load(prepared.attributed_store.read_text(encoding="utf-8"))["records"]
    assert records["record:product/bolt"]["machine"] == "short bolt summary"
    assert "machine" not in records.get("record:product/nut", {})

    repeated = finalize_background_dispatch(prepared)
    assert repeated.rejected == 1
    assert repeated.stale == 1
    assert repeated.applied == 1
