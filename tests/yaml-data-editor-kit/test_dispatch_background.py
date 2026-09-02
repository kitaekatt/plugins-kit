from pathlib import Path

import pytest
import yaml

from content_pipeline.llm.backends import MockBackend
from content_pipeline.execution.model import AttemptKind, UnitState
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


def test_generation_time_stale_failure_is_counted_stale(tmp_path: Path, write) -> None:
    """Staleness arrives on TWO axes and both must count. A worker that
    terminally FAILS a unit as stale during generation (worker_mount's
    stale_fail writes "stale:<anchor>") is a different path from the apply
    boundary refusing an accepted unit at finalize. Deriving status from the
    apply axis alone silently drops the first."""
    prepared = prepare_background_dispatch(_two_unit_request(tmp_path, write))
    execution = ExecutionStore(prepared.execution_store)
    units = execution.list_units(prepared.run_id)

    claim = execution.claim_unit(prepared.run_id, units[0].unit_id, "worker-0")
    execution.fail_unit(
        prepared.run_id,
        units[0].unit_id,
        claim.fencing_token,
        error="stale:product/bolt/summary",
        terminal=True,
    )

    status = get_background_dispatch_status(prepared)
    assert units[0].unit_id in status.stale, (
        "a generation-time stale failure must count toward stale; only the "
        "apply-time axis was being counted"
    )


def test_apply_rejected_unit_still_counts_as_accepted(tmp_path: Path, write) -> None:
    """An apply rejection does not undo the SUBMIT verdict -- the unit stays in
    UnitState.ACCEPTED by design, so it must count toward `accepted`. Omitting
    it made this lane disagree with the inline lane on the same scenario."""
    prepared = prepare_background_dispatch(_two_unit_request(tmp_path, write))
    execution = ExecutionStore(prepared.execution_store)
    units = execution.list_units(prepared.run_id)

    for index, unit in enumerate(units):
        claim = execution.claim_unit(prepared.run_id, unit.unit_id, "worker-{}".format(index))
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
    assert summary.stale == 1
    assert summary.applied == 1
    # Both units passed submit-time adjudication; one was refused at apply.
    assert summary.accepted == 2, (
        "an apply-rejected unit is still ACCEPTED -- acceptance and apply "
        "rejection are separate axes"
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
    assert summary.statuses["record:product/nut"] == "stale"
    assert summary.statuses["record:product/bolt"] == "applied"

    stale_unit = execution.get_unit(prepared.run_id, "record:product/nut")
    assert stale_unit is not None
    assert stale_unit.state is UnitState.ACCEPTED
    stale_attempts = execution.list_attempts(prepared.run_id, "record:product/nut")
    assert stale_attempts[-1].kind is AttemptKind.APPLY_REJECTED
    assert stale_attempts[-1].error.startswith("stale:")

    records = yaml.safe_load(prepared.attributed_store.read_text(encoding="utf-8"))["records"]
    assert records["record:product/bolt"]["machine"] == "short bolt summary"
    assert "machine" not in records.get("record:product/nut", {})

    before_repeat = prepared.attributed_store.read_bytes()
    attempts_before_repeat = execution.list_attempts(prepared.run_id, "record:product/nut")
    repeated = finalize_background_dispatch(prepared)
    assert repeated.rejected == 1
    assert repeated.stale == 1
    assert repeated.applied == 1
    assert prepared.attributed_store.read_bytes() == before_repeat
    assert execution.list_attempts(prepared.run_id, "record:product/nut") == attempts_before_repeat
