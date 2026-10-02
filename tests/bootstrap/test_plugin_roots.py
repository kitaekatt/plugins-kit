"""The pre-gate env records: ``plugin_roots`` and ``tool_bins``.

Three layers, one per owner:

- the record format itself (``bootstrap_lib/env_var_check.py``),
- the engine writing it on a full pass (``_maintain_env_records``),
- ``session-bootstrap.sh``'s prelude re-emitting it in a session whose pass
  was short-circuited by a skip gate, which is the whole point: both gates
  return before the engine runs, so ``<PLUGIN>_ROOT`` and
  ``BOOTSTRAP_BIN_<TOOL>`` were absent from most sessions.

The bash harness is the one in ``test_sessionstart_interpreter_env``, reused
rather than re-derived -- it already arranges a temporary HOME holding a fake
deterministic interpreter, a redirected ``CLAUDE_BOOTSTRAP_DATA_ROOT``, and a
fake ``uname``. Every test here seeds the Layer-1 guard, so the hook exits
right after the prelude and never provisions anything.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from bootstrap_lib.env_var_check import (
    PLUGIN_ROOTS_FILENAME,
    TOOL_BINS_FILENAME,
    plugin_root_env_var_name,
    plugin_roots_record_path,
    read_env_record,
    tool_bins_record_path,
    write_env_record,
)
from bootstrap.link_compat import link_tree
from bootstrap.test_engine_multiplugin import (
    BOOTSTRAP_ROOT,
    _isolated_env,
    _write_minimal_defaults,
    run_engine,
)
from bootstrap.test_sessionstart_interpreter_env import (
    HOOK,
    PRELUDE_END,
    _env,
    _run,
    _scaffold,
    needs_bash,
)

BLOCK_START = "# --- Recorded env names for this session"


class TestRecordFormat:
    """``NAME=path`` lines, written and read by the two owners."""

    def test_round_trips_a_name_and_a_path(self, tmp_path):
        path = str(tmp_path / PLUGIN_ROOTS_FILENAME)
        wrote, kept = write_env_record(path, {"HUE_KIT_ROOT": "/plugins/hue-kit/0.9.1"})
        assert wrote and list(kept) == ["HUE_KIT_ROOT"]
        assert Path(path).read_text(encoding="utf-8") == \
            "HUE_KIT_ROOT=/plugins/hue-kit/0.9.1\n"
        assert read_env_record(path) == {"HUE_KIT_ROOT": "/plugins/hue-kit/0.9.1"}

    def test_a_value_holding_a_single_quote_is_never_written(self, tmp_path):
        # The prelude re-emits the value inside single quotes into a file that
        # is SOURCED, so one quote would turn the rest of it into code.
        path = str(tmp_path / PLUGIN_ROOTS_FILENAME)
        wrote, kept = write_env_record(path, {
            "ODD_ROOT": "/plugins/it's/0.1.0",
            "FINE_ROOT": "/plugins/fine/0.1.0",
        })
        assert wrote and list(kept) == ["FINE_ROOT"]
        assert "it's" not in Path(path).read_text(encoding="utf-8")

    def test_an_unparsable_line_is_dropped_on_read(self, tmp_path):
        # Repairs a record damaged by a partial write instead of carrying the
        # damage into every session, same choice as session_env._read_existing.
        path = tmp_path / PLUGIN_ROOTS_FILENAME
        path.write_text(
            "GOOD_ROOT=/plugins/good/1.0.0\n"
            "no-equals-sign\n"
            "lower_case_root=/plugins/bad/1.0.0\n"
            "BAD ROOT=/plugins/bad/1.0.0\n"
            "QUOTED_ROOT=/plugins/it's/1.0.0\n"
            "EMPTY_ROOT=\n",
            encoding="utf-8",
        )
        assert read_env_record(str(path)) == {"GOOD_ROOT": "/plugins/good/1.0.0"}

    def test_the_whole_file_is_rewritten_so_a_dropped_name_disappears(self, tmp_path):
        # The record-level half of registry-change self-correction.
        path = str(tmp_path / PLUGIN_ROOTS_FILENAME)
        write_env_record(path, {"A_ROOT": "/a", "B_ROOT": "/b"})
        write_env_record(path, {"A_ROOT": "/a"})
        assert read_env_record(path) == {"A_ROOT": "/a"}

    def test_the_two_records_are_separate_files(self, tmp_path):
        assert plugin_roots_record_path(str(tmp_path)).endswith(PLUGIN_ROOTS_FILENAME)
        assert tool_bins_record_path(str(tmp_path)).endswith(TOOL_BINS_FILENAME)
        assert plugin_roots_record_path(str(tmp_path)) != tool_bins_record_path(str(tmp_path))


def _fake_tree(tmp_path, plugins, *, bootstrap_json=True):
    """A bootstrap plugin root plus a registry listing ``plugins``."""
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir(exist_ok=True)
    fake_root = plugins_dir / "bootstrap"
    fake_root.mkdir(exist_ok=True)
    link_tree(fake_root / "bootstrap_lib", os.path.join(BOOTSTRAP_ROOT, "bootstrap_lib"))
    link_tree(fake_root / "engine", os.path.join(BOOTSTRAP_ROOT, "engine"))
    _write_minimal_defaults(fake_root)
    (fake_root / "bootstrap.json").write_text(json.dumps({}))
    return fake_root, _rewrite_registry(tmp_path, plugins, bootstrap_json=bootstrap_json)


def _rewrite_registry(tmp_path, plugins, *, bootstrap_json=True):
    """Re-point the registry and config at exactly ``plugins``."""
    plugins_dir = tmp_path / "plugins"
    registry = {"plugins": {}}
    for name in plugins:
        install = plugins_dir / name
        install.mkdir(exist_ok=True)
        if bootstrap_json:
            (install / "bootstrap.json").write_text(json.dumps({}))
        registry["plugins"][f"kit:{name}"] = [
            {"installPath": f"./{name}", "version": "1.0.0"},
        ]
    (plugins_dir / "installed_plugins.json").write_text(json.dumps(registry))

    data_dir = tmp_path / "data" / "kit" / "bootstrap"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "config.json").write_text(json.dumps({
        "schema_version": 3,
        "enabled_plugins": [f"kit:{n}" for n in plugins],
        "log_level": "info", "log_success_shell": False, "log_success_checks": True,
    }))
    return data_dir


class TestEnginePass:
    """A full pass records what it exported."""

    def test_a_pass_records_each_plugin_root(self, tmp_path):
        fake_root, data_dir = _fake_tree(tmp_path, ["alpha-kit", "beta-kit"])
        result = run_engine(str(data_dir), plugin_root=str(fake_root),
                            env=_isolated_env(tmp_path))
        assert result.returncode == 0, result.stderr
        record = read_env_record(plugin_roots_record_path(str(data_dir)))
        assert record[plugin_root_env_var_name("alpha-kit")] == \
            str(tmp_path / "plugins" / "alpha-kit")
        assert plugin_root_env_var_name("beta-kit") in record

    def test_a_plugin_that_left_the_registry_loses_its_line(self, tmp_path):
        """Registry-change self-correction, end to end."""
        fake_root, data_dir = _fake_tree(tmp_path, ["alpha-kit", "beta-kit"])
        assert run_engine(str(data_dir), plugin_root=str(fake_root),
                          env=_isolated_env(tmp_path)).returncode == 0
        assert plugin_root_env_var_name("beta-kit") in \
            read_env_record(plugin_roots_record_path(str(data_dir)))

        _rewrite_registry(tmp_path, ["alpha-kit"])  # beta-kit leaves the registry
        assert run_engine(str(data_dir), plugin_root=str(fake_root),
                          env=_isolated_env(tmp_path)).returncode == 0
        record = read_env_record(plugin_roots_record_path(str(data_dir)))
        assert plugin_root_env_var_name("alpha-kit") in record
        assert plugin_root_env_var_name("beta-kit") not in record

    def test_a_plugin_with_no_bootstrap_json_is_not_recorded(self, tmp_path):
        """The gate the below-gate export sits behind, asserted on the writer.

        Driven directly rather than through a pass, because Step 4's own
        discovery (``list_enabled_plugins``) already drops a plugin with no
        ``bootstrap.json`` -- so an end-to-end run passes whether or not
        ``_maintain_env_records`` keeps its own gate, and would be silent if
        the writer were ever handed a wider list.
        """
        from types import SimpleNamespace

        from bootstrap_lib.engine import _maintain_env_records

        (tmp_path / "with").mkdir()
        (tmp_path / "with" / "bootstrap.json").write_text("{}", encoding="utf-8")
        (tmp_path / "without").mkdir()
        oks, quiets = [], []
        _maintain_env_records(str(tmp_path), [
            SimpleNamespace(name="with-kit", install_path=str(tmp_path / "with")),
            SimpleNamespace(name="bare-kit", install_path=str(tmp_path / "without")),
        ], oks, quiets)
        record = read_env_record(plugin_roots_record_path(str(tmp_path)))
        assert plugin_root_env_var_name("with-kit") in record
        assert plugin_root_env_var_name("bare-kit") not in record
        assert any("plugin roots" in e for e in oks + quiets), \
            "every bootstrap operation logs its outcome"


class TestPreludeStatic:
    """Text-level contract for the new prelude block; no bash needed."""

    def _block(self) -> str:
        text = HOOK.read_text(encoding="utf-8")
        start = text.index(BLOCK_START)
        return text[start:text.index(PRELUDE_END, start)]

    def test_the_block_sits_before_both_skip_gates(self):
        text = HOOK.read_text(encoding="utf-8")
        start = text.index(BLOCK_START)
        assert start < text.index('cat "$_GUARD_FILE"'), "must precede Layer 1"
        assert start < text.index('if [ -f "$_COOLDOWN_FILE" ]'), "must precede Layer 2"
        # Inside the prelude slice, so test_sessionstart_interpreter_env's
        # bash-3.2/fork guard covers this block too.
        assert text.index("# --- Interpreter names for this session") < start

    def test_both_records_are_read_with_their_own_existence_test(self):
        block = self._block()
        assert '_pr_scan "$PLUGIN_DATA/plugin_roots" d' in block
        assert '_pr_scan "$PLUGIN_DATA/tool_bins" f' in block
        assert '[ -d "$_pr_path" ] || continue' in block, \
            "a plugin root must be verified as a directory"
        assert '[ -f "$_pr_path" ] || continue' in block, \
            "a tool path must be verified as a regular file"

    def test_posix_only_constructs(self):
        """bash 3.2 and zsh; no Mac is available to run this on."""
        import re
        code = "\n".join(line for line in self._block().splitlines()
                         if not line.lstrip().startswith("#"))
        forbidden = {
            r"\$\{[A-Za-z_]+\^": "case modification (bash 4)",
            r"\$\{[A-Za-z_]+,": "case modification (bash 4)",
            r"\bdeclare\s+-[aAn]\b": "declare -A/-a/-n",
            r"\b(mapfile|readarray|coproc)\b": "bash 4 builtin",
            r"\bread\b[^\n]*\s-[pa]\b": "read -p/-a (dies under zsh)",
            r"\blocal\s+-n\b": "nameref",
            r"\|&|&>>|;;&|;&": "bash 4 operator",
            r"@[QEPAa]\}": "parameter transformation",
            r"\[\[": "[[ ]] (keep to POSIX test)",
            r"\bwait\s+-n\b": "wait -n (bash 4.3)",
            r"`": "backtick substitution",
        }
        for pattern, why in forbidden.items():
            assert not re.search(pattern, code), f"block uses {why}: {pattern}"
        # No array at all, so the ${ARR[@]+"${ARR[@]}"} guard cannot be got
        # wrong here -- a possibly-empty "${ARR[@]}" is FATAL before bash 4.4.
        assert not re.search(r"\b[A-Za-z_]+=\(", code), "no arrays in this block"
        assert "[@]" not in code, "no array expansion in this block"
        # Fork-free, like the block above it.
        assert "$(" not in code and "<(" not in code, "the block must not fork"


@needs_bash
class TestPreludeReEmit:
    """A gate-skipped session: the hook exits after the prelude."""

    def _seeded(self, tmp_path, roots=None, bins=None):
        scaffold = _scaffold(tmp_path)
        scaffold.seed_guard()
        if roots is not None:
            write_env_record(plugin_roots_record_path(str(scaffold.plugin_data)), roots)
        if bins is not None:
            write_env_record(tool_bins_record_path(str(scaffold.plugin_data)), bins)
        return scaffold

    def test_a_recorded_root_reaches_a_gate_skipped_session(self, tmp_path):
        live = tmp_path / "cache" / "hue-kit" / "0.9.1"
        live.mkdir(parents=True)
        scaffold = self._seeded(tmp_path, roots={"HUE_KIT_ROOT": live.as_posix()})
        result = _run(scaffold)
        assert result.returncode == 0, result.stderr
        assert scaffold.exports("HUE_KIT_ROOT") == [f"'{live.as_posix()}'"]

    def test_a_recorded_path_that_no_longer_exists_is_skipped(self, tmp_path):
        """The user's ruling: a stale record degrades to the loud :? failure."""
        gone = (tmp_path / "cache" / "hue-kit" / "0.9.0").as_posix()
        live = tmp_path / "cache" / "p4-kit" / "1.0.0"
        live.mkdir(parents=True)
        scaffold = self._seeded(tmp_path, roots={
            "HUE_KIT_ROOT": gone, "P4_KIT_ROOT": live.as_posix(),
        })
        result = _run(scaffold)
        assert result.returncode == 0, result.stderr
        assert scaffold.exports("HUE_KIT_ROOT") == [], \
            "a deleted version directory must NOT be exported"
        assert scaffold.exports("P4_KIT_ROOT") == [f"'{live.as_posix()}'"]

    def test_a_tool_record_needs_a_regular_file_not_a_directory(self, tmp_path):
        a_dir = tmp_path / "bin" / "git"
        a_dir.mkdir(parents=True)
        a_file = tmp_path / "bin" / "jq.exe"
        a_file.write_text("", encoding="utf-8")
        scaffold = self._seeded(tmp_path, bins={
            "BOOTSTRAP_BIN_GIT": a_dir.as_posix(),
            "BOOTSTRAP_BIN_JQ": a_file.as_posix(),
        })
        result = _run(scaffold)
        assert result.returncode == 0, result.stderr
        assert scaffold.exports("BOOTSTRAP_BIN_GIT") == [], \
            "a directory is not a tool"
        assert scaffold.exports("BOOTSTRAP_BIN_JQ") == [f"'{a_file.as_posix()}'"]

    def test_an_existing_export_line_is_never_written_again(self, tmp_path):
        """The engine-verified value wins; the prelude only fills the gap."""
        live = tmp_path / "cache" / "hue-kit" / "0.9.1"
        live.mkdir(parents=True)
        scaffold = self._seeded(tmp_path, roots={"HUE_KIT_ROOT": live.as_posix()})
        scaffold.env_file.write_text("export HUE_KIT_ROOT='/verified/by/the/engine'\n",
                                     encoding="utf-8", newline="\n")
        result = _run(scaffold)
        assert result.returncode == 0, result.stderr
        assert scaffold.exports("HUE_KIT_ROOT") == ["'/verified/by/the/engine'"]

    def test_the_append_starts_on_its_own_line(self, tmp_path):
        """A last line with no trailing newline would fuse two exports.

        Both interpreter names are pre-seeded so the block ABOVE appends
        nothing -- otherwise its own separator repairs the missing newline
        first and this block's separator is never exercised.
        """
        live = tmp_path / "cache" / "hue-kit" / "0.9.1"
        live.mkdir(parents=True)
        scaffold = self._seeded(tmp_path, roots={"HUE_KIT_ROOT": live.as_posix()})
        scaffold.env_file.write_text(
            "export BOOTSTRAP_PYTHON='/x'\n"
            "export BOOTSTRAP_PROJECT_PYTHON='/x'\n"
            "export SOMETHING_ELSE='x'",
            encoding="utf-8", newline="\n")
        result = _run(scaffold)
        assert result.returncode == 0, result.stderr
        assert scaffold.exports("HUE_KIT_ROOT") == [f"'{live.as_posix()}'"]
        assert scaffold.exports("SOMETHING_ELSE") == ["'x'"]

    def test_a_hand_edited_record_cannot_inject_shell_code(self, tmp_path):
        """The engine drops a quoted value at write time; so does the prelude.

        Two barriers cover this, and either alone is enough on a filesystem
        that refuses a quote in a filename: the explicit quote refusal, and
        the existence check (the forged path does not exist). The property
        under test is that NOTHING injected reaches the env file -- see the
        revert evidence, where removing both barriers is what turns it red.
        """
        live = tmp_path / "cache" / "odd" / "0.1.0"
        live.mkdir(parents=True)
        scaffold = self._seeded(tmp_path, roots={})
        record = Path(plugin_roots_record_path(str(scaffold.plugin_data)))
        record.write_text(f"ODD_ROOT={live.as_posix()}'; echo pwned\n",
                          encoding="utf-8", newline="\n")
        result = _run(scaffold)
        assert result.returncode == 0, result.stderr
        assert scaffold.exports("ODD_ROOT") == []
        assert "pwned" not in scaffold.env_file.read_text(encoding="utf-8")

    def test_an_absent_record_is_a_silent_no_op(self, tmp_path):
        # Silent matters: without the file test the redirect itself fails and
        # the hook spills a bash error naming the record onto stderr.
        scaffold = self._seeded(tmp_path)
        result = _run(scaffold)
        assert result.returncode == 0, result.stderr
        text = scaffold.env_file.read_text(encoding="utf-8") if \
            scaffold.env_file.exists() else ""
        assert "_ROOT=" not in text and "BOOTSTRAP_BIN_" not in text
        assert "plugin_roots" not in result.stderr
        assert "tool_bins" not in result.stderr

    def test_console_mode_writes_no_env_file(self, tmp_path):
        # CLAUDE_ENV_FILE is deliberately still set, so only the --console
        # test stands between the record and the file.
        live = tmp_path / "cache" / "hue-kit" / "0.9.1"
        live.mkdir(parents=True)
        scaffold = self._seeded(tmp_path, roots={"HUE_KIT_ROOT": live.as_posix()})
        result = _run(scaffold, "--console", env=_env(scaffold), stdin="")
        assert result.returncode == 0, result.stderr
        assert scaffold.exports("HUE_KIT_ROOT") == []
