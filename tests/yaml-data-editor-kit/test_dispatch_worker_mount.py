"""Contract tests for the plan-authenticated worker mount."""

from pathlib import Path

import pytest

from content_pipeline.execution.store import ExecutionStore
from content_pipeline.execution.model import AttemptKind
from content_pipeline.freshness.hashing import content_hash
from content_pipeline.execution.workerpack import enumerate_worker_invocations
from content_pipeline.execution.workerpack import format_fenced_answer
from content_pipeline.execution.workerpack import parse_fenced_answer
from content_pipeline.execution.workerpack import AnswerFenceMismatchError

from yaml_data_editor_kit.comments.store import CommentStore
from yaml_data_editor_kit.dispatch.state import write_plan
from yaml_data_editor_kit.dispatch.worker_mount import build_worker_command
from yaml_data_editor_kit.dispatch.worker_mount import _runtime


def test_worker_command_has_exact_plan_first_template(tmp_path: Path) -> None:
    plan = tmp_path / "run" / "dispatch-plan.yaml"
    worker = tmp_path / "run" / "workers"
    command = build_worker_command(plan, worker)

    assert command.argv[1:] == (
        "-m",
        "yaml_data_editor_kit.dispatch.worker_mount",
        str(plan.resolve()),
    )
    assert command.answer_dir == str((worker / "answers").resolve())
    assert command.envelope_dir == str((worker / "envelopes").resolve())


def test_worker_invocations_have_no_claim_and_use_text_file_equals(tmp_path: Path) -> None:
    command = build_worker_command(
        tmp_path / "dispatch-plan.yaml", tmp_path / "workers"
    )
    invocations = enumerate_worker_invocations(command, "run-1", "unit-1", "worker-1")

    assert len(invocations) == 6
    assert " protocol @" in invocations[0]
    assert " protocol @" in invocations[1]
    assert "--text-file=" in invocations[1]
    assert " protocol @" in invocations[2]
    assert all(" claim " not in invocation for invocation in invocations[:3])
    assert invocations[3].startswith("Write tool -> ")
    assert invocations[4].startswith("Write tool -> ")
    assert invocations[5].startswith("Write tool -> ")


def _mount_fixture(
    tmp_path: Path, write, *, comment_guard: str | None = None, claim_worker: str | None = "worker-1"
):
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
    comments_path = tmp_path / "comments"
    CommentStore.init(comments_path)
    target = {
        "id": "record:product/bolt",
        "anchor": "product/bolt",
        "anchored_slice": {"id": "bolt", "summary": "fastener"},
        "content_hash": content_hash({"id": "bolt", "summary": "fastener"}),
        "comment_anchors": [] if comment_guard is None else ["product/bolt/id"],
        "comment_guards": [] if comment_guard is None else [comment_guard],
        "comment_ids": ["summary-note"],
        "comments": ["Make it precise."],
    }
    execution_path = tmp_path / "execution.sqlite3"
    execution = ExecutionStore(execution_path)
    run_id = "run-1"
    plan_path = tmp_path / "dispatch-plan.yaml"
    plan = write_plan(
        plan_path,
        run_id=run_id,
        corpus_path=tmp_path,
        comment_store_path=comments_path,
        units=[{"id": "unit-1", "payload": target}],
    )
    execution.create_run(
        run_id, driver="claude_bg", backend="mock", model="",
        adapter_version=plan.adapter_version,
    )
    execution.register_units(run_id, ["unit-1"])
    loaded_plan, loaded_execution, adapter, handlers = _runtime(plan_path)
    if claim_worker is None:
        return plan_path, loaded_execution, handlers, None, target
    claim = handlers["claim"]({
        "run_id": run_id, "unit_id": "unit-1", "worker_id": claim_worker,
    })
    return plan_path, loaded_execution, handlers, claim["fencing_token"], target


def _submit_payload(token: int, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "run_id": "run-1",
        "unit_id": "unit-1",
        "worker_id": "worker-1",
        "fencing_token": token,
        "text": "updated fastener",
    }
    payload.update(overrides)
    return payload


def test_claim_authenticates_unit_run_and_plan_before_writing(tmp_path: Path, write) -> None:
    plan_path, execution, handlers, _token, _target = _mount_fixture(tmp_path, write)
    execution_before = (tmp_path / "execution.sqlite3").read_bytes()

    with pytest.raises(KeyError, match="missing"):
        handlers["claim"]({
            "run_id": "run-1", "unit_id": "missing", "worker_id": "worker-1",
        })
    with pytest.raises(ValueError, match="run_id"):
        handlers["claim"]({
            "run_id": "wrong-run", "unit_id": "unit-1", "worker_id": "worker-1",
        })
    plan_path.write_text(plan_path.read_text(encoding="utf-8").replace(
        "run_id: run-1", "run_id: tampered-run", 1
    ), encoding="utf-8")
    with pytest.raises(ValueError, match="digest"):
        _runtime(plan_path)

    assert (tmp_path / "execution.sqlite3").read_bytes() == execution_before
    assert execution.get_unit("run-1", "unit-1").claimed_by == "worker-1"


def test_claim_returns_fence_and_lease_for_valid_direct_claim(tmp_path: Path, write) -> None:
    _plan_path, _execution, handlers, _token, _target = _mount_fixture(
        tmp_path, write, claim_worker=None
    )
    claim = handlers["claim"]({"run_id": "run-1", "unit_id": "unit-1", "worker_id": "worker-1"})
    assert isinstance(claim["fencing_token"], int)
    assert claim["lease_expires_at"] > 0


def test_wrong_worker_read_is_rejected(tmp_path: Path, write) -> None:
    _plan_path, _execution, handlers, _token, _target = _mount_fixture(tmp_path, write)
    with pytest.raises(ValueError, match="active claimant"):
        handlers["read"]({
            "run_id": "run-1", "unit_id": "unit-1", "worker_id": "worker-2",
        })


def test_stale_comment_guard_terminally_fails_read(tmp_path: Path, write) -> None:
    _plan_path, execution, handlers, _token, _target = _mount_fixture(
        tmp_path, write, comment_guard="not-the-saved-guard"
    )
    with pytest.raises(ValueError, match="^stale:"):
        handlers["read"]({
            "run_id": "run-1", "unit_id": "unit-1", "worker_id": "worker-1",
        })
    assert execution.get_unit("run-1", "unit-1").state.value == "failed"


def test_submit_matching_fences_accept_answer_text(tmp_path: Path, write) -> None:
    _plan_path, execution, handlers, token, _target = _mount_fixture(tmp_path, write)
    answer = format_fenced_answer(token, "updated fastener")
    text = parse_fenced_answer(answer, token)
    result = handlers["submit"](_submit_payload(token, text=text))
    assert result["accepted"] is True
    assert execution.get_unit("run-1", "unit-1").accepted_text == "updated fastener"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"fencing_token": 99}, "fencing token"),
        ({"worker_id": "worker-2"}, "active claimant"),
    ],
)
def test_submit_stale_fence_or_wrong_owner_writes_no_acceptance(
    tmp_path: Path, write, overrides: dict[str, object], message: str
) -> None:
    _plan_path, execution, handlers, token, _target = _mount_fixture(tmp_path, write)
    with pytest.raises(ValueError, match=message):
        handlers["submit"](_submit_payload(token, **overrides))
    unit = execution.get_unit("run-1", "unit-1")
    assert unit.accepted_text is None
    assert unit.state.value == "claimed"


def test_submit_answer_fence_mismatch_writes_no_acceptance(tmp_path: Path, write) -> None:
    _plan_path, execution, handlers, token, _target = _mount_fixture(tmp_path, write)
    with pytest.raises(AnswerFenceMismatchError, match="different claim"):
        parse_fenced_answer(format_fenced_answer(token + 1, "updated fastener"), token)
    assert execution.get_unit("run-1", "unit-1").accepted_text is None
    assert handlers["read"]({
        "run_id": "run-1", "unit_id": "unit-1", "worker_id": "worker-1",
    })["unit_id"] == "unit-1"


def test_stale_submit_terminally_fails_without_attributed_result(tmp_path: Path, write) -> None:
    _plan_path, execution, handlers, token, _target = _mount_fixture(tmp_path, write)
    attributed = tmp_path / "attributed.yaml"
    attributed.write_bytes(b"records: {}\n")
    before = attributed.read_bytes()
    (tmp_path / "content/products.yaml").write_text(
        "- {id: bolt, summary: changed}\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="^stale:"):
        handlers["submit"](_submit_payload(token))
    assert execution.get_unit("run-1", "unit-1").state.value == "failed"
    assert execution.get_unit("run-1", "unit-1").accepted_text is None
    assert attributed.read_bytes() == before


@pytest.mark.parametrize(
    "overrides",
    [{"terminal": True, "error": "   "}, {"terminal": False, "error": "worker failed"}],
)
def test_fail_rejects_empty_detail_and_nonterminal_requests(
    tmp_path: Path, write, overrides: dict[str, object]
) -> None:
    _plan_path, execution, handlers, token, _target = _mount_fixture(tmp_path, write)
    with pytest.raises(ValueError):
        handlers["fail"]({
            "run_id": "run-1", "unit_id": "unit-1", "worker_id": "worker-1",
            "fencing_token": token, **overrides,
        })
    assert execution.get_unit("run-1", "unit-1").state.value == "claimed"


def test_valid_fail_creates_terminal_fail_attempt(tmp_path: Path, write) -> None:
    _plan_path, execution, handlers, token, _target = _mount_fixture(tmp_path, write)
    handlers["fail"]({
        "run_id": "run-1", "unit_id": "unit-1", "worker_id": "worker-1",
        "fencing_token": token, "terminal": True, "error": "worker failed",
    })
    assert execution.get_unit("run-1", "unit-1").state.value == "failed"
    attempt = execution.list_attempts("run-1", "unit-1")[-1]
    assert attempt.kind is AttemptKind.FAIL
    assert attempt.error == "worker failed"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"worker_id": "worker-2"}, "active claimant"),
        ({"fencing_token": 99}, "fencing token"),
    ],
)
def test_fail_authenticates_owner_and_fence_before_writing(
    tmp_path: Path, write, overrides: dict[str, object], message: str
) -> None:
    _plan_path, execution, handlers, token, _target = _mount_fixture(tmp_path, write)
    with pytest.raises(ValueError, match=message):
        handlers["fail"]({
            "run_id": "run-1", "unit_id": "unit-1", "worker_id": "worker-1",
            "fencing_token": token, "terminal": True, "error": "worker failed",
            **overrides,
        })
    assert execution.get_unit("run-1", "unit-1").state.value == "claimed"


def test_submit_preserves_editor_files(tmp_path: Path, write) -> None:
    _plan_path, _execution, handlers, token, _target = _mount_fixture(tmp_path, write)
    corpus = tmp_path / "content/products.yaml"
    comments = {
        path.name: path.read_bytes() for path in (tmp_path / "comments").glob("*.yaml")
    }
    attributed = tmp_path / "attributed.yaml"
    attributed.write_bytes(b"records: {}\n")
    before = (corpus.read_bytes(), comments, attributed.read_bytes())
    handlers["submit"](_submit_payload(token))
    assert corpus.read_bytes() == before[0]
    assert {
        path.name: path.read_bytes() for path in (tmp_path / "comments").glob("*.yaml")
    } == before[1]
    assert attributed.read_bytes() == before[2]


def test_read_returns_canonical_prompts_and_fail_keeps_editor_files_unchanged(tmp_path, write) -> None:
    _plan_path, execution, handlers, token, _target = _mount_fixture(tmp_path, write)
    result = handlers["read"]({
        "run_id": "run-1", "unit_id": "unit-1", "worker_id": "worker-1",
    })
    assert result["system"] == "Transform the anchored slice according to the comments. Return only the result."
    assert "Make it precise." in result["user"]

    corpus_before = (tmp_path / "content/products.yaml").read_bytes()
    comments_before = {
        path.name: path.read_bytes()
        for path in (tmp_path / "comments").glob("*.yaml")
    }
    attributed = tmp_path / "attributed.yaml"
    attributed.write_bytes(b"records: {}\n")
    attributed_before = attributed.read_bytes()
    handlers["fail"]({
        "run_id": "run-1", "unit_id": "unit-1", "worker_id": "worker-1",
        "fencing_token": token, "terminal": True, "error": "worker failed",
    })
    assert (tmp_path / "content/products.yaml").read_bytes() == corpus_before
    assert {
        path.name: path.read_bytes()
        for path in (tmp_path / "comments").glob("*.yaml")
    } == comments_before
    assert attributed.read_bytes() == attributed_before
    assert execution.get_unit("run-1", "unit-1").state.value == "failed"


def test_stale_read_terminally_fails_without_editor_store_writes(tmp_path, write) -> None:
    _plan_path, execution, handlers, _token, _target = _mount_fixture(tmp_path, write)
    corpus = tmp_path / "content/products.yaml"
    corpus_before = corpus.read_bytes()
    comments_before = {
        path.name: path.read_bytes()
        for path in (tmp_path / "comments").glob("*.yaml")
    }
    attributed = tmp_path / "attributed.yaml"
    attributed.write_bytes(b"records: {}\n")
    attributed_before = attributed.read_bytes()
    corpus.write_text("- {id: bolt, summary: changed}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="^stale:"):
        handlers["read"]({
            "run_id": "run-1", "unit_id": "unit-1", "worker_id": "worker-1",
        })
    assert execution.get_unit("run-1", "unit-1").state.value == "failed"
    assert corpus.read_bytes() != corpus_before
    assert {
        path.name: path.read_bytes()
        for path in (tmp_path / "comments").glob("*.yaml")
    } == comments_before
    assert attributed.read_bytes() == attributed_before
