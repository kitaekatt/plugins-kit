"""Tests for the deferred-requirement API on _ScriptContext.

A deferred requirement is the counterpart to a failure: an unmet precondition
that only SOME capability needs, recorded for the point-of-need action to act
on rather than escalated at session start. The two properties that matter are
that it never reaches fix-all, and that the record disappears on its own once
the requirement is satisfied.
"""

import json
import os

import bootstrap_lib.engine as engine


def _ctx(data_dir, plugin_name="demo-kit"):
    return engine._ScriptContext(
        config={},
        data_dir=str(data_dir),
        plugin_root=str(data_dir),
        log_entries=[],
        ok_entries=[],
        prefix="",
        plugin_name=plugin_name,
    )


def _record_path(data_dir):
    return os.path.join(str(data_dir), engine.DEFERRED_REQUIREMENTS_FILENAME)


class TestApi:
    def test_deferred_does_not_reach_failures(self, tmp_path):
        """The whole point: no fix-all entry, so no session-start prompt."""
        ctx = _ctx(tmp_path)
        ctx.add_deferred_requirement(
            "some_credential", user_msg="u", agent_msg="a",
        )
        assert ctx.failures == []
        assert len(ctx.deferred) == 1

    def test_record_carries_plugin_and_messages(self, tmp_path):
        ctx = _ctx(tmp_path, plugin_name="llm-scripting-kit")
        ctx.add_deferred_requirement(
            "openrouter_credential",
            user_msg="nothing to do yet",
            agent_msg="ask like this",
            satisfied_by="llm-scripting-kit set-key",
        )
        d = ctx.deferred[0]
        assert d == {
            "name": "openrouter_credential",
            "plugin": "llm-scripting-kit",
            "user_msg": "nothing to do yet",
            "agent_msg": "ask like this",
            "satisfied_by": "llm-scripting-kit set-key",
        }

    def test_satisfied_by_is_optional(self, tmp_path):
        ctx = _ctx(tmp_path)
        ctx.add_deferred_requirement("x", user_msg="u", agent_msg="a")
        assert "satisfied_by" not in ctx.deferred[0]

    def test_failures_and_deferred_are_independent(self, tmp_path):
        ctx = _ctx(tmp_path)
        ctx.add_failure("real_breakage", user_msg="u")
        ctx.add_deferred_requirement("optional_thing", user_msg="u", agent_msg="a")
        assert len(ctx.failures) == 1
        assert len(ctx.deferred) == 1
        assert ctx.failures[0]["type"] == "real_breakage"


class TestPersistence:
    def test_writes_readable_record(self, tmp_path):
        engine._write_deferred_requirements(
            str(tmp_path), "demo-kit",
            [{"name": "n", "plugin": "demo-kit", "user_msg": "u", "agent_msg": "a"}],
        )
        payload = json.loads(open(_record_path(tmp_path)).read())
        assert payload["plugin"] == "demo-kit"
        assert payload["requirements"][0]["name"] == "n"
        assert payload["updated"].endswith("Z")

    def test_empty_write_removes_a_stale_record(self, tmp_path):
        """Satisfying a requirement must clear it with no separate clear step."""
        engine._write_deferred_requirements(
            str(tmp_path), "demo-kit", [{"name": "n", "user_msg": "u", "agent_msg": "a"}],
        )
        assert os.path.isfile(_record_path(tmp_path))
        engine._write_deferred_requirements(str(tmp_path), "demo-kit", [])
        assert not os.path.exists(_record_path(tmp_path))

    def test_empty_write_is_safe_when_nothing_was_recorded(self, tmp_path):
        engine._write_deferred_requirements(str(tmp_path), "demo-kit", [])
        assert not os.path.exists(_record_path(tmp_path))

    def test_rewrite_replaces_rather_than_appends(self, tmp_path):
        engine._write_deferred_requirements(
            str(tmp_path), "demo-kit", [{"name": "first", "user_msg": "u", "agent_msg": "a"}],
        )
        engine._write_deferred_requirements(
            str(tmp_path), "demo-kit", [{"name": "second", "user_msg": "u", "agent_msg": "a"}],
        )
        payload = json.loads(open(_record_path(tmp_path)).read())
        assert [r["name"] for r in payload["requirements"]] == ["second"]

    def test_write_failure_does_not_raise(self, tmp_path, monkeypatch):
        """Bookkeeping must never break the pass -- the requirement is deferred
        by definition, so a lost record costs a prompt, not correctness."""
        def boom(*a, **k):
            raise OSError("read-only")

        monkeypatch.setattr(engine, "_write_atomic", boom)
        engine._write_deferred_requirements(
            str(tmp_path), "demo-kit", [{"name": "n", "user_msg": "u", "agent_msg": "a"}],
        )
