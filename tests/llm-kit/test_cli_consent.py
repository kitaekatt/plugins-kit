"""``llm-kit consent``: show, partial-update set (global and scoped), and the group wiring.

Every test runs against the real consent store under a throwaway HOME (the
``home`` fixture), so the CLI is checked end to end through
``llm_kit.registry.consent`` rather than against a stub of it.
"""

from __future__ import annotations

import json
from pathlib import Path

from llm_kit.cli import consent as consent_cli
from test_cli_harness import final_mods, home, run  # noqa: F401 -- fixtures

NOTICE = (
    "With subscription or metered on, operations that opt in send their inputs unredacted "
    "to the providers of those models."
)


def _consent_file(home: Path) -> Path:
    return home / ".config" / "llm-kit" / "consent.json"


def _stored(home: Path) -> dict:
    return json.loads(_consent_file(home).read_text(encoding="utf-8"))


def _classes(home: Path) -> tuple:
    data = _stored(home)
    return (data["local"], data["subscription"], data["metered"])


class TestShow:
    def test_show_json_absent_is_the_default_policy(self, final_mods, home, capsys):
        out = run(final_mods.cli.main, ["consent", "show", "--json"], capsys)
        assert out.code == 0
        payload = json.loads(out.out)
        assert set(payload) == {"scope", "source", "path", "write_path", "classes", "layer"}
        assert payload["scope"] is None and payload["source"] == "default"
        assert payload["write_path"] == payload["path"] == str(_consent_file(home))
        assert {k: v["allowed"] for k, v in payload["classes"].items()} == {
            "local": True, "subscription": True, "metered": False,
        }
        assert {v["provenance"] for v in payload["classes"].values()} == {"default"}
        assert payload["classes"]["metered"]["commands"] == ["llm-kit consent set --metered on"]
        assert payload["layer"]["optional"] is True  # the layer is described, not applied
        assert not _consent_file(home).exists()  # show never writes
        assert not (home / ".config").exists()

    def test_show_text_prints_the_policy_and_that_the_layer_is_optional(self, final_mods, home, capsys):
        out = run(final_mods.cli.main, ["consent", "show"], capsys)
        assert out.code == 0
        lines = out.out.splitlines()
        assert lines[0] == "scope: (global)" and lines[1] == "source: default"
        assert lines[2] == f"path: {_consent_file(home)}" and lines[3] == f"write_path: {_consent_file(home)}"
        assert lines[4] == "local: on (default)" and lines[5] == "subscription: on (default)"
        assert lines[6].startswith("metered: off (default); to allow: `llm-kit consent set --metered on`")
        assert "optional" in lines[7] and "opts in" in lines[7]

    def test_show_reads_the_stored_file(self, final_mods, home, capsys):
        run(final_mods.cli.main, ["consent", "set", "--metered", "on"], capsys)
        payload = json.loads(run(final_mods.cli.main, ["consent", "show", "--json"], capsys).out)
        assert payload["source"] == "file"
        assert payload["classes"]["metered"] == {
            "allowed": True, "provenance": "global", "global": True, "scope": None,
            "blocked_by_global": False, "commands": [],
        }
        text = run(final_mods.cli.main, ["consent", "show"], capsys).out.splitlines()
        assert text[1] == "source: file" and text[6] == "metered: on (global)"

    def test_show_a_scope_reports_each_value_and_what_blocks_it(self, final_mods, home, capsys):
        run(final_mods.cli.main, ["consent", "set", "--scope", "databench", "--metered", "on"], capsys)
        run(final_mods.cli.main, ["consent", "set", "--scope", "databench", "--subscription", "off"], capsys)
        payload = json.loads(
            run(final_mods.cli.main, ["consent", "show", "--scope", "databench", "--json"], capsys).out
        )
        classes = payload["classes"]
        assert payload["scope"] == "databench"
        # a scope `on` that the global policy blocks is shown as blocked, naming the global command
        assert classes["metered"]["allowed"] is False and classes["metered"]["blocked_by_global"] is True
        assert classes["metered"]["commands"] == ["llm-kit consent set --metered on"]
        assert classes["subscription"]["allowed"] is False and classes["subscription"]["provenance"] == "scope"
        assert classes["subscription"]["commands"] == ["llm-kit consent set --scope databench --subscription on"]
        assert classes["local"]["allowed"] is True
        text = run(final_mods.cli.main, ["consent", "show", "--scope", "databench"], capsys).out
        assert "blocks it: run `llm-kit consent set --metered on`" in text

    def test_show_a_malformed_file_is_a_configuration_error(self, final_mods, home, capsys):
        target = _consent_file(home)
        target.parent.mkdir(parents=True)
        target.write_text("{broken", encoding="utf-8")
        out = run(final_mods.cli.main, ["consent", "show"], capsys)
        assert out.code == 2 and out.out == ""
        error = json.loads(out.err)["error"]
        assert error["kind"] == "configuration" and "not valid JSON" in error["message"]


class TestSet:
    def test_set_partial_preserves_other(self, final_mods, home, capsys):
        assert run(final_mods.cli.main, ["consent", "set", "--metered", "on"], capsys).code == 0
        assert _classes(home) == (True, True, True)
        assert run(final_mods.cli.main, ["consent", "set", "--subscription", "off"], capsys).code == 0
        assert _classes(home) == (True, False, True)
        assert run(final_mods.cli.main, ["consent", "set", "--metered", "off"], capsys).code == 0
        assert _classes(home) == (True, False, False)

    def test_set_all_flags_in_one_call(self, final_mods, home, capsys):
        out = run(
            final_mods.cli.main,
            ["consent", "set", "--local", "off", "--subscription", "off", "--metered", "on"],
            capsys,
        )
        assert out.code == 0
        assert _classes(home) == (False, False, True)

    def test_scoped_set_writes_only_the_scope_and_a_global_set_keeps_it(self, final_mods, home, capsys):
        assert run(final_mods.cli.main, ["consent", "set", "--scope", "task-kit", "--metered", "off"], capsys).code == 0
        assert _stored(home)["scopes"] == {"task-kit": {"metered": False}}
        assert _classes(home) == (True, True, False)
        assert run(final_mods.cli.main, ["consent", "set", "--metered", "on"], capsys).code == 0
        assert _stored(home)["scopes"] == {"task-kit": {"metered": False}}  # the global change kept the scope
        assert _classes(home) == (True, True, True)

    def test_set_without_a_flag_is_a_usage_error_and_writes_nothing(self, final_mods, home, capsys):
        out = run(final_mods.cli.main, ["consent", "set"], capsys)
        assert out.code == 2 and "--local" in out.err and "--metered" in out.err
        assert out.out == ""
        assert not _consent_file(home).exists()

    def test_set_rejects_the_retired_flags(self, final_mods, home, capsys):
        for retired in ("--usage", "--spend", "--free"):
            out = run(final_mods.cli.main, ["consent", "set", retired, "on"], capsys)
            assert out.code == 2, retired
        assert not _consent_file(home).exists()

    def test_set_rejects_a_value_other_than_on_or_off(self, final_mods, home, capsys):
        out = run(final_mods.cli.main, ["consent", "set", "--metered", "maybe"], capsys)
        assert out.code == 2 and "invalid choice: 'maybe'" in out.err
        assert not _consent_file(home).exists()

    def test_set_rejects_an_empty_scope(self, final_mods, home, capsys):
        out = run(final_mods.cli.main, ["consent", "set", "--scope", " ", "--metered", "on"], capsys)
        assert out.code == 2 and json.loads(out.err)["error"]["kind"] == "configuration"
        assert not _consent_file(home).exists()

    def test_set_output_states_the_unredacted_send_and_that_the_layer_is_optional(self, final_mods, home, capsys):
        out = run(final_mods.cli.main, ["consent", "set", "--metered", "on"], capsys)
        assert out.code == 0
        assert NOTICE in out.out.splitlines()
        assert f"Consent written to {_consent_file(home)}" in out.out
        assert "metered: on (global)" in out.out
        assert any("optional" in line for line in out.out.splitlines())

    def test_set_refuses_to_overwrite_a_malformed_file(self, final_mods, home, capsys):
        target = _consent_file(home)
        target.parent.mkdir(parents=True)
        target.write_text('{"schema": 1, "usage": true, "spend": true}', encoding="utf-8")
        out = run(final_mods.cli.main, ["consent", "set", "--metered", "on"], capsys)
        assert out.code == 2 and "usage/spend" in json.loads(out.err)["error"]["message"]
        assert target.read_text(encoding="utf-8") == '{"schema": 1, "usage": true, "spend": true}'

    def test_set_write_failure_is_a_configuration_error(self, final_mods, home, capsys, monkeypatch):
        def boom(*_: object, **__: object):
            raise PermissionError("read-only")

        monkeypatch.setattr(consent_cli.consent_store, "update_config", boom)
        out = run(final_mods.cli.main, ["consent", "set", "--metered", "on"], capsys)
        assert out.code == 2 and out.out == ""
        assert json.loads(out.err)["error"]["kind"] == "configuration"


class TestWiring:
    def test_the_consent_group_is_routed(self, final_mods, capsys):
        out = run(final_mods.cli.main, ["consent", "--help"], capsys)
        assert out.code == 0 and "usage: llm-kit consent" in out.out

    def test_the_verbs_parse_their_help(self, final_mods, capsys):
        for verb in ("show", "set"):
            assert run(final_mods.cli.main, ["consent", verb, "--help"], capsys).code == 0, verb

    def test_the_group_is_listed_in_the_top_level_help(self, final_mods, capsys):
        out = run(final_mods.cli.main, ["--help"], capsys)
        assert out.code == 0 and "consent" in out.out
