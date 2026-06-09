"""Tests for fix-up-redirectors p4cli helpers."""

import sys
from pathlib import Path

import pytest

_LIB_DIR = (
    Path(__file__).resolve().parent.parent.parent
    / "plugins"
    / "unreal-kit"
    / "skills"
    / "fix-up-redirectors"
    / "lib"
)
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import p4cli


class TestGetP4User:
    def test_uses_env_when_set(self, monkeypatch):
        monkeypatch.setenv("P4USER", "alice")
        # Even if `p4 info` would return something else, env wins.
        monkeypatch.setattr(p4cli, "run_p4", lambda *a, **kw: (0, "bob\n", ""))
        assert p4cli.get_p4_user() == "alice"

    def test_falls_back_to_p4_info(self, monkeypatch):
        monkeypatch.delenv("P4USER", raising=False)
        monkeypatch.setattr(p4cli, "run_p4", lambda *a, **kw: (0, "User name: carol\n", ""))
        assert p4cli.get_p4_user() == "carol"

    def test_returns_empty_when_p4_fails(self, monkeypatch):
        monkeypatch.delenv("P4USER", raising=False)
        monkeypatch.setattr(p4cli, "run_p4", lambda *a, **kw: (1, "", "p4 not configured"))
        assert p4cli.get_p4_user() == ""

    def test_strips_whitespace(self, monkeypatch):
        monkeypatch.setenv("P4USER", "  alice  ")
        assert p4cli.get_p4_user() == "alice"

    def test_empty_env_falls_through_to_p4_info(self, monkeypatch):
        # P4USER set but blank should not short-circuit.
        monkeypatch.setenv("P4USER", "   ")
        monkeypatch.setattr(p4cli, "run_p4", lambda *a, **kw: (0, "User name: dan\n", ""))
        assert p4cli.get_p4_user() == "dan"


class TestDeleteFiles:
    def test_calls_p4_delete_with_cl(self, monkeypatch):
        captured = []

        def fake_run_p4_or_die(args, stdin=None, what=None):
            captured.append((tuple(args), stdin))
            return ""

        monkeypatch.setattr(p4cli, "run_p4_or_die", fake_run_p4_or_die)
        p4cli.delete_files("12345", ["/a/b.uasset", "/a/c.uasset"])

        assert len(captured) == 1
        args, stdin = captured[0]
        assert args == ("-x", "-", "delete", "-c", "12345")
        assert stdin == "/a/b.uasset\n/a/c.uasset"

    def test_batches_large_inputs(self, monkeypatch):
        captured = []

        def fake_run_p4_or_die(args, stdin=None, what=None):
            captured.append(stdin.count("\n") + 1)
            return ""

        monkeypatch.setattr(p4cli, "run_p4_or_die", fake_run_p4_or_die)
        files = [f"/a/{i}.uasset" for i in range(450)]
        p4cli.delete_files("12345", files, batch_size=200)

        assert captured == [200, 200, 50]
