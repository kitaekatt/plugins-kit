"""Tests for fix-up-redirectors p4cli helpers."""

import sys
import subprocess
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


class TestTimeoutBudgets:
    def test_query_uses_query_budget_even_with_global_x_option(self, monkeypatch):
        calls = []

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, "ok", "")

        monkeypatch.setenv("UNREAL_KIT_P4_QUERY_TIMEOUT_S", "1.25")
        monkeypatch.setenv("UNREAL_KIT_P4_MUTATION_TIMEOUT_S", "9")
        monkeypatch.setattr(p4cli.subprocess, "run", fake_run)

        result = p4cli.run_p4(["-x", "-", "fstat"], stdin="//depot/a")

        assert tuple(result) == (0, "ok", "")
        assert calls[0][1]["timeout"] == 1.25

    @pytest.mark.parametrize(
        "args",
        [
            ["edit", "-c", "123"],
            ["-x", "-", "delete", "-c", "123"],
            ["reopen", "-c", "123"],
            ["change", "-i"],
        ],
    )
    def test_mutations_use_mutation_budget(self, monkeypatch, args):
        calls = []

        def fake_run(command, **kwargs):
            calls.append(kwargs)
            return subprocess.CompletedProcess(command, 0, "", "")

        monkeypatch.setenv("UNREAL_KIT_P4_QUERY_TIMEOUT_S", "0.25")
        monkeypatch.setenv("UNREAL_KIT_P4_MUTATION_TIMEOUT_S", "7.5")
        monkeypatch.setattr(p4cli.subprocess, "run", fake_run)

        p4cli.run_p4(args)

        assert calls[0]["timeout"] == 7.5

    def test_large_mutation_batches_use_the_independent_larger_budget(self, monkeypatch):
        calls = []

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, "", "")

        monkeypatch.setenv("UNREAL_KIT_P4_QUERY_TIMEOUT_S", "0.25")
        monkeypatch.setenv("UNREAL_KIT_P4_MUTATION_TIMEOUT_S", "30")
        monkeypatch.setattr(p4cli.subprocess, "run", fake_run)

        p4cli.delete_files("123", [f"/a/{index}.uasset" for index in range(450)])

        assert len(calls) == 3
        assert all(kwargs["timeout"] == 30 for _command, kwargs in calls)

    def test_explicit_override_wins_over_selected_environment_budget(self, monkeypatch):
        calls = []
        monkeypatch.setenv("UNREAL_KIT_P4_QUERY_TIMEOUT_S", "1")
        monkeypatch.setenv("UNREAL_KIT_P4_MUTATION_TIMEOUT_S", "2")
        monkeypatch.setattr(
            p4cli.subprocess,
            "run",
            lambda command, **kwargs: (
                calls.append(kwargs)
                or subprocess.CompletedProcess(command, 0, "", "")
            ),
        )

        p4cli.run_p4(["delete", "//depot/a"], timeout_s=11)

        assert calls[0]["timeout"] == 11

    def test_unset_budgets_preserve_unbounded_legacy_call(self, monkeypatch):
        calls = []
        monkeypatch.delenv("UNREAL_KIT_P4_QUERY_TIMEOUT_S", raising=False)
        monkeypatch.delenv("UNREAL_KIT_P4_MUTATION_TIMEOUT_S", raising=False)
        monkeypatch.setattr(
            p4cli.subprocess,
            "run",
            lambda command, **kwargs: (
                calls.append(kwargs)
                or subprocess.CompletedProcess(command, 0, "", "")
            ),
        )

        p4cli.run_p4(["info"])

        assert calls[0]["timeout"] is None

    @pytest.mark.parametrize(
        "name,value,args",
        [
            ("UNREAL_KIT_P4_QUERY_TIMEOUT_S", "0", ["info"]),
            ("UNREAL_KIT_P4_MUTATION_TIMEOUT_S", "nan", ["delete", "//depot/a"]),
            ("UNREAL_KIT_P4_QUERY_TIMEOUT_S", "not-a-number", ["info"]),
        ],
    )
    def test_invalid_config_is_rejected_before_subprocess(self, monkeypatch, name, value, args):
        calls = []
        monkeypatch.setenv(name, value)
        monkeypatch.setattr(
            p4cli.subprocess,
            "run",
            lambda *args, **kwargs: calls.append((args, kwargs)),
        )

        with pytest.raises(p4cli.P4TimeoutConfigError):
            p4cli.run_p4(args)

        assert calls == []

    def test_timeout_returns_partial_evidence_without_retry(self, monkeypatch):
        calls = []

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            raise subprocess.TimeoutExpired(
                command, kwargs["timeout"], output="partial stdout", stderr="partial stderr"
            )

        monkeypatch.setenv("UNREAL_KIT_P4_QUERY_TIMEOUT_S", "1")
        monkeypatch.setattr(p4cli.subprocess, "run", fake_run)

        result = p4cli.run_p4(["fstat", "//depot/a"])

        assert result.returncode != 0
        assert result.timed_out is True
        assert "partial stdout" in result.stdout
        assert "partial stderr" in result.stderr
        assert "timed out" in result.stderr
        assert len(calls) == 1

    def test_timeout_is_phase_specific_for_die_wrapper(self, monkeypatch):
        monkeypatch.setenv("UNREAL_KIT_P4_MUTATION_TIMEOUT_S", "2")

        def fake_run(command, **kwargs):
            raise subprocess.TimeoutExpired(
                command, kwargs["timeout"], output="partial", stderr="child stopped"
            )

        monkeypatch.setattr(p4cli.subprocess, "run", fake_run)

        with pytest.raises(p4cli.P4TimeoutError) as exc_info:
            p4cli.run_p4_or_die(["delete", "//depot/a"], what="redirector delete")

        assert exc_info.value.label == "redirector delete"
        assert exc_info.value.result.timed_out is True
        assert "partial" in exc_info.value.result.stdout


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


class TestWhereRecords:
    def test_tagged_per_file_resolution_preserves_true_depot_identity(self, monkeypatch):
        calls = []

        def fake_run(args, stdin=None, what=None):
            calls.append((args, stdin))
            local = args[-1]
            if local.endswith("/Mapped.uasset"):
                return (
                    "... depotFile //depot/plugin/Content/Mapped.uasset\n"
                    "... clientFile //ws/plugin/Content/Mapped.uasset\n"
                    "... path C:/work/plugin/Content/Mapped.uasset\n"
                )
            return (
                "... depotFile //depot/game/Content/Other.uasset\n"
                "... clientFile //ws/game/Content/Other.uasset\n"
                "... path C:/work/game/Content/Other.uasset\n"
            )

        def fake_or_die(args, stdin=None, what=None):
            result = fake_run(args, stdin, what)
            return result

        monkeypatch.setattr(p4cli, "run_p4_or_die", fake_or_die)
        records = p4cli.where_records([
            "C:/work/plugin/Content/Mapped.uasset",
            "C:/work/game/Content/Other.uasset",
        ])

        assert records[0]["depotFile"] == "//depot/plugin/Content/Mapped.uasset"
        assert records[1]["depotFile"] == "//depot/game/Content/Other.uasset"
        assert [args[:3] for args, _ in calls] == [
            ["-ztag", "where", "C:/work/plugin/Content/Mapped.uasset"],
            ["-ztag", "where", "C:/work/game/Content/Other.uasset"],
        ]

    def test_ambiguous_mapping_is_retained_as_multiple_records(self, monkeypatch):
        monkeypatch.setattr(
            p4cli,
            "run_p4_or_die",
            lambda *args, **kwargs: (
                "... depotFile //depot/a/File.uasset\n"
                "... clientFile //ws/a/File.uasset\n"
                "... path C:/work/File.uasset\n"
                "... depotFile //depot/b/File.uasset\n"
                "... clientFile //ws/b/File.uasset\n"
                "... path C:/work/File.uasset\n"
            ),
        )
        records = p4cli.where_records(["C:/work/File.uasset"])
        assert len(records) == 2
