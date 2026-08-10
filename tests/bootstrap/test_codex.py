"""Tests for bootstrap_lib/codex.py -- Codex CLI detection + command building.

Hermetic: the real ``codex`` binary is never invoked. ``shutil.which`` and
``subprocess.run`` are monkeypatched in the module's own namespace, mirroring
tests/awesome-kit/test_orchestration_guidance.py::TestDetectBackend.

The argv tests assert the COMPLETE list rather than spot-checking flags, in
the style of tests/llm-scripting-kit/test_completion_backends.py
::test_happy_path_cmd_and_response -- the ordering and the presence of the
Windows sandbox key are the load-bearing parts, and a spot check would not
catch a reordering that moves stdin `-` off the end.
"""

import subprocess

import pytest

from bootstrap_lib import codex
from bootstrap_lib.codex import CodexDetection


@pytest.fixture(autouse=True)
def _clear_cache():
    codex.reset_detection_cache()
    yield
    codex.reset_detection_cache()


class _Proc:
    """A finished subprocess, as detect_codex reads it (bytes stdout)."""

    def __init__(self, returncode=0, stdout=b""):
        self.returncode = returncode
        self.stdout = stdout


def _fake_run(proc_or_exc, recorder=None):
    def run(argv, **kwargs):
        if recorder is not None:
            recorder.append({"argv": list(argv), "kwargs": kwargs})
        if isinstance(proc_or_exc, BaseException):
            raise proc_or_exc
        return proc_or_exc

    return run


# --------------------------------------------------------------------------
# resolve_cli
# --------------------------------------------------------------------------


class TestResolveCli:
    def test_absent_returns_none(self, monkeypatch):
        monkeypatch.setattr(codex.shutil, "which", lambda name: None)
        assert codex.resolve_cli("codex") is None

    def test_plain_executable_is_a_one_tuple(self, monkeypatch):
        monkeypatch.setattr(codex.shutil, "which", lambda name: "/usr/bin/codex")
        assert codex.resolve_cli("codex") == ("/usr/bin/codex",)

    def test_windows_cmd_shim_is_wrapped_in_cmd_c(self, monkeypatch):
        """npm/scoop install `codex.cmd`; CreateProcess cannot exec a batch file."""
        monkeypatch.setattr(codex.os, "name", "nt")
        monkeypatch.setattr(
            codex.shutil, "which", lambda name: "C:\\tools\\codex.CMD"
        )
        assert codex.resolve_cli("codex") == ("cmd", "/c", "C:\\tools\\codex.CMD")

    def test_windows_bat_shim_is_wrapped_too(self, monkeypatch):
        monkeypatch.setattr(codex.os, "name", "nt")
        monkeypatch.setattr(codex.shutil, "which", lambda name: "C:\\t\\codex.bat")
        assert codex.resolve_cli("codex") == ("cmd", "/c", "C:\\t\\codex.bat")

    def test_windows_exe_is_not_wrapped(self, monkeypatch):
        monkeypatch.setattr(codex.os, "name", "nt")
        monkeypatch.setattr(codex.shutil, "which", lambda name: "C:\\t\\codex.exe")
        assert codex.resolve_cli("codex") == ("C:\\t\\codex.exe",)

    def test_which_is_asked_for_the_bare_name(self, monkeypatch):
        seen = {}

        def fake_which(name):
            seen["name"] = name
            return "/usr/bin/codex"

        monkeypatch.setattr(codex.shutil, "which", fake_which)
        codex.resolve_cli(codex.CODEX_EXECUTABLE)
        assert seen["name"] == "codex"


# --------------------------------------------------------------------------
# parse_codex_version
# --------------------------------------------------------------------------


class TestParseCodexVersion:
    def test_real_banner(self):
        assert codex.parse_codex_version("codex-cli 0.146.0") == (0, 146, 0)

    def test_bare_codex_prefix(self):
        assert codex.parse_codex_version("codex 1.2.3") == (1, 2, 3)

    def test_no_prefix(self):
        assert codex.parse_codex_version("0.146.0") == (0, 146, 0)

    def test_v_prefix(self):
        assert codex.parse_codex_version("codex-cli v0.146.0") == (0, 146, 0)

    def test_prerelease_suffix(self):
        assert codex.parse_codex_version("codex-cli 0.147.0-rc.2") == (0, 147, 0)

    def test_trailing_text(self):
        assert codex.parse_codex_version("codex-cli 0.146.0 (abcdef)") == (0, 146, 0)

    def test_extra_lines_ignored(self):
        assert codex.parse_codex_version("codex-cli 0.146.0\nblah\n") == (0, 146, 0)

    def test_junk_is_none(self):
        assert codex.parse_codex_version("not a version at all") is None

    def test_partial_version_is_none(self):
        assert codex.parse_codex_version("codex-cli 0.146") is None

    def test_empty_is_none(self):
        assert codex.parse_codex_version("") is None
        assert codex.parse_codex_version("   \n  ") is None


# --------------------------------------------------------------------------
# detect_codex
# --------------------------------------------------------------------------


class TestDetectCodex:
    def test_success(self, monkeypatch):
        calls = []
        monkeypatch.setattr(codex.shutil, "which", lambda name: "/usr/bin/codex")
        monkeypatch.setattr(
            codex.subprocess,
            "run",
            _fake_run(_Proc(0, b"codex-cli 0.146.0\n"), calls),
        )
        result = codex.detect_codex()
        assert isinstance(result, CodexDetection)
        assert result.available is True
        assert result.reason == "codex-cli 0.146.0"
        assert result.version == (0, 146, 0)
        assert result.argv_prefix == ("/usr/bin/codex",)
        assert calls[0]["argv"] == ["/usr/bin/codex", "--version"]
        assert calls[0]["kwargs"]["stdout"] is subprocess.PIPE
        assert calls[0]["kwargs"]["stderr"] is subprocess.STDOUT
        assert calls[0]["kwargs"]["timeout"] == 10.0

    def test_availability_is_not_coupled_to_parseability(self, monkeypatch):
        monkeypatch.setattr(codex.shutil, "which", lambda name: "/usr/bin/codex")
        monkeypatch.setattr(
            codex.subprocess, "run", _fake_run(_Proc(0, b"codex build 2026-08\n"))
        )
        result = codex.detect_codex()
        assert result.available is True
        assert result.version is None
        assert result.reason == "codex build 2026-08"

    def test_not_on_path(self, monkeypatch):
        monkeypatch.setattr(codex.shutil, "which", lambda name: None)
        result = codex.detect_codex()
        assert result.available is False
        assert "not found on PATH" in result.reason
        assert result.argv_prefix is None
        assert result.version is None

    def test_nonzero_exit(self, monkeypatch):
        monkeypatch.setattr(codex.shutil, "which", lambda name: "/usr/bin/codex")
        monkeypatch.setattr(codex.subprocess, "run", _fake_run(_Proc(127, b"")))
        result = codex.detect_codex()
        assert result.available is False
        assert "exited 127" in result.reason

    def test_oserror_fails_closed(self, monkeypatch):
        monkeypatch.setattr(codex.shutil, "which", lambda name: "/usr/bin/codex")
        monkeypatch.setattr(
            codex.subprocess, "run", _fake_run(OSError("WinError 2"))
        )
        result = codex.detect_codex()
        assert result.available is False
        assert "did not run (OSError)" in result.reason

    def test_timeout_fails_closed_with_its_own_reason(self, monkeypatch):
        monkeypatch.setattr(codex.shutil, "which", lambda name: "/usr/bin/codex")
        monkeypatch.setattr(
            codex.subprocess,
            "run",
            _fake_run(subprocess.TimeoutExpired(cmd="codex", timeout=10.0)),
        )
        result = codex.detect_codex()
        assert result.available is False
        assert "timed out" in result.reason

    def test_subprocess_error_fails_closed(self, monkeypatch):
        monkeypatch.setattr(codex.shutil, "which", lambda name: "/usr/bin/codex")
        monkeypatch.setattr(
            codex.subprocess, "run", _fake_run(subprocess.SubprocessError("boom"))
        )
        result = codex.detect_codex()
        assert result.available is False
        assert "SubprocessError" in result.reason

    def test_each_failure_reason_is_distinct(self, monkeypatch):
        reasons = set()
        monkeypatch.setattr(codex.shutil, "which", lambda name: None)
        reasons.add(codex.detect_codex().reason)
        for outcome in (
            _Proc(127, b""),
            OSError("nope"),
            subprocess.TimeoutExpired(cmd="codex", timeout=10.0),
        ):
            codex.reset_detection_cache()
            monkeypatch.setattr(codex.shutil, "which", lambda name: "/usr/bin/codex")
            monkeypatch.setattr(codex.subprocess, "run", _fake_run(outcome))
            reasons.add(codex.detect_codex().reason)
        assert len(reasons) == 4

    def test_result_is_cached_for_the_process(self, monkeypatch):
        calls = []
        monkeypatch.setattr(codex.shutil, "which", lambda name: "/usr/bin/codex")
        monkeypatch.setattr(
            codex.subprocess, "run", _fake_run(_Proc(0, b"codex-cli 0.146.0\n"), calls)
        )
        first = codex.detect_codex()
        second = codex.detect_codex()
        assert second is first
        assert len(calls) == 1

    def test_reset_clears_the_cache(self, monkeypatch):
        calls = []
        monkeypatch.setattr(codex.shutil, "which", lambda name: "/usr/bin/codex")
        monkeypatch.setattr(
            codex.subprocess, "run", _fake_run(_Proc(0, b"codex-cli 0.146.0\n"), calls)
        )
        codex.detect_codex()
        codex.reset_detection_cache()
        codex.detect_codex()
        assert len(calls) == 2


# --------------------------------------------------------------------------
# build_codex_exec_argv
# --------------------------------------------------------------------------


class TestBuildCodexExecArgv:
    def test_full_default_argv_posix(self, monkeypatch, tmp_path):
        monkeypatch.setattr(codex.os, "name", "posix")
        root = tmp_path / "proj"
        root.mkdir()
        assert codex.build_codex_exec_argv(
            root=root, argv_prefix=("codex",)
        ) == [
            "codex",
            "exec",
            "-s",
            "workspace-write",
            "-c",
            "sandbox_workspace_write.network_access=true",
            "-C",
            str(root),
            "--skip-git-repo-check",
            "--color",
            "never",
            "-",
        ]

    def test_full_default_argv_windows(self, monkeypatch, tmp_path):
        monkeypatch.setattr(codex.os, "name", "nt")
        root = tmp_path / "proj"
        root.mkdir()
        assert codex.build_codex_exec_argv(
            root=root, argv_prefix=("cmd", "/c", "C:\\t\\codex.cmd")
        ) == [
            "cmd",
            "/c",
            "C:\\t\\codex.cmd",
            "exec",
            "-s",
            "workspace-write",
            "-c",
            'windows.sandbox="unelevated"',
            "-c",
            "sandbox_workspace_write.network_access=true",
            "-C",
            str(root),
            "--skip-git-repo-check",
            "--color",
            "never",
            "-",
        ]

    def test_everything_set_posix(self, monkeypatch, tmp_path):
        monkeypatch.setattr(codex.os, "name", "posix")
        root = tmp_path / "proj"
        extra = tmp_path / "lib"
        scratch = tmp_path / "scratch"
        out = tmp_path / "reply.txt"
        schema = tmp_path / "schema.json"
        assert codex.build_codex_exec_argv(
            root=root,
            scratch_dir=scratch,
            model="gpt-5.6-codex",
            effort="high",
            output_file=out,
            add_dirs=[extra],
            sandbox="read-only",
            network=False,
            output_schema=schema,
            json_events=True,
            extra_config=["tools.web_search=true"],
            argv_prefix=("codex",),
        ) == [
            "codex",
            "exec",
            "-s",
            "read-only",
            "-m",
            "gpt-5.6-codex",
            "-c",
            "model_reasoning_effort=high",
            "-c",
            "tools.web_search=true",
            "-C",
            str(root),
            "--add-dir",
            str(extra),
            "--add-dir",
            str(scratch),
            "-o",
            str(out),
            "--output-schema",
            str(schema),
            "--json",
            "--skip-git-repo-check",
            "--color",
            "never",
            "-",
        ]

    def test_stdin_dash_is_always_last(self, monkeypatch, tmp_path):
        monkeypatch.setattr(codex.os, "name", "nt")
        argv = codex.build_codex_exec_argv(
            root=tmp_path,
            scratch_dir=tmp_path / "s",
            output_file=tmp_path / "o.txt",
            json_events=True,
            argv_prefix=("codex",),
        )
        assert argv[-1] == "-"
        assert argv.count("-") == 1

    def test_network_flag_omitted_when_false(self, monkeypatch, tmp_path):
        monkeypatch.setattr(codex.os, "name", "posix")
        argv = codex.build_codex_exec_argv(
            root=tmp_path, network=False, argv_prefix=("codex",)
        )
        assert "sandbox_workspace_write.network_access=true" not in argv

    def test_windows_sandbox_key_present_only_on_nt(self, monkeypatch, tmp_path):
        monkeypatch.setattr(codex.os, "name", "nt")
        assert 'windows.sandbox="unelevated"' in codex.build_codex_exec_argv(
            root=tmp_path, argv_prefix=("codex",)
        )
        monkeypatch.setattr(codex.os, "name", "posix")
        assert 'windows.sandbox="unelevated"' not in codex.build_codex_exec_argv(
            root=tmp_path, argv_prefix=("codex",)
        )

    def test_scratch_dir_lands_in_add_dirs(self, tmp_path):
        scratch = tmp_path / "scratch"
        argv = codex.build_codex_exec_argv(
            root=tmp_path, scratch_dir=scratch, argv_prefix=("codex",)
        )
        assert argv[argv.index("--add-dir") + 1] == str(scratch)

    def test_relative_root_raises_naming_the_param(self, tmp_path):
        with pytest.raises(ValueError) as excinfo:
            codex.build_codex_exec_argv(root="proj", argv_prefix=("codex",))
        assert "root" in str(excinfo.value)
        assert "writable-root" in str(excinfo.value)

    def test_relative_add_dir_raises_naming_the_param(self, tmp_path):
        with pytest.raises(ValueError) as excinfo:
            codex.build_codex_exec_argv(
                root=tmp_path, add_dirs=["rel/dir"], argv_prefix=("codex",)
            )
        assert "add_dirs[0]" in str(excinfo.value)

    def test_relative_scratch_dir_raises_naming_the_param(self, tmp_path):
        with pytest.raises(ValueError) as excinfo:
            codex.build_codex_exec_argv(
                root=tmp_path, scratch_dir="scratch", argv_prefix=("codex",)
            )
        assert "scratch_dir" in str(excinfo.value)

    def test_relative_output_file_raises_naming_the_param(self, tmp_path):
        with pytest.raises(ValueError) as excinfo:
            codex.build_codex_exec_argv(
                root=tmp_path, output_file="reply.txt", argv_prefix=("codex",)
            )
        assert "output_file" in str(excinfo.value)

    def test_relative_output_schema_raises_naming_the_param(self, tmp_path):
        with pytest.raises(ValueError) as excinfo:
            codex.build_codex_exec_argv(
                root=tmp_path, output_schema="schema.json", argv_prefix=("codex",)
            )
        assert "output_schema" in str(excinfo.value)

    def test_cmd_wrapped_argv_rejects_metacharacters(self, tmp_path):
        """cmd.exe re-parses these; subprocess does not quote them.

        VERIFIED: an argv element `hello&echo>F.txt` passed through a
        `cmd /c` prefix created that file, because list2cmdline quotes only
        for spaces per the MSVC convention. Refusing beats executing.
        """
        for bad in ("a&b", "a|b", "a<b", "a>b", "a^b"):
            with pytest.raises(ValueError) as excinfo:
                codex.build_codex_exec_argv(
                    root=tmp_path,
                    model=bad,
                    argv_prefix=("cmd", "/c", "C:/tools/codex.cmd"),
                )
            assert "cmd.exe" in str(excinfo.value)
            assert bad in str(excinfo.value)

    def test_real_executable_prefix_allows_metacharacters(self, tmp_path):
        """No cmd hop, no re-parse -- CreateProcess takes argv verbatim."""
        argv = codex.build_codex_exec_argv(
            root=tmp_path, model="a&b", argv_prefix=("C:/tools/codex.exe",)
        )
        assert "a&b" in argv

    def test_prefix_resolved_from_path_when_omitted(self, monkeypatch, tmp_path):
        monkeypatch.setattr(codex.os, "name", "posix")
        monkeypatch.setattr(codex.shutil, "which", lambda name: "/usr/bin/codex")
        argv = codex.build_codex_exec_argv(root=tmp_path)
        assert argv[0] == "/usr/bin/codex"

    def test_unresolvable_cli_raises_runtimeerror(self, monkeypatch, tmp_path):
        monkeypatch.setattr(codex.shutil, "which", lambda name: None)
        with pytest.raises(RuntimeError) as excinfo:
            codex.build_codex_exec_argv(root=tmp_path)
        assert "not on PATH" in str(excinfo.value)

    def test_pathlike_and_str_both_accepted(self, tmp_path):
        argv = codex.build_codex_exec_argv(
            root=str(tmp_path), add_dirs=(tmp_path,), argv_prefix=("codex",)
        )
        assert all(isinstance(part, str) for part in argv)


# --------------------------------------------------------------------------
# probe_config_key
# --------------------------------------------------------------------------


class _TextProc:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


class TestProbeConfigKey:
    def test_known_key_reaches_the_empty_prompt_error(self, monkeypatch):
        calls = []
        monkeypatch.setattr(codex.shutil, "which", lambda name: "/usr/bin/codex")
        monkeypatch.setattr(
            codex.subprocess,
            "run",
            _fake_run(_TextProc(1, "ERROR: No prompt provided via stdin"), calls),
        )
        assert codex.probe_config_key("model_reasoning_effort") is True
        argv = calls[0]["argv"]
        assert "--strict-config" in argv
        assert argv[argv.index("-c") + 1] == "model_reasoning_effort=low"
        assert argv[-1] == "-"
        assert calls[0]["kwargs"]["input"] == ""

    def test_unknown_key_is_rejected(self, monkeypatch):
        monkeypatch.setattr(codex.shutil, "which", lambda name: "/usr/bin/codex")
        monkeypatch.setattr(
            codex.subprocess,
            "run",
            _fake_run(_TextProc(1, "error: unknown configuration field `nope`")),
        )
        assert codex.probe_config_key("nope") is False

    def test_boolean_key_gets_a_boolean_probe_value(self, monkeypatch):
        calls = []
        monkeypatch.setattr(codex.shutil, "which", lambda name: "/usr/bin/codex")
        monkeypatch.setattr(
            codex.subprocess,
            "run",
            _fake_run(_TextProc(1, "No prompt provided"), calls),
        )
        assert codex.probe_config_key("sandbox_workspace_write.network_access") is True
        argv = calls[0]["argv"]
        assert (
            argv[argv.index("-c") + 1]
            == "sandbox_workspace_write.network_access=true"
        )

    def test_timeout_fails_closed(self, monkeypatch):
        monkeypatch.setattr(codex.shutil, "which", lambda name: "/usr/bin/codex")
        monkeypatch.setattr(
            codex.subprocess,
            "run",
            _fake_run(subprocess.TimeoutExpired(cmd="codex", timeout=60.0)),
        )
        assert codex.probe_config_key("model_reasoning_effort") is False

    def test_missing_cli_fails_closed(self, monkeypatch):
        monkeypatch.setattr(codex.shutil, "which", lambda name: None)
        assert codex.probe_config_key("model_reasoning_effort") is False

    def test_unexpected_nonzero_outcome_fails_closed(self, monkeypatch):
        monkeypatch.setattr(codex.shutil, "which", lambda name: "/usr/bin/codex")
        monkeypatch.setattr(
            codex.subprocess, "run", _fake_run(_TextProc(3, "something else"))
        )
        assert codex.probe_config_key("model_reasoning_effort") is False

    def test_clean_exit_counts_as_accepted(self, monkeypatch):
        monkeypatch.setattr(codex.shutil, "which", lambda name: "/usr/bin/codex")
        monkeypatch.setattr(codex.subprocess, "run", _fake_run(_TextProc(0, "")))
        assert codex.probe_config_key("model_reasoning_effort") is True
