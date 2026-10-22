"""``llm-kit consent``: show, partial-update set, and the group wiring.

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
    "With usage or spend on, operations that opt in send their inputs unredacted "
    "to the providers of those models."
)


def _consent_file(home: Path) -> Path:
    return home / ".claude" / "plugins" / "data" / "plugins-kit" / "llm-kit" / "consent.json"


def _stored(home: Path) -> dict:
    return json.loads(_consent_file(home).read_text(encoding="utf-8"))


class TestShow:
    def test_show_json_absent(self, final_mods, home, capsys):
        out = run(final_mods.cli.main, ["consent", "show", "--json"], capsys)
        assert out.code == 0
        payload = json.loads(out.out)
        assert set(payload) == {"usage", "spend", "source", "path", "note"}
        assert payload["usage"] is False and payload["spend"] is False
        assert payload["source"] == "absent"
        assert payload["path"] == str(_consent_file(home))
        assert payload["note"]  # absence is explained, not silent
        assert not _consent_file(home).exists()  # show never writes

    def test_show_text_prints_every_field(self, final_mods, home, capsys):
        out = run(final_mods.cli.main, ["consent", "show"], capsys)
        assert out.code == 0
        lines = out.out.splitlines()
        assert lines[0] == "usage: off" and lines[1] == "spend: off"
        assert lines[2] == "source: absent"
        assert lines[3] == f"path: {_consent_file(home)}"
        assert lines[4].startswith("note: ") and len(lines[4]) > len("note: ")

    def test_show_reads_the_stored_file(self, final_mods, home, capsys):
        run(final_mods.cli.main, ["consent", "set", "--usage", "on"], capsys)
        payload = json.loads(run(final_mods.cli.main, ["consent", "show", "--json"], capsys).out)
        assert payload["usage"] is True and payload["spend"] is False
        assert payload["source"] == "file" and payload["note"] == ""
        text = run(final_mods.cli.main, ["consent", "show"], capsys).out
        assert text.splitlines()[:3] == ["usage: on", "spend: off", "source: file"]


class TestSet:
    def test_set_partial_preserves_other(self, final_mods, home, capsys):
        assert run(final_mods.cli.main, ["consent", "set", "--spend", "on"], capsys).code == 0
        assert (_stored(home)["usage"], _stored(home)["spend"]) == (False, True)
        assert run(final_mods.cli.main, ["consent", "set", "--usage", "on"], capsys).code == 0
        assert (_stored(home)["usage"], _stored(home)["spend"]) == (True, True)
        assert run(final_mods.cli.main, ["consent", "set", "--usage", "off"], capsys).code == 0
        assert (_stored(home)["usage"], _stored(home)["spend"]) == (False, True)

    def test_set_spend_preserves_usage(self, final_mods, home, capsys):
        assert run(final_mods.cli.main, ["consent", "set", "--usage", "on"], capsys).code == 0
        assert run(final_mods.cli.main, ["consent", "set", "--spend", "on"], capsys).code == 0
        assert (_stored(home)["usage"], _stored(home)["spend"]) == (True, True)
        assert run(final_mods.cli.main, ["consent", "set", "--spend", "off"], capsys).code == 0
        assert (_stored(home)["usage"], _stored(home)["spend"]) == (True, False)

    def test_set_both_flags_in_one_call(self, final_mods, home, capsys):
        out = run(final_mods.cli.main, ["consent", "set", "--usage", "on", "--spend", "on"], capsys)
        assert out.code == 0
        assert (_stored(home)["usage"], _stored(home)["spend"]) == (True, True)

    def test_set_without_a_flag_is_a_usage_error_and_writes_nothing(self, final_mods, home, capsys):
        out = run(final_mods.cli.main, ["consent", "set"], capsys)
        assert out.code == 2 and "--usage" in out.err and "--spend" in out.err
        assert out.out == ""
        assert not _consent_file(home).exists()

    def test_set_rejects_a_value_other_than_on_or_off(self, final_mods, home, capsys):
        out = run(final_mods.cli.main, ["consent", "set", "--usage", "maybe"], capsys)
        assert out.code == 2 and "invalid choice: 'maybe'" in out.err
        assert not _consent_file(home).exists()

    def test_set_output_states_the_unredacted_send(self, final_mods, home, capsys):
        out = run(final_mods.cli.main, ["consent", "set", "--usage", "on"], capsys)
        assert out.code == 0
        assert NOTICE in out.out.splitlines()
        assert f"Consent written to {_consent_file(home)}" in out.out
        assert "usage: on" in out.out and "spend: off" in out.out

    def test_set_write_failure_is_a_configuration_error(self, final_mods, home, capsys, monkeypatch):
        def boom(**_: object) -> Path:
            raise PermissionError("read-only")

        monkeypatch.setattr(consent_cli.consent_store, "save_consent", boom)
        out = run(final_mods.cli.main, ["consent", "set", "--usage", "on"], capsys)
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
