from __future__ import annotations

from dataclasses import dataclass

from bootstrap_lib.code_review.mechanical import MechanicalSnapshot
from bootstrap_lib.code_review.mechanical_repository import (
    CheckOutcome,
    PathEffect,
    ReadResult,
    RepositoryRequest,
    SourceRequests,
    StatResult,
    scan_repository,
)
import bootstrap_lib.code_review.mechanical_repository as repository


def _source(file: str = "docs/a.md") -> MechanicalSnapshot:
    return MechanicalSnapshot(file, "diff", "", ((1, ""),), (), "[x](target.md)\n")


class Reader:
    def __init__(self) -> None:
        self.stat_calls: list[tuple[str, ...]] = []
        self.read_calls: list[tuple[str, ...]] = []

    def stat(self, paths: tuple[str, ...]):
        self.stat_calls.append(paths)
        return {path: StatResult("missing") for path in paths}

    def read(self, paths: tuple[str, ...]):
        self.read_calls.append(paths)
        return {path: ReadResult("missing") for path in paths}


@dataclass(frozen=True)
class Check:
    check_id: str = "test"
    phrase: str = "test phrase"

    def collect(self, sources):
        return {
            source: SourceRequests((RepositoryRequest("target.md"),))
            for source in sources
        }

    def evaluate(self, sources, view, requests):
        stats = view.stat_many(("target.md",))
        assert stats["target.md"].kind == "file"
        return {source: CheckOutcome(True) for source in requests}


def test_overlay_wins_and_deduplicates_reader_queries() -> None:
    reader = Reader()
    sources = {"docs/a.md": _source(), "docs/b.md": _source("docs/b.md")}
    records, identity = scan_repository(
        sources,
        snapshot_seed="git:base:post:digest",
        path_effects=(PathEffect("target.md", "add", "review", b"ok"),),
        reader=reader,
        checks=(Check(),),
    )
    assert reader.stat_calls == []
    assert records["docs/a.md"]["checks_run"] == ["test"]
    assert records["docs/b.md"]["checks_run"] == ["test"]
    assert identity is not None


def test_unavailable_snapshot_diagnoses_only_eligible_sources() -> None:
    class MarkdownOnly(Check):
        def collect(self, sources):
            return {"docs/a.md": SourceRequests()}

    records, identity = scan_repository(
        {"docs/a.md": _source(), "src/a.py": _source("src/a.py")},
        snapshot_seed=None,
        path_effects=(),
        reader=None,
        checks=(MarkdownOnly(),),
    )
    assert records["docs/a.md"]["diagnostics"] == ["repository snapshot unavailable"]
    assert "diagnostics" not in records["src/a.py"]
    assert identity is None


def test_view_rejects_queries_not_declared_during_collection() -> None:
    class Bad(Check):
        def evaluate(self, sources, view, requests):
            view.stat_many(("surprise.md",))
            raise AssertionError("unreachable")

    records, _ = scan_repository(
        {"docs/a.md": _source()},
        snapshot_seed="seed",
        path_effects=(),
        reader=Reader(),
        checks=(Bad(),),
    )
    assert records["docs/a.md"]["checks_run"] == []
    assert "was not collected" in records["docs/a.md"]["diagnostics"][0]


def test_same_change_delete_tombstone_breaks_added_link_before_base_lookup() -> None:
    source = MechanicalSnapshot(
        "docs/guide.md",
        "diff",
        "",
        ((1, "[read](../README.md)"),),
        (),
        "[read](../README.md)",
    )
    reader = Reader()
    records, _ = scan_repository(
        {"docs/guide.md": source},
        snapshot_seed="git:base:post:digest",
        path_effects=(PathEffect("README.md", "delete", "review"),),
        reader=reader,
    )
    record = records["docs/guide.md"]
    assert reader.stat_calls == []
    assert record["checks_run"] == ["local_link_targets"]
    assert "target does not exist" in record["findings"][0]["detail"]


def test_target_limit_is_all_or_none_before_metadata(monkeypatch) -> None:
    class TwoTargets(Check):
        def collect(self, sources):
            return {
                source: SourceRequests(
                    (RepositoryRequest("a.md"), RepositoryRequest("b.md"))
                )
                for source in sources
            }

    monkeypatch.setattr(repository, "MAX_TARGETS", 1)
    reader = Reader()
    records, _ = scan_repository(
        {"docs/a.md": _source()},
        snapshot_seed="seed",
        path_effects=(),
        reader=reader,
        checks=(TwoTargets(),),
    )
    assert reader.stat_calls == []
    assert "target limit exceeded" in records["docs/a.md"]["diagnostics"][0]


def test_content_limit_fetches_no_target_content(monkeypatch) -> None:
    class ContentCheck(Check):
        def collect(self, sources):
            return {
                source: SourceRequests((RepositoryRequest("target.md", True),))
                for source in sources
            }

    class LargeReader(Reader):
        def stat(self, paths):
            self.stat_calls.append(paths)
            return {path: StatResult("file", 2) for path in paths}

    monkeypatch.setattr(repository, "MAX_TARGET_CONTENT_BYTES", 1)
    reader = LargeReader()
    records, _ = scan_repository(
        {"docs/a.md": _source()},
        snapshot_seed="seed",
        path_effects=(),
        reader=reader,
        checks=(ContentCheck(),),
    )
    assert reader.read_calls == []
    assert "content limit exceeded" in records["docs/a.md"]["diagnostics"][0]


def test_changed_file_with_unavailable_content_answers_existence_only() -> None:
    source = MechanicalSnapshot(
        "docs/guide.md",
        "diff",
        "",
        ((1, "[read](target.md)"),),
        (),
        "[read](target.md)",
    )
    records, _ = scan_repository(
        {"docs/guide.md": source},
        snapshot_seed="git:base:diff:digest",
        path_effects=(
            PathEffect(
                "docs/target.md",
                "edit",
                "review",
                post_image_error="post-image reconstruction failed",
            ),
        ),
        reader=Reader(),
    )
    assert records["docs/guide.md"]["checks_run"] == ["local_link_targets"]
    assert records["docs/guide.md"]["findings"] == []


def test_changed_file_with_unavailable_content_defers_anchor_check() -> None:
    source = MechanicalSnapshot(
        "docs/guide.md",
        "diff",
        "",
        ((1, "[read](target.md#part)"),),
        (),
        "[read](target.md#part)",
    )
    records, _ = scan_repository(
        {"docs/guide.md": source},
        snapshot_seed="git:base:diff:digest",
        path_effects=(
            PathEffect(
                "docs/target.md",
                "edit",
                "review",
                post_image_error="post-image reconstruction failed",
            ),
        ),
        reader=Reader(),
    )
    assert records["docs/guide.md"]["checks_run"] == []
    assert records["docs/guide.md"]["diagnostics"] == [
        "post-image reconstruction failed"
    ]


def test_global_limit_preserves_collector_diagnostic(monkeypatch) -> None:
    class Mixed(Check):
        def collect(self, sources):
            return {
                "docs/a.md": SourceRequests(diagnostic="source is oversized"),
                "docs/b.md": SourceRequests(
                    (RepositoryRequest("one.md"), RepositoryRequest("two.md"))
                ),
            }

    monkeypatch.setattr(repository, "MAX_TARGETS", 1)
    records, _ = scan_repository(
        {"docs/a.md": _source(), "docs/b.md": _source("docs/b.md")},
        snapshot_seed="seed",
        path_effects=(),
        reader=Reader(),
        checks=(Mixed(),),
    )
    assert records["docs/a.md"]["diagnostics"] == ["source is oversized"]
    assert "target limit exceeded" in records["docs/b.md"]["diagnostics"][0]
