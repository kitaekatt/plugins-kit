"""The question catalog: shipped files, fleet layer, retirement, validation errors, digest, schema subset."""

from __future__ import annotations

import shutil
from pathlib import Path
from types import MappingProxyType

import pytest
import yaml

from task_system import questions
from task_system.answers_types import Question
from task_system.questions import QuestionError

SHIPPED = questions.default_shipped_dir()
GOLDEN = Path(__file__).parent / "fixtures" / "answers" / "golden"


def make_dirs(tmp_path: Path) -> tuple[Path, Path]:
    """A writable copy of the shipped questions and an empty fleet directory."""
    shipped = tmp_path / "shipped"
    shutil.copytree(SHIPPED, shipped)
    fleet = tmp_path / "fleet"
    fleet.mkdir()
    return shipped, fleet


def shipped_data(question_id: str) -> dict:
    return yaml.safe_load((SHIPPED / f"{question_id}.yaml").read_text(encoding="utf-8"))


def write_question(directory: Path, data: dict) -> Path:
    path = directory / f"{data['id']}.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


class TestShippedCatalog:
    def test_four_v1_questions_load_in_catalog_order(self):
        catalog = questions.load_catalog(SHIPPED)
        assert catalog.version == 1
        assert catalog.ids == ("orientation", "decisions", "activity", "task_commits")
        assert all(isinstance(q, Question) and len(q.digest) == 64 for q in catalog.questions)

    def test_every_shipped_file_is_ascii(self):
        for path in SHIPPED.glob("*.yaml"):
            path.read_text(encoding="utf-8").encode("ascii")

    def test_script_questions_have_no_prompt_and_model_questions_have_no_script(self):
        catalog = questions.load_catalog(SHIPPED)
        for qid in ("activity", "task_commits"):
            question = catalog.get(qid)
            assert question.script == qid and question.prompt is None
        for qid in ("orientation", "decisions"):
            question = catalog.get(qid)
            assert question.script is None and question.prompt

    def test_decisions_apply_only_to_active_and_blocked(self):
        assert questions.load_catalog(SHIPPED).get("decisions").applies_to == ("active", "blocked")

    def test_answer_schema_is_read_only_all_the_way_down(self):
        schema = questions.load_catalog(SHIPPED).get("decisions").answer_schema
        assert isinstance(schema, MappingProxyType)
        with pytest.raises(TypeError):
            schema["type"] = "array"
        with pytest.raises(TypeError):
            schema["properties"]["decisions"]["maxItems"] = 99


class TestFleetLayer:
    def test_a_fleet_file_with_a_shipped_id_replaces_it(self, tmp_path):
        shipped, fleet = make_dirs(tmp_path)
        data = shipped_data("orientation")
        data["prompt"] = "Fleet wording."
        path = write_question(fleet, data)
        before = questions.load_catalog(shipped).get("orientation")
        catalog = questions.load_catalog(shipped, fleet)
        after = catalog.get("orientation")
        assert after.prompt == "Fleet wording." and after.source == path
        assert after.digest != before.digest
        assert catalog.ids == ("orientation", "decisions", "activity", "task_commits")  # same position

    def test_retired_true_drops_the_shipped_question(self, tmp_path):
        shipped, fleet = make_dirs(tmp_path)
        (fleet / "decisions.yaml").write_text(
            '_schema_version: "1"\nid: decisions\nretired: true\n', encoding="utf-8"
        )
        catalog = questions.load_catalog(shipped, fleet)
        assert catalog.ids == ("orientation", "activity", "task_commits")
        assert catalog.retired == ("decisions",)

    def test_retiring_an_id_the_catalog_lacks_changes_nothing(self, tmp_path):
        shipped, fleet = make_dirs(tmp_path)
        (fleet / "gone_question.yaml").write_text(
            '_schema_version: "1"\nid: gone_question\nretired: true\n', encoding="utf-8"
        )
        assert questions.load_catalog(shipped, fleet).ids == questions.load_catalog(shipped).ids

    def test_a_fleet_file_with_a_new_id_adds_a_question_after_the_shipped_ones(self, tmp_path):
        shipped, fleet = make_dirs(tmp_path)
        data = shipped_data("orientation")
        data["id"] = "extra_view"
        write_question(fleet, data)
        assert questions.load_catalog(shipped, fleet).ids[-1] == "extra_view"

    def test_a_missing_fleet_directory_is_no_fleet_layer(self, tmp_path):
        shipped, _ = make_dirs(tmp_path)
        assert questions.load_catalog(shipped, tmp_path / "absent").ids == questions.load_catalog(shipped).ids

    def test_retired_in_a_shipped_file_is_an_error(self, tmp_path):
        shipped, _ = make_dirs(tmp_path)
        data = shipped_data("activity")
        data["retired"] = True
        write_question(shipped, data)
        with pytest.raises(QuestionError, match=r"activity\.yaml.*retired by removing"):
            questions.load_catalog(shipped)


class TestErrorsNameTheFile:
    def load_with(self, tmp_path, qid, **changes):
        shipped, fleet = make_dirs(tmp_path)
        data = shipped_data(qid)
        data.update(changes)
        path = write_question(fleet, data)
        with pytest.raises(QuestionError) as info:
            questions.load_catalog(shipped, fleet)
        assert str(path) in str(info.value)
        return str(info.value)

    def test_unknown_reader(self, tmp_path):
        message = self.load_with(tmp_path, "orientation", inputs=["task", "code_commits"])
        assert "unknown readers ['code_commits']" in message

    def test_unknown_script(self, tmp_path):
        message = self.load_with(tmp_path, "activity", script="no_such_script")
        assert "unknown script 'no_such_script'" in message

    def test_unknown_key(self, tmp_path):
        assert "unknown keys ['colour']" in self.load_with(tmp_path, "orientation", colour="red")

    def test_wrong_schema_version(self, tmp_path):
        assert "_schema_version" in self.load_with(tmp_path, "orientation", _schema_version="2")

    def test_file_name_must_equal_the_id(self, tmp_path):
        shipped, fleet = make_dirs(tmp_path)
        data = shipped_data("orientation")
        (fleet / "other_name.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
        with pytest.raises(QuestionError, match=r"other_name\.yaml.*must be orientation\.yaml"):
            questions.load_catalog(shipped, fleet)

    def test_a_script_only_reader_cannot_feed_a_prompt(self, tmp_path):
        message = self.load_with(tmp_path, "orientation", inputs=["task", "task_commits"])
        assert "script-only" in message

    def test_max_input_chars_must_cover_the_reader_limits(self, tmp_path):
        assert "below the readers' limits" in self.load_with(tmp_path, "orientation", max_input_chars=39999)

    def test_needs_a_script_or_a_prompt(self, tmp_path):
        assert "script, a prompt" in self.load_with(tmp_path, "activity", script=None)

    def test_unknown_status_in_applies_to(self, tmp_path):
        assert "unknown statuses ['done']" in self.load_with(tmp_path, "orientation", applies_to=["done"])

    def test_answer_schema_outside_the_subset(self, tmp_path):
        schema = shipped_data("orientation")["answer"]
        schema["properties"]["headline"]["pattern"] = "^x"
        assert "outside the supported subset" in self.load_with(tmp_path, "orientation", answer=schema)

    def test_object_schema_must_close_additional_properties(self, tmp_path):
        schema = shipped_data("orientation")["answer"]
        del schema["additionalProperties"]
        assert "additionalProperties: false" in self.load_with(tmp_path, "orientation", answer=schema)

    def test_grounding_path_must_name_a_field(self, tmp_path):
        message = self.load_with(tmp_path, "decisions", grounding=["decisions[].nothing"])
        assert "no field 'nothing'" in message

    def test_grounding_path_must_reach_a_string_field(self, tmp_path):
        message = self.load_with(tmp_path, "decisions", grounding=["decisions"])
        assert "must reach a string field" in message

    def test_unreadable_yaml(self, tmp_path):
        shipped, fleet = make_dirs(tmp_path)
        (fleet / "broken.yaml").write_text("id: [unclosed", encoding="utf-8")
        with pytest.raises(QuestionError, match=r"broken\.yaml"):
            questions.load_catalog(shipped, fleet)

    def test_shipped_file_missing_from_the_catalog_is_an_error(self, tmp_path):
        shipped, _ = make_dirs(tmp_path)
        data = shipped_data("orientation")
        data["id"] = "stray"
        write_question(shipped, data)
        with pytest.raises(QuestionError, match=r"catalog\.yaml.*\['stray'\].*not listed"):
            questions.load_catalog(shipped)

    def test_catalog_listing_a_missing_file_is_an_error(self, tmp_path):
        shipped, _ = make_dirs(tmp_path)
        (shipped / "decisions.yaml").unlink()
        with pytest.raises(QuestionError, match=r"catalog\.yaml.*'decisions'.*does not exist"):
            questions.load_catalog(shipped)


class TestDigest:
    def digest_of(self, tmp_path, **changes):
        shipped, fleet = make_dirs(tmp_path)
        data = shipped_data("orientation")
        data.update(changes)
        write_question(fleet, data)
        return questions.load_catalog(shipped, fleet).get("orientation").digest

    def test_definition_changes_change_the_digest(self, tmp_path):
        base = self.digest_of(tmp_path)
        assert base == questions.load_catalog(SHIPPED).get("orientation").digest
        assert self.digest_of(tmp_path / "a", prompt="Other wording.") != base
        assert self.digest_of(tmp_path / "b", version=2) != base
        assert self.digest_of(tmp_path / "c", inputs=["task", "claude_md"]) != base
        assert self.digest_of(tmp_path / "d", max_input_chars=50000) != base
        schema = shipped_data("orientation")["answer"]
        schema["properties"]["next"]["maxLength"] = 100
        assert self.digest_of(tmp_path / "e", answer=schema) != base

    def test_title_applicability_models_and_effort_are_outside_the_digest(self, tmp_path):
        base = self.digest_of(tmp_path)
        changed = self.digest_of(
            tmp_path / "x", title="Another title", applies_to=["active"], models=["luna"], effort="high"
        )
        assert changed == base

    def test_golden_digest_is_stable_across_loads(self):
        first = questions.load_question(GOLDEN / "golden.yaml")
        second = questions.load_question(GOLDEN / "golden.yaml")
        assert first.digest == second.digest


class TestSchemaSubset:
    SCHEMA = {
        "type": "object", "required": ["a"], "additionalProperties": False,
        "properties": {
            "a": {"type": "string", "minLength": 1, "maxLength": 3},
            "n": {"type": "integer", "minimum": 0, "maximum": 5},
            "e": {"type": "string", "enum": ["x", "y"]},
            "list": {"type": "array", "maxItems": 1, "items": {"type": "boolean"}},
        },
    }

    def test_a_matching_value_has_no_errors(self):
        assert questions.validate_value(self.SCHEMA, {"a": "ok", "n": 2, "e": "x", "list": [True]}) == []

    @pytest.mark.parametrize("value,fragment", [
        ({}, "missing required 'a'"),
        ({"a": "ok", "z": 1}, "unexpected key 'z'"),
        ({"a": ""}, "shorter than 1"),
        ({"a": "toolong"}, "longer than 3"),
        ({"a": "ok", "n": True}, "expected an integer"),
        ({"a": "ok", "n": 6}, "above 5"),
        ({"a": "ok", "e": "z"}, "not one of"),
        ({"a": "ok", "list": [True, False]}, "more than 1"),
        ({"a": "ok", "list": ["no"]}, "answer.list[0]: expected a boolean"),
        ("text", "expected an object"),
    ])
    def test_violations_are_named_with_their_path(self, value, fragment):
        errors = questions.validate_value(self.SCHEMA, value)
        assert any(fragment in error for error in errors), errors

    def test_shipped_schemas_accept_their_own_shape(self):
        catalog = questions.load_catalog(SHIPPED)
        assert questions.validate_value(
            catalog.get("orientation").answer_schema, {"headline": "h", "state": "s", "next": "n"}
        ) == []
        assert questions.validate_value(
            catalog.get("decisions").answer_schema,
            {"decisions": [{"item": "i", "question": "q", "evidence": "e"}]},
        ) == []

    def test_grounded_values_step_into_arrays_and_skip_absent_fields(self):
        answer = {"decisions": [
            {"item": "a", "question": "q", "evidence": "one", "default": "yes"},
            {"item": "b", "question": "q", "evidence": "two"},
        ]}
        assert questions.grounded_values(answer, "decisions[].evidence") == ["one", "two"]
        assert questions.grounded_values(answer, "decisions[].default") == ["yes"]
        assert questions.grounded_values({"decisions": []}, "decisions[].evidence") == []
