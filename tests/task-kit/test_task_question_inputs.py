"""Input readers, canonical text, limits and the inputs fingerprint, against the golden fixture."""

from __future__ import annotations

import re
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from task_system import question_inputs, questions
from task_system.discovery import read_task_block
from task_system.question_inputs import QuestionInputError

GOLDEN = Path(__file__).parent / "fixtures" / "answers" / "golden"
SHIPPED = questions.default_shipped_dir()


@pytest.fixture
def golden_question():
    return questions.load_question(GOLDEN / "golden.yaml")


@pytest.fixture
def folder(tmp_path) -> Path:
    """A writable copy of the golden task folder."""
    target = tmp_path / "task"
    shutil.copytree(GOLDEN / "task", target)
    return target


def bundle_of(folder: Path, question):
    return question_inputs.read_inputs(folder, read_task_block(folder), question)


def fingerprint_of(folder: Path, question) -> str:
    return question_inputs.inputs_fingerprint(question, bundle_of(folder, question))


class TestGolden:
    def test_bundle_text_matches_the_fixture_byte_for_byte(self, golden_question):
        bundle = question_inputs.read_inputs(
            GOLDEN / "task", read_task_block(GOLDEN / "task"), golden_question, task_id="golden-task"
        )
        expected = (GOLDEN / "expected-bundle.txt").read_bytes()
        assert bundle.prompt_text().encode("utf-8") == expected
        assert bundle.task_id == "golden-task" and bundle.truncated == ()

    def test_fingerprint_matches_the_fixture(self, golden_question):
        expected = (GOLDEN / "expected-fingerprint.txt").read_text(encoding="ascii").strip()
        assert fingerprint_of(GOLDEN / "task", golden_question) == expected

    def test_the_fixture_exercises_what_it_claims(self):
        """A fixture that excludes nothing proves nothing about the exclusions."""
        text = (GOLDEN / "expected-bundle.txt").read_text(encoding="utf-8")
        assert "summary" not in text and "refresh (no field edits)" not in text
        assert "update: summary" not in text and "_schema_version" not in text
        raw_log = (GOLDEN / "task" / "log.md").read_text(encoding="utf-8")
        assert "refresh (no field edits)" in raw_log and "summary: refreshed" in raw_log
        assert "summary_fingerprint" in (GOLDEN / "task" / "task.yaml").read_text(encoding="utf-8")

    def test_task_id_defaults_to_the_folder_name(self, folder, golden_question):
        assert bundle_of(folder, golden_question).task_id == "task"


class TestFingerprintStability:
    def test_a_refresh_line_leaves_the_fingerprint_unchanged(self, folder, golden_question):
        before = fingerprint_of(folder, golden_question)
        with (folder / "log.md").open("a", encoding="utf-8") as fh:
            fh.write("- 2026-10-20 10:00: update: refresh (no field edits)\n")
            fh.write("- 2026-10-20 10:01: update: summary: refreshed\n")
        assert fingerprint_of(folder, golden_question) == before

    def test_a_real_log_entry_changes_the_fingerprint(self, folder, golden_question):
        """Guards the test above: the fingerprint does read the log."""
        before = fingerprint_of(folder, golden_question)
        with (folder / "log.md").open("a", encoding="utf-8") as fh:
            fh.write("- 2026-10-20 10:00: The punch-down is done.\n")
        assert fingerprint_of(folder, golden_question) != before

    def test_generated_summary_keys_in_task_yaml_are_outside_the_fingerprint(self, folder, golden_question):
        before = fingerprint_of(folder, golden_question)
        data = yaml.safe_load((folder / "task.yaml").read_text(encoding="utf-8"))
        data["task"]["summary"] = "A different generated summary."
        data["task"]["summary_fingerprint"] = "ffff"
        data["task"]["summary_updated"] = "2026-10-21"
        (folder / "task.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
        assert fingerprint_of(folder, golden_question) == before
        data["task"]["title"] = "A new title"
        (folder / "task.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
        assert fingerprint_of(folder, golden_question) != before

    def test_files_under_answers_are_never_read(self, folder, golden_question):
        before = fingerprint_of(folder, golden_question)
        answers = folder / "answers" / "orientation"
        answers.mkdir(parents=True)
        (answers / "studio.yaml").write_text("answer: {headline: x}\n", encoding="utf-8")
        (answers / "notes.md").write_text("- 2026-10-20 10:00: not a log entry\n", encoding="utf-8")
        assert fingerprint_of(folder, golden_question) == before

    def test_crlf_and_trailing_whitespace_do_not_change_the_fingerprint(self, folder, golden_question):
        before = fingerprint_of(folder, golden_question)
        for name in ("CLAUDE.md", "plan.md", "log.md"):
            raw = (folder / name).read_bytes().decode("utf-8")
            (folder / name).write_bytes(raw.replace("\n", "  \r\n").encode("utf-8"))
        assert fingerprint_of(folder, golden_question) == before

    def test_the_question_definition_is_part_of_the_fingerprint(self, folder, golden_question):
        other = replace(golden_question, digest="0" * 64)
        assert fingerprint_of(folder, other) != fingerprint_of(folder, golden_question)

    def test_a_bundle_read_for_another_question_is_refused(self, folder, golden_question):
        bundle = bundle_of(folder, golden_question)
        narrower = replace(golden_question, inputs=("task",))
        with pytest.raises(QuestionInputError, match="declares"):
            question_inputs.inputs_fingerprint(narrower, bundle)


class TestReaders:
    def read(self, folder, name):
        question = replace(questions.load_catalog(SHIPPED).get("orientation"), inputs=(name,))
        return dict(bundle_of(folder, question).texts)[name]

    def test_task_reader_is_sorted_yaml_without_generated_keys(self, folder):
        text = self.read(folder, "task")
        keys = [line.split(":")[0] for line in text.splitlines() if re.match(r"[a-z_]+:", line)]
        assert keys == sorted(keys)
        assert "summary" not in text and "_schema_version" not in text
        assert "description: |\n  Data path is dead." in text

    def test_items_reader_lists_each_item_with_its_optional_fields(self, folder):
        text = self.read(folder, "items")
        assert "- id: buy-tools\n  note: waiting on the hardware store\n  priority: P1" in text
        assert "- id: retest-run\n  state: deferred" in text

    def test_activity_reader_is_newest_first_and_leaves_out_bookkeeping(self, folder):
        lines = self.read(folder, "activity").splitlines()
        assert lines == [
            "2026-10-06: Tester shows the pair open at the wall plate.",
            "2026-10-03 09:15: update: status = 'active'",
        ]

    def test_absent_files_read_as_empty_text(self, folder):
        (folder / "CLAUDE.md").unlink()
        (folder / "log.md").unlink()
        assert self.read(folder, "claude_md") == ""
        assert self.read(folder, "activity") == ""

    def test_an_unreadable_file_is_an_error_naming_it(self, folder):
        (folder / "CLAUDE.md").write_bytes(b"\xff\xfe\x00bad")
        with pytest.raises(QuestionInputError, match="CLAUDE.md"):
            self.read(folder, "claude_md")

    def test_an_unreadable_task_block_reads_as_empty(self, folder):
        question = replace(questions.load_catalog(SHIPPED).get("orientation"), inputs=("task",))
        bundle = question_inputs.read_inputs(folder, None, question)
        assert dict(bundle.texts)["task"] == "{}"

    def test_a_script_only_reader_is_refused_in_a_bundle(self, folder):
        question = questions.load_catalog(SHIPPED).get("task_commits")
        with pytest.raises(QuestionInputError, match="script-only"):
            bundle_of(folder, question)

    def test_an_unknown_reader_is_refused(self, folder):
        question = replace(questions.load_catalog(SHIPPED).get("orientation"), inputs=("code_commits",))
        with pytest.raises(QuestionInputError, match="unknown reader"):
            bundle_of(folder, question)


class TestLimits:
    def test_the_shipped_questions_fit_their_input_budget(self):
        for question in questions.load_catalog(SHIPPED).questions:
            total = sum(question_inputs.READERS[name].limit or 0 for name in question.inputs)
            assert total <= question.max_input_chars, question.id

    def test_text_over_the_limit_is_cut_with_a_final_marker_line_and_reported(self, folder):
        (folder / "CLAUDE.md").write_text("line of text\n" * 2000, encoding="utf-8")
        question = replace(questions.load_catalog(SHIPPED).get("orientation"), inputs=("claude_md", "task"))
        bundle = bundle_of(folder, question)
        text = dict(bundle.texts)["claude_md"]
        assert len(text) <= question_inputs.READERS["claude_md"].limit
        assert text.endswith("\n[truncated]") and text.splitlines()[-1] == "[truncated]"
        assert bundle.truncated == ("claude_md",)

    def test_text_at_the_limit_is_not_cut(self):
        text, cut = question_inputs.cut_to_limit("x" * 100, 100)
        assert text == "x" * 100 and cut is False
        text, cut = question_inputs.cut_to_limit("x" * 101, 100)
        assert cut is True and len(text) == 100 and text.endswith("[truncated]")

    def test_cut_text_is_canonical(self):
        text, _ = question_inputs.cut_to_limit("ab   \n" * 100, 50)
        assert all(line == line.rstrip() for line in text.splitlines())

    def test_every_reader_limit_holds_for_oversize_material(self, folder):
        (folder / "plan.md").write_text("plan line\n" * 5000, encoding="utf-8")
        (folder / "log.md").write_text("".join(f"- 2026-01-{d:02d}: entry {d} {'z' * 80}\n" for d in range(1, 29)) * 10,
                                       encoding="utf-8")
        question = replace(
            questions.load_catalog(SHIPPED).get("orientation"),
            inputs=("task", "claude_md", "plan_md", "items", "activity"),
        )
        for name, text in bundle_of(folder, question).texts:
            assert len(text) <= question_inputs.READERS[name].limit, name


class TestPromptText:
    def test_sections_are_headed_and_in_declared_order(self, folder, golden_question):
        bundle = bundle_of(folder, golden_question)
        headings = [line for line in bundle.prompt_text().splitlines() if line.startswith("=== ")]
        assert headings == [f"=== {name} ===" for name in golden_question.inputs]

    def test_empty_text_still_gets_its_heading(self, folder, golden_question):
        (folder / "CLAUDE.md").unlink()
        text = bundle_of(folder, golden_question).prompt_text()
        assert "=== claude_md ===\n\n=== plan_md ===\n" in text
