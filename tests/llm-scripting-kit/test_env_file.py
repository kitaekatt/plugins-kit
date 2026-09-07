"""Tests for llm_scripting_kit.env_file."""

import os
import sys

import pytest

from llm_scripting_kit.env_file import read_env_file, write_env_file


class TestReadEnvFile:
    def test_missing_file_returns_empty_dict(self, tmp_path):
        assert read_env_file(tmp_path / "nope.env") == {}

    def test_simple_kv(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("A=1\nB=two\n")
        assert read_env_file(env) == {"A": "1", "B": "two"}

    def test_skips_blank_and_comment_lines(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("\n# a comment\nA=1\n\nB=2\n# trailing comment\n")
        assert read_env_file(env) == {"A": "1", "B": "2"}

    def test_strips_double_quotes(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text('KEY="value with spaces"\n')
        assert read_env_file(env) == {"KEY": "value with spaces"}

    def test_strips_single_quotes(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("KEY='value'\n")
        assert read_env_file(env) == {"KEY": "value"}

    def test_raises_on_missing_equals(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("BROKEN_LINE\n")
        with pytest.raises(ValueError, match="missing '='"):
            read_env_file(env)

    def test_strips_leading_export_shell_idiom(self, tmp_path):
        """I3: `export KEY=value` used to yield the key "export KEY" silently
        -- a malformed line that should be surfaced loudly, per the module
        docstring, instead reads a garbage key with no error at all."""
        env = tmp_path / ".env"
        env.write_text("export FOO=bar\n")
        assert read_env_file(env) == {"FOO": "bar"}

    def test_export_idiom_with_quoted_value(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text('export FOO="bar baz"\n')
        assert read_env_file(env) == {"FOO": "bar baz"}


class TestQuotingRoundTrip:
    def test_padded_value_round_trips_byte_identically(self, tmp_path):
        env = tmp_path / ".env"
        write_env_file(env, {"KEY": "  padded value  "})
        assert read_env_file(env) == {"KEY": "  padded value  "}

    def test_embedded_quote_value_round_trips_byte_identically(self, tmp_path):
        env = tmp_path / ".env"
        write_env_file(env, {"KEY": 'has "a quote" inside'})
        assert read_env_file(env) == {"KEY": 'has "a quote" inside'}

    def test_embedded_backslash_round_trips_byte_identically(self, tmp_path):
        env = tmp_path / ".env"
        write_env_file(env, {"KEY": "back\\slash"})
        assert read_env_file(env) == {"KEY": "back\\slash"}

    def test_plain_value_is_not_quoted(self, tmp_path):
        """No unnecessary quoting -- a value needing no protection round-trips
        exactly as before, so an existing hand-edited .env stays diffable."""
        env = tmp_path / ".env"
        write_env_file(env, {"KEY": "plain-value"})
        assert env.read_text(encoding="utf-8") == "KEY=plain-value\n"


class TestWriteEnvFile:
    def test_round_trip(self, tmp_path):
        env = tmp_path / ".env"
        write_env_file(env, {"A": "1", "B": "two"})
        assert read_env_file(env) == {"A": "1", "B": "two"}

    def test_creates_parent_dirs(self, tmp_path):
        env = tmp_path / "a" / "b" / "c" / ".env"
        write_env_file(env, {"K": "v"})
        assert env.is_file()
        assert read_env_file(env) == {"K": "v"}

    def test_overwrites_existing(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("OLD=value\n")
        write_env_file(env, {"NEW": "value"})
        assert read_env_file(env) == {"NEW": "value"}

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions only")
    def test_permissions_0600_on_posix(self, tmp_path):
        env = tmp_path / ".env"
        write_env_file(env, {"K": "v"})
        mode = env.stat().st_mode & 0o777
        assert mode == 0o600

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions only")
    def test_permissions_0600_even_under_permissive_umask(self, tmp_path):
        """The mode comes from os.open's 0600 at creation, not from the umask."""
        env = tmp_path / ".env"
        old_umask = os.umask(0o000)
        try:
            write_env_file(env, {"K": "v"})
        finally:
            os.umask(old_umask)
        assert env.stat().st_mode & 0o777 == 0o600

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions only")
    def test_temp_file_never_world_readable(self, tmp_path, monkeypatch):
        """Regression for the world-readable window: the temp file the key is
        written to must already be 0600 when it is renamed into place (the old
        code created it umask-default and only chmod'ed after os.replace)."""
        env = tmp_path / ".env"
        seen = {}
        real_replace = os.replace

        def spy(src, dst):
            seen["tmp_mode"] = os.stat(src).st_mode & 0o777
            return real_replace(src, dst)

        monkeypatch.setattr("llm_scripting_kit.env_file.os.replace", spy)
        old_umask = os.umask(0o000)
        try:
            write_env_file(env, {"K": "v"})
        finally:
            os.umask(old_umask)
        assert seen["tmp_mode"] == 0o600

    def test_atomic_no_partial_file_on_io_error(self, tmp_path, monkeypatch):
        env = tmp_path / ".env"
        env.write_text("ORIGINAL=keep\n")

        # Force os.replace to raise; the temp file write completed but the
        # rename did not. Original file should be untouched.
        original_replace = os.replace

        def boom(src, dst):
            raise OSError("simulated rename failure")

        monkeypatch.setattr("llm_scripting_kit.env_file.os.replace", boom)
        with pytest.raises(OSError, match="simulated"):
            write_env_file(env, {"NEW": "value"})

        assert env.read_text() == "ORIGINAL=keep\n"
