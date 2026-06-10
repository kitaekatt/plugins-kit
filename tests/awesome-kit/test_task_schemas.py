"""Schema acceptance/rejection for the task-system typed units.

TASK_SCHEMA and TASK_LIST_SCHEMA are exercised straight through
skills_kit_lib.schema_engine.validate -- the same call validate.py makes.
Vocabulary checks (status/priority/_schema_version/type) are post-walker
checks in validate.py and are covered by test_task_validate.py.
"""

import pytest
from skills_kit_lib import schema_engine

from task_system.schemas import TASK_LIST_SCHEMA, TASK_SCHEMA


def _fails(data: dict, schema: dict) -> list:
    fails, _checked = schema_engine.validate(data, schema)
    return fails


def _minimal_task() -> dict:
    return {
        "task": {
            "_schema_version": "1",
            "type": "hand-off",
            "title": "Re-terminate the closet cat6a run",
            "status": "active",
        }
    }


def _full_task() -> dict:
    data = _minimal_task()
    data["task"].update(
        {
            "priority": "P2",
            "description": "Data path is dead.\nRe-terminate and re-test.\n",
            "depends_on": ["dev/tasks/buy-tools"],
            "blocked_by": ["tmp/spike-tester"],
            "agent_hint": "backend-developer",
            "skills_to_invoke": ["home-domain"],
        }
    )
    return data


class TestTaskSchemaAccepts:
    def test_minimal_valid(self):
        assert _fails(_minimal_task(), TASK_SCHEMA) == []

    def test_full_valid(self):
        assert _fails(_full_task(), TASK_SCHEMA) == []

    def test_empty_optional_lists_valid(self):
        data = _minimal_task()
        data["task"]["depends_on"] = []
        data["task"]["blocked_by"] = []
        assert _fails(data, TASK_SCHEMA) == []

    def test_unknown_extra_key_passes(self):
        # Schemas are floors, not ceilings.
        data = _minimal_task()
        data["task"]["custom_field"] = {"anything": True}
        assert _fails(data, TASK_SCHEMA) == []


class TestTaskSchemaRejects:
    @pytest.mark.parametrize(
        "missing", ["_schema_version", "type", "title", "status"]
    )
    def test_required_field_omission(self, missing):
        data = _minimal_task()
        del data["task"][missing]
        fails = _fails(data, TASK_SCHEMA)
        assert any(path == f"task.{missing}" for path, _ in fails), fails

    def test_empty_title_rejected(self):
        data = _minimal_task()
        data["task"]["title"] = ""
        fails = _fails(data, TASK_SCHEMA)
        assert any("title must be non-empty" in msg for _, msg in fails), fails

    def test_whitespace_title_rejected(self):
        data = _minimal_task()
        data["task"]["title"] = "   "
        fails = _fails(data, TASK_SCHEMA)
        assert any("title must be non-empty" in msg for _, msg in fails), fails

    @pytest.mark.parametrize(
        "key,bad_value",
        [
            ("_schema_version", 1),
            ("type", ["hand-off"]),
            ("title", 7),
            ("status", ["active"]),
            ("priority", 2),
            ("description", ["lines"]),
            ("depends_on", "dev/tasks/x"),
            ("blocked_by", {"path": "tmp/x"}),
            ("agent_hint", 3),
            ("skills_to_invoke", "home-domain"),
        ],
    )
    def test_wrong_types(self, key, bad_value):
        data = _full_task()
        data["task"][key] = bad_value
        fails = _fails(data, TASK_SCHEMA)
        assert any(path == f"task.{key}" for path, _ in fails), fails

    def test_root_key_missing(self):
        fails = _fails({"not_task": {}}, TASK_SCHEMA)
        assert fails == [("task", "root key missing")]

    def test_root_not_a_dict(self):
        fails = _fails({"task": ["a"]}, TASK_SCHEMA)
        assert any(path == "task" for path, _ in fails), fails


class TestTaskListSchema:
    def test_empty_refs_valid(self):
        assert _fails({"task_list": {"refs": []}}, TASK_LIST_SCHEMA) == []

    def test_path_only_ref_valid(self):
        data = {"task_list": {"refs": [{"path": "dev/tasks/x"}]}}
        assert _fails(data, TASK_LIST_SCHEMA) == []

    def test_path_and_host_ref_valid(self):
        data = {
            "task_list": {
                "refs": [
                    {"path": "dev/tasks/x"},
                    {"path": "tmp/spike-y", "host": "macbook"},
                ]
            }
        }
        assert _fails(data, TASK_LIST_SCHEMA) == []

    def test_ref_missing_path_invalid(self):
        data = {"task_list": {"refs": [{"host": "macbook"}]}}
        fails = _fails(data, TASK_LIST_SCHEMA)
        assert any(
            path == "task_list.refs[0].path" and msg == "required key missing"
            for path, msg in fails
        ), fails

    def test_refs_missing_invalid(self):
        fails = _fails({"task_list": {}}, TASK_LIST_SCHEMA)
        assert any(path == "task_list.refs" for path, _ in fails), fails

    def test_refs_wrong_type_invalid(self):
        fails = _fails({"task_list": {"refs": "dev/tasks/x"}}, TASK_LIST_SCHEMA)
        assert any(path == "task_list.refs" for path, _ in fails), fails

    def test_ref_item_not_dict_invalid(self):
        fails = _fails({"task_list": {"refs": ["dev/tasks/x"]}}, TASK_LIST_SCHEMA)
        assert any(path == "task_list.refs[0]" for path, _ in fails), fails

    def test_ref_path_wrong_type_invalid(self):
        fails = _fails({"task_list": {"refs": [{"path": 7}]}}, TASK_LIST_SCHEMA)
        assert any(path == "task_list.refs[0].path" for path, _ in fails), fails

    def test_root_key_missing(self):
        fails = _fails({"task": {}}, TASK_LIST_SCHEMA)
        assert fails == [("task_list", "root key missing")]
