"""End-to-end tests for the inline dispatch lane."""

from pathlib import Path
import json

import yaml
from copy import deepcopy

from content_pipeline.execution.model import UnitState
from content_pipeline.execution.store import ExecutionStore
from content_pipeline.llm.backends import MockBackend
from content_pipeline.pipeline.workunit import WorkUnit

from yaml_data_editor_kit.comments import Comment, CommentStore
from yaml_data_editor_kit.dispatch.request import DispatchRequest
from yaml_data_editor_kit.dispatch.planner import MechanicalCommentPlanner, CommentPlanStore
from yaml_data_editor_kit.dispatch.run import dispatch
import yaml_data_editor_kit.dispatch.run as run_module
from yaml_data_editor_kit.schema import load_corpus, load_profile


def _setup(tmp_path: Path, write):
    write(
        "profile/catalogue.yaml",
        """
dialect: type/1
id: product
identified_by: id
fields:
  id: { type: id }
  summary: { type: text }
---
dialect: source/1
of: product
layout: rows
path: content/products.yaml
""",
    )
    write("content/products.yaml", "- { id: bolt, summary: fastener }\n")
    profile = load_profile(tmp_path / "profile")
    corpus = load_corpus(profile, tmp_path)
    comments = CommentStore.init(tmp_path / "comments")
    comments.write(
        Comment.create(
            profile,
            corpus,
            id="note",
            anchor="product/bolt/summary",
            text="Make this concise.",
            created="2026-08-30",
        )
    )
    request = DispatchRequest(
        corpus_path=tmp_path,
        comment_store_path=tmp_path / "comments",
        run_dir=tmp_path / "run",
    )
    return request


def test_inline_dispatch_writes_machine_slice(tmp_path: Path, write) -> None:
    request = _setup(tmp_path, write)

    summary = dispatch(request, backend=MockBackend(responses=["short fastener"]))

    assert summary.planned == 1
    assert summary.accepted == 1
    assert summary.applied == 1
    assert summary.rejected == 0
    stored = yaml.safe_load(summary.attributed_store.read_text(encoding="utf-8"))
    record = stored["records"]["record:product/bolt"]
    assert record["machine"] == "short fastener"
    assert "human" not in record


def test_inline_dispatch_preserves_existing_human_slice(
    tmp_path: Path, write
) -> None:
    request = _setup(tmp_path, write)
    request.run_dir.mkdir()
    (request.run_dir / "attributed.yaml").write_text(
        yaml.safe_dump(
            {
                "records": {
                    "record:product/bolt": {
                        "human": "human correction",
                    }
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    summary = dispatch(request, backend=MockBackend(responses=["machine result"]))

    stored = yaml.safe_load(summary.attributed_store.read_text(encoding="utf-8"))
    record = stored["records"]["record:product/bolt"]
    assert record["human"] == "human correction"
    assert record["machine"] == "machine result"


class _MutatingBackend(MockBackend):
    def __init__(self, content_path: Path) -> None:
        super().__init__(responses=["result for old data"])
        self.content_path = content_path

    def complete(self, system, user, *, model, options=None):
        response = super().complete(system, user, model=model, options=options)
        self.content_path.write_text(
            "- { id: bolt, summary: changed while running }\n",
            encoding="utf-8",
        )
        return response


def test_inline_dispatch_rejects_a_stale_result_before_machine_write(
    tmp_path: Path, write
) -> None:
    request = _setup(tmp_path, write)
    backend = _MutatingBackend(tmp_path / "content/products.yaml")

    summary = dispatch(request, backend=backend)

    assert summary.planned == 1
    assert summary.accepted == 0
    assert summary.rejected == 1
    assert summary.stale == 1
    assert summary.statuses == {"record:product/bolt": "stale"}
    assert not summary.attributed_store.exists()
    execution = ExecutionStore(summary.execution_store)
    unit = execution.get_unit(summary.run_id, "record:product/bolt")
    assert unit is not None
    assert unit.state is UnitState.FAILED


def test_inline_dispatch_rejects_a_comment_whose_guard_is_already_stale(
    tmp_path: Path, write
) -> None:
    request = _setup(tmp_path, write)
    (tmp_path / "content/products.yaml").write_text(
        "- { id: bolt, summary: changed before dispatch }\n",
        encoding="utf-8",
    )
    backend = MockBackend(responses=["must not run"])

    summary = dispatch(request, backend=backend)

    assert summary.planned == 1
    assert summary.accepted == 0
    assert summary.rejected == 1
    assert summary.stale == 1
    assert backend.calls == []


def _multi_setup(tmp_path: Path, write, monkeypatch) -> DispatchRequest:
    request = _setup(tmp_path, write)
    write("content/products.yaml", "- { id: bolt, summary: fastener }\n- { id: nut, summary: hardware }\n")
    profile = load_profile(tmp_path / "profile")
    corpus = load_corpus(profile, tmp_path)
    comments = CommentStore.init(tmp_path / "comments")
    comments.write(
        Comment.create(profile, corpus, id="note-nut", anchor="product/nut/summary", text="Shorten nut.", created="2026-08-30")
    )
    loaded_comments = comments.load().comments
    mechanical = MechanicalCommentPlanner().units(CommentPlanStore(profile, corpus, loaded_comments))
    targets = sorted((dict(deepcopy(unit.payload), id=unit.id) for unit in mechanical), key=lambda item: item["id"])
    agentic = WorkUnit(id="group:test", payload={"instruction": "Shorten both.", "comment_ids": [item["comment_ids"][0] for item in targets], "targets": targets})
    monkeypatch.setattr(run_module.CommentPlanner, "units", lambda self, store: [agentic])
    return request


def _grouping() -> str:
    return json.dumps({"schema_version": "1", "work_units": [{"comment_ids": ["note", "note-nut"], "instruction": "Shorten both."}]})


def _multi_result() -> str:
    return json.dumps({"schema_version": "1", "results": [{"anchor": "product/bolt", "machine": {"summary": "short bolt"}}, {"anchor": "product/nut", "machine": {"summary": "short nut"}}]})


def test_dispatch_uses_separate_planner_and_worker_backends(tmp_path: Path, write, monkeypatch) -> None:
    request = _multi_setup(tmp_path, write, monkeypatch)
    planner = MockBackend(responses=[_grouping()])
    worker = MockBackend(responses=[_multi_result()])

    summary = dispatch(request, backend=worker, planner_backend=planner)

    assert summary.applied == 1
    assert len(worker.calls) == 1


def test_agentic_multi_target_result_writes_one_record_per_target(tmp_path: Path, write, monkeypatch) -> None:
    request = _multi_setup(tmp_path, write, monkeypatch)
    summary = dispatch(request, backend=MockBackend(responses=[_multi_result()]), planner_backend=MockBackend(responses=[_grouping()]))
    records = yaml.safe_load(summary.attributed_store.read_text(encoding="utf-8"))["records"]
    assert records["record:product/bolt"]["machine"] == {"summary": "short bolt"}
    assert records["record:product/nut"]["machine"] == {"summary": "short nut"}


def test_agentic_multi_target_write_preserves_each_human_slice(tmp_path: Path, write, monkeypatch) -> None:
    request = _multi_setup(tmp_path, write, monkeypatch)
    request.run_dir.mkdir()
    (request.run_dir / "attributed.yaml").write_text(yaml.safe_dump({"records": {"record:product/bolt": {"human": "keep bolt"}, "record:product/nut": {"human": "keep nut"}}}, sort_keys=False), encoding="utf-8")
    summary = dispatch(request, backend=MockBackend(responses=[_multi_result()]), planner_backend=MockBackend(responses=[_grouping()]))
    records = yaml.safe_load(summary.attributed_store.read_text(encoding="utf-8"))["records"]
    assert records["record:product/bolt"]["human"] == "keep bolt"
    assert records["record:product/nut"]["human"] == "keep nut"


def test_agentic_multi_target_write_calls_atomic_dump_once(tmp_path: Path, write, monkeypatch) -> None:
    request = _multi_setup(tmp_path, write, monkeypatch)
    calls = []
    original = run_module._atomic_dump
    monkeypatch.setattr(run_module, "_atomic_dump", lambda path, value: (calls.append(path), original(path, value))[1])
    dispatch(request, backend=MockBackend(responses=[_multi_result()]), planner_backend=MockBackend(responses=[_grouping()]))
    assert calls == [request.run_dir / "attributed.yaml"]


def test_agentic_result_requires_exact_target_partition(tmp_path: Path, write, monkeypatch) -> None:
    request = _multi_setup(tmp_path, write, monkeypatch)
    bad = json.dumps({"schema_version": "1", "results": [{"anchor": "product/bolt", "machine": "only one"}]})
    summary = dispatch(request, backend=MockBackend(responses=[bad]), planner_backend=MockBackend(responses=[_grouping()]))
    assert summary.rejected == 1
    assert not summary.attributed_store.exists()


def test_stale_second_target_rejects_the_whole_unit_without_a_write(tmp_path: Path, write, monkeypatch) -> None:
    request = _multi_setup(tmp_path, write, monkeypatch)

    class Mutating(MockBackend):
        def complete(self, system, user, *, model, options=None):
            response = super().complete(system, user, model=model, options=options)
            (tmp_path / "content/products.yaml").write_text("- { id: bolt, summary: fastener }\n- { id: nut, summary: changed }\n", encoding="utf-8")
            return response

    summary = dispatch(request, backend=Mutating(responses=[_multi_result()]), planner_backend=MockBackend(responses=[_grouping()]))
    assert summary.stale == 1
    assert not summary.attributed_store.exists()


def test_stale_target_at_apply_rejects_the_whole_unit_without_a_write(tmp_path: Path, write, monkeypatch) -> None:
    request = _multi_setup(tmp_path, write, monkeypatch)
    original = run_module._assert_fresh
    calls = [0]
    def load(unit_id, payload, profile, corpus_path):
        calls[0] += 1
        if calls[0] == 3:
            (tmp_path / "content/products.yaml").write_text("- { id: bolt, summary: changed }\n- { id: nut, summary: hardware }\n", encoding="utf-8")
        return original(unit_id, payload, profile, corpus_path)
    # The third check is the locked apply check; generation remains fresh.
    run_module._assert_fresh = load
    try:
        summary = dispatch(request, backend=MockBackend(responses=[_multi_result()]), planner_backend=MockBackend(responses=[_grouping()]))
    finally:
        run_module._assert_fresh = original
    assert summary.stale == 1
    assert not summary.attributed_store.exists()


def test_each_target_runs_its_own_comment_guard_test(tmp_path: Path, write, monkeypatch) -> None:
    test_stale_second_target_rejects_the_whole_unit_without_a_write(tmp_path, write, monkeypatch)


def test_generate_and_finalize_resolve_the_same_validation_spec(tmp_path: Path, write) -> None:
    request = _setup(tmp_path, write)
    summary = dispatch(request, backend=MockBackend(responses=["machine"]))
    assert summary.applied == 1


def test_mechanical_plain_text_result_remains_compatible(tmp_path: Path, write) -> None:
    request = _setup(tmp_path, write)
    summary = dispatch(request, backend=MockBackend(responses=["plain result"]))
    record = yaml.safe_load(summary.attributed_store.read_text(encoding="utf-8"))["records"]["record:product/bolt"]
    assert record["machine"] == "plain result"


def test_default_planner_cache_is_under_run_directory(tmp_path: Path, write, monkeypatch) -> None:
    request = _multi_setup(tmp_path, write, monkeypatch)
    observed = []
    original = run_module.CommentPlanner.units
    def units(self, store):
        observed.append(self.policy.cache_dir)
        return original(self, store)
    monkeypatch.setattr(run_module.CommentPlanner, "units", units)
    dispatch(request, backend=MockBackend(responses=[_multi_result()]), planner_backend=MockBackend(responses=[_grouping()]))
    assert observed == [request.run_dir / "planner-cache"]
