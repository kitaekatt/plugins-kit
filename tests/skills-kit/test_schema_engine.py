"""Tests for schema_engine internals (arch-review S10).

The required/present/recurse loop is one _check_keys helper now, validate()
delegates the dict root to _validate_value, and ok_paths records only paths
checked WITHOUT failure (the documented semantics; previously a failing path
was appended too).
"""

from skills_kit_lib.schema_engine import validate


SCHEMA = {
    "root": "unit",
    "keys": {
        "good": {"type": "string", "required": True},
        "bad": {"type": "string", "required": True},
        "nested": {"type": "dict", "required": False, "keys": {
            "inner": {"type": "string", "required": True},
        }},
    },
}


class TestOkPathsSemantics:
    def test_failing_path_not_in_checked(self):
        data = {"unit": {"good": "ok", "bad": 123}}
        fails, checked = validate(data, SCHEMA)
        assert any(p == "unit.bad" for p, _ in fails)
        assert "unit.bad" not in checked
        assert "unit.good" in checked

    def test_parent_of_failing_child_not_in_checked(self):
        data = {"unit": {"good": "ok", "bad": "ok", "nested": {}}}
        fails, checked = validate(data, SCHEMA)
        assert any(p == "unit.nested.inner" for p, _ in fails)
        # nested's inner failed, so 'unit.nested' was not checked-without-failure
        assert "unit.nested" not in checked

    def test_clean_data_checks_all_paths(self):
        data = {"unit": {"good": "ok", "bad": "ok", "nested": {"inner": "ok"}}}
        fails, checked = validate(data, SCHEMA)
        assert fails == []
        for p in ("unit.good", "unit.bad", "unit.nested.inner", "unit.nested"):
            assert p in checked


class TestDictRootDelegation:
    def test_required_key_missing_still_fails(self):
        fails, _ = validate({"unit": {"good": "ok"}}, SCHEMA)
        assert any(p == "unit.bad" and "required key missing" in m for p, m in fails)

    def test_forbidden_keys_still_fail(self):
        schema = {"root": "unit", "keys": {}, "forbidden_keys": ["rules"]}
        fails, _ = validate({"unit": {"rules": []}}, schema)
        assert any("forbidden key" in m for _, m in fails)

    def test_root_value_schema_still_walks_arbitrary_keys(self):
        schema = {
            "root": "actions",
            "value_schema": {"keys": {
                "goal": {"type": "string", "required": True},
            }},
        }
        data = {"actions": {"deploy": {"goal": "ship"}, "broken": {}}}
        fails, checked = validate(data, schema)
        assert any(p == "actions.broken.goal" for p, _ in fails)
        assert "actions.deploy.goal" in checked

    def test_list_items_recurse_through_check_keys(self):
        schema = {
            "root": "unit",
            "keys": {"items_list": {"type": "list", "items": {"keys": {
                "id": {"type": "string", "required": True},
            }}}},
        }
        data = {"unit": {"items_list": [{"id": "a"}, {}]}}
        fails, checked = validate(data, schema)
        assert any(p == "unit.items_list[1].id" for p, _ in fails)
        assert "unit.items_list[0].id" in checked
