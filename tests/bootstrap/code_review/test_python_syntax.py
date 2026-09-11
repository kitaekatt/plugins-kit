"""Mapped syntax coverage and snapshot mutation regressions."""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest

from bootstrap_lib.code_review import lane_prompts
from bootstrap_lib.code_review.mechanical import MechanicalSnapshot
from bootstrap_lib.code_review.mechanical_repository import (
    PathEffect,
    ReadResult,
    StatResult,
    scan_repository,
)
from bootstrap_lib.code_review.mechanical_repository.python_syntax import CHECK
from bootstrap_lib.code_review.pipeline import assemble_bundle


GRAMMAR = f"{sys.version_info.major}.{sys.version_info.minor}"


class Reader:
    def __init__(self, contents: dict[str, bytes | None]) -> None:
        self.contents = contents
        self.stat_calls: list[tuple[str, ...]] = []
        self.read_calls: list[tuple[str, ...]] = []

    def stat(self, paths: tuple[str, ...]) -> dict[str, StatResult]:
        self.stat_calls.append(paths)
        return {
            path: StatResult("file", len(self.contents[path] or b""))
            if path in self.contents else StatResult("missing")
            for path in paths
        }

    def read(self, paths: tuple[str, ...]) -> dict[str, ReadResult]:
        self.read_calls.append(paths)
        return {
            path: ReadResult("file", self.contents[path])
            if self.contents[path] is not None else ReadResult("error")
            for path in paths
        }


def source(text: str, file: str = "src/app.py", added: int = 1) -> MechanicalSnapshot:
    return MechanicalSnapshot(file, "diff", "", ((added, ""),), (), text)


def scan(
    snapshot: MechanicalSnapshot,
    *,
    mappings: dict[str, bytes | None] | None = None,
    effects: tuple[PathEffect, ...] = (),
) -> dict[str, object]:
    records, _ = scan_repository(
        {snapshot.file: snapshot},
        snapshot_seed="test:snapshot",
        reader=Reader({".python-version": GRAMMAR.encode()} if mappings is None else mappings),
        path_effects=effects,
        checks=(CHECK,),
    )
    return records[snapshot.file]


@pytest.mark.parametrize("text", [
    "value = 1\n",
    "raise RuntimeError('reviewed code must never execute')\n",
    "import module_that_does_not_exist\n",
    "def broken_at_runtime():\n    return unknown_name\n",
    "\ufeffvalue = 1\n",
])
def test_valid_compilation_never_executes_or_resolves_imports(text: str) -> None:
    record = scan(source(text))
    assert record["checks_run"] == ["python_syntax"]
    assert record["findings"] == []


@pytest.mark.parametrize(("text", "message"), [
    ("def broken(:\n    pass\n", "invalid syntax"),
    ("return 1\n", "outside function"),
    ("break\n", "outside loop"),
    ("def f():\npass\n", "indented block"),
    ("def f(x, x):\n    pass\n", "duplicate argument"),
])
def test_grammar_and_context_syntax_errors_are_detected(text: str, message: str) -> None:
    record = scan(source(text))
    assert record["checks_run"] == ["python_syntax"]
    assert len(record["findings"]) == 1
    finding = record["findings"][0]
    assert finding["check"] == "python_syntax"
    assert message in finding["detail"]
    assert f"CPython {GRAMMAR} (.python-version)" in finding["detail"]


def test_diagnostic_on_unchanged_line_is_not_lost() -> None:
    record = scan(source("value = 1\nreturn value\n", added=1))
    assert record["checks_run"] == ["python_syntax"]
    assert record["findings"][0]["line"] == 2
    assert "introduction requires review" in record["findings"][0]["detail"]


@pytest.mark.parametrize("text", [
    "# coding: unknown_codec\nvalue = 1\n",
    "# coding: ascii\nvalue = 'caf\u00e9'\n",
])
def test_encoding_failures_preserve_unlocated_compiler_diagnostic(text: str) -> None:
    record = scan(source(text))
    assert record["checks_run"] == []
    assert record["findings"] == []
    assert "first compiler diagnostic:" in record["diagnostics"][0]
    assert "compiler supplied line 0, not a source location" in record["diagnostics"][0]
    record["mechanical_contract"] = 2
    message = lane_prompts.format_mechanical_findings([record])
    assert "Unavailable coverage: CPython" in message
    assert "compiler supplied line 0" in message
    assert "src/app.py:0" not in message


@pytest.mark.parametrize("mapping", [b"3.99", b"pypy3.12", b"system", b"3.12\n3.13", b"", b"\xff", None])
def test_unavailable_or_ambiguous_nearest_mapping_never_falls_back(mapping: bytes | None) -> None:
    record = scan(source("return 1\n"), mappings={
        ".python-version": GRAMMAR.encode(),
        "src/.python-version": mapping,
    })
    assert record["checks_run"] == []
    assert record["findings"] == []
    assert "src/.python-version" in record["diagnostics"][0]


def test_absent_mapping_is_uncovered() -> None:
    record = scan(source("return 1\n"), mappings={})
    assert record["checks_run"] == []
    assert "no snapshot .python-version" in record["diagnostics"][0]


def test_nearest_mapping_shadows_a_different_ancestor() -> None:
    record = scan(source("return 1\n"), mappings={
        ".python-version": b"3.99",
        "src/.python-version": f"{GRAMMAR}.0\n".encode(),
    })
    assert record["checks_run"] == ["python_syntax"]
    assert "src/.python-version" in record["findings"][0]["detail"]


def test_mapping_add_edit_and_delete_use_snapshot_overlay() -> None:
    snapshot = source("return 1\n")
    base = {".python-version": GRAMMAR.encode(), "src/.python-version": b"3.99"}
    for effect in ("add", "edit"):
        record = scan(snapshot, mappings=base, effects=(
            PathEffect("src/.python-version", effect, "review", GRAMMAR.encode()),
        ))
        assert record["checks_run"] == ["python_syntax"]
        assert "src/.python-version" in record["findings"][0]["detail"]
    record = scan(snapshot, mappings=base, effects=(
        PathEffect("src/.python-version", "delete", "review"),
    ))
    assert record["checks_run"] == ["python_syntax"]
    assert f"CPython {GRAMMAR} (.python-version)" in record["findings"][0]["detail"]


def test_unavailable_changed_mapping_does_not_use_base_content() -> None:
    record = scan(source("return 1\n"), effects=(
        PathEffect(".python-version", "edit", "review", post_image_error="capture failed"),
    ))
    assert record["checks_run"] == []
    assert "content unavailable" in record["diagnostics"][0]


def test_mapping_queries_are_batched_and_deduplicated() -> None:
    reader = Reader({".python-version": GRAMMAR.encode()})
    snapshots = {path: source("value = 1\n", path) for path in ("src/a.py", "src/b.py")}
    records, _ = scan_repository(snapshots, snapshot_seed="seed", reader=reader, path_effects=(), checks=(CHECK,))
    assert reader.stat_calls == [(".python-version", "src/.python-version")]
    assert reader.read_calls == [(".python-version",)]
    assert all(record["checks_run"] == ["python_syntax"] for record in records.values())


def test_source_mutation_changes_clean_result_to_syntax_finding() -> None:
    valid = scan(source("def f(value):\n    return value\n"))
    invalid = scan(source("def f(value)\n    return value\n"))
    repaired = scan(source("def f(value):\n    return value\n"))
    assert valid["findings"] == repaired["findings"] == []
    assert invalid["findings"][0]["line"] == 1


def test_mapping_mutation_removes_coverage_instead_of_claiming_clean() -> None:
    snapshot = source("return 1\n")
    assert scan(snapshot)["findings"]
    changed = scan(snapshot, effects=(PathEffect(".python-version", "edit", "review", b"3.99"),))
    assert changed["checks_run"] == []
    assert changed["findings"] == []
    assert "grammar unavailable" in changed["diagnostics"][0]


def test_missing_post_image_and_unsupported_paths_are_uncovered() -> None:
    for snapshot in (
        replace(source("return 1\n"), post_image_text=None),
        source("return 1\n", "../escape.py"),
        source("return 1\n", "/escape.py"),
    ):
        assert scan(snapshot)["checks_run"] == []
    assert CHECK.collect({"template.py.in": source("return 1\n", "template.py.in")}) == {}


def test_first_diagnostic_does_not_claim_all_errors_are_enumerated() -> None:
    record = scan(source("return 1\nbreak\n"))
    record["mechanical_contract"] = 2
    assert len(record["findings"]) == 1
    assert record["findings"][0]["line"] == 1
    message = lane_prompts.build_user_message(
        "reviewer_b_diff_only_bugs",
        diff_text="changed Python source",
        files=["src/app.py"],
        mechanical_findings={"schema_version": 2, "files": [record]},
        mechanical_check_phrases={CHECK.check_id: CHECK.phrase},
    )
    assert CHECK.phrase in message
    assert "Do not repeat\n  that covered question" in message
    assert "Do not repeat that compilation question" in message
    assert "does not enumerate later errors" in message
    assert "bug finding requires the lane's bug criteria" in message
    assert "added lines only" not in message
    assert "hidden by the first diagnostic remain reviewer scope and may be reported" in message


@pytest.mark.parametrize("identifier", ["src/app.py", "//depot/main/src/app.py"])
def test_bundle_delivers_syntax_answer_to_original_lane(
    tmp_path: Path, identifier: str
) -> None:
    bundle = assemble_bundle(
        preamble="",
        sections=[{"identifier": identifier, "text": "@@ -0,0 +1 @@\n+return 1\n"}],
        files=[{
            "identifier": identifier,
            "repository_path": "src/app.py",
            "local": None,
            "pre_image_is_empty": True,
        }],
        bundle_dir=tmp_path / "bundle",
        max_chunk_bytes=1024 * 1024,
        workspace_root=None,
        snapshot_seed="test:snapshot",
        snapshot_reader=Reader({".python-version": GRAMMAR.encode()}),
        path_effects=(PathEffect("src/app.py", "add", "review", b"return 1\n"),),
        mechanical_contract=2,
    )
    chunk = bundle["diff_chunks"][0]
    record = chunk["mechanical_scan"]["files"][0]
    assert record["file"] == identifier
    assert record["checks_run"] == ["python_syntax"]
    assert record["findings"][0]["line"] == 1
    message = lane_prompts.build_user_message(
        "reviewer_b_diff_only_bugs",
        diff_text="@@ -0,0 +1 @@\n+return 1\n",
        files=[identifier],
        mechanical_findings=chunk["mechanical_scan"],
        mechanical_check_phrases=bundle["mechanical_check_phrases"],
    )
    assert f"{identifier}:1 [python_syntax]" in message
    assert CHECK.phrase in message
    assert "Do not repeat that compilation question" in message


@pytest.mark.parametrize("identifier", ["src/app.py", "//depot/main/src/app.py"])
@pytest.mark.parametrize("contract", [None, 1, 2])
def test_old_and_new_bundle_consumers_receive_only_supported_answers(
    tmp_path: Path, identifier: str, contract: int | None
) -> None:
    data_id = identifier.replace("app.py", "data.json")
    pre_image = tmp_path / "data.pre"
    pre_image.write_text('{"new": 0,\n"old": invalid}\n', encoding="utf-8")
    arguments = {} if contract is None else {"mechanical_contract": contract}
    bundle = assemble_bundle(
        preamble="",
        sections=[
            {"identifier": identifier, "text": "@@ -0,0 +1 @@\n+return 1\n"},
            {"identifier": data_id, "text": '@@ -1,2 +1,2 @@\n-{"new": 0,\n+{"new": 1,\n "old": invalid}\n'},
        ],
        files=[
            {"identifier": identifier, "repository_path": "src/app.py", "local": None, "pre_image_is_empty": True},
            {"identifier": data_id, "repository_path": "src/data.json", "local": None, "pre_image": str(pre_image)},
        ],
        bundle_dir=tmp_path / "bundle",
        max_chunk_bytes=1024 * 1024,
        workspace_root=None,
        snapshot_seed="test:snapshot",
        snapshot_reader=Reader({".python-version": GRAMMAR.encode()}),
        path_effects=(),
        **arguments,
    )
    chunk = bundle["diff_chunks"][0]
    records = {record["file"]: record for record in chunk["mechanical_scan"]["files"]}
    message = lane_prompts.build_user_message(
        "reviewer_b_diff_only_bugs", diff_text="review diff", files=[identifier, data_id],
        mechanical_findings=chunk["mechanical_scan"],
        mechanical_check_phrases=bundle["mechanical_check_phrases"],
    )
    if contract == 2:
        assert bundle["mechanical_contract"] == 2
        assert all(record["mechanical_contract"] == 2 for record in records.values())
        assert records[identifier]["checks_run"] == ["python_syntax"]
        assert records[data_id]["findings"][0]["line"] == 2
        assert "Mechanical scan:" in message
        assert "hidden by the first diagnostic remain reviewer scope" in message
    else:
        assert "mechanical_contract" not in bundle
        assert all("mechanical_contract" not in record for record in records.values())
        assert records[identifier]["checks_run"] == []
        assert records[data_id]["findings"] == []
        assert "python_syntax" not in bundle["mechanical_check_phrases"]
        assert bundle["mechanical_check_phrases"]["structured_parse"] == "structured-data parse failures"
        assert "Mechanical scan (added lines only):" in message
        assert "no\n  whole-post-image syntax" in message


def test_unknown_consumer_contract_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsupported mechanical consumer contract"):
        assemble_bundle("", [], [], tmp_path, 1024, None, mechanical_contract=99)
