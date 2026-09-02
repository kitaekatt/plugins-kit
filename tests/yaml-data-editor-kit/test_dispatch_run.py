"""End-to-end tests for the inline dispatch lane."""

from pathlib import Path

import yaml

from content_pipeline.execution.model import UnitState
from content_pipeline.execution.store import ExecutionStore
from content_pipeline.llm.backends import MockBackend

from yaml_data_editor_kit.comments import Comment, CommentStore
from yaml_data_editor_kit.dispatch.request import DispatchRequest
from yaml_data_editor_kit.dispatch.run import dispatch
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
