from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "plugins" / "llm-scripting-kit"


def _run(name: str, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    # Scrub the launcher's own overrides out of the inherited environment. These
    # tests assert the PROFILE defaults, and the fleet exports QWEN38_HOST and
    # QWEN36_HOST globally -- so on a fleet machine the ambient value silently
    # replaced the default and the host assertion failed, while the same test
    # passed anywhere else. A test whose result depends on who ran it is worse
    # than no test; the profile is what is under test, not the environment.
    for key in [k for k in merged if k.startswith(("QWEN36_", "QWEN38_"))]:
        del merged[key]
    # Same reasoning for the swapper-cli-u3 launch-guard overrides: a fleet
    # machine (or this very test session, via LLM-scripting-kit's own
    # scripts) could already export either, which would silently defeat a
    # test asserting the launcher's DEFAULT resolution (no override present).
    for key in ("LLM_SCRIPTING_KIT_PYTHON", "LLM_SCRIPTING_KIT_CLI", "LLM_SCRIPTING_KIT_LAUNCH_GUARD"):
        merged.pop(key, None)
    if env:
        merged.update(env)
    return subprocess.run(
        ["bash", str(PLUGIN / "bin" / name), *args],
        text=True,
        capture_output=True,
        check=False,
        env=merged,
    )


def test_qwen36_help_does_not_require_runtime() -> None:
    result = _run("qwen36-server", "--help")
    assert result.returncode == 0
    assert "qwen36-server" in result.stdout


def test_qwen38_help_does_not_require_runtime() -> None:
    result = _run("qwen38-server", "--help")
    assert result.returncode == 0
    assert "qwen38-server" in result.stdout


def test_qwen38l_help_does_not_require_runtime() -> None:
    result = _run("qwen38l-server", "--help")
    assert result.returncode == 0
    assert "qwen38l-server" in result.stdout


def test_path_symlink_resolves_back_to_plugin(tmp_path: Path) -> None:
    command = tmp_path / "qwen36-server"
    command.symlink_to(PLUGIN / "bin" / "qwen36-server")
    result = subprocess.run(
        ["bash", str(command), "--help"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert "qwen36-server" in result.stdout


def test_qwen36_prints_measured_mtp_profile(tmp_path: Path) -> None:
    server = tmp_path / "ninfer-serve"
    artifact = tmp_path / "model.ninfer"
    server.write_text("#!/bin/sh\n", encoding="utf-8")
    server.chmod(0o755)
    artifact.touch()
    result = _run(
        "qwen36-server",
        "--print-command",
        env={"NINFER_SERVE": str(server), "QWEN36_ARTIFACT": str(artifact)},
    )
    assert result.returncode == 0
    assert "--max-context 262144" in result.stdout
    assert "--kv-dtype int8" in result.stdout
    assert "--spec mtp" in result.stdout
    assert "--draft-tokens 3" in result.stdout
    assert "--lm-head-draft" in result.stdout


def test_qwen38_prints_measured_nvfp4_profile(tmp_path: Path) -> None:
    server = tmp_path / "ninfer-serve"
    artifact = tmp_path / "model.ninfer"
    server.write_text("#!/bin/sh\n", encoding="utf-8")
    server.chmod(0o755)
    artifact.touch()
    result = _run(
        "qwen38-server",
        "--print-command",
        env={"NINFER_SERVE": str(server), "QWEN38_ARTIFACT": str(artifact)},
    )
    assert result.returncode == 0
    assert "--model-id qwen3.8-27b" in result.stdout
    assert "--max-context 240000" in result.stdout
    assert "--kv-capacity 240000" in result.stdout
    assert "--kv-dtype fp8" in result.stdout
    assert "--spec mtp" in result.stdout
    assert "--draft-tokens 3" in result.stdout
    assert "--lm-head-draft" in result.stdout


def test_qwen38l_prints_full_context_gpu_profile(tmp_path: Path) -> None:
    prefix = tmp_path / "llama.cpp"
    server = prefix / "bin" / "llama-server"
    model = tmp_path / "model.gguf"
    server.parent.mkdir(parents=True)
    server.write_text("#!/bin/sh\n", encoding="utf-8")
    server.chmod(0o755)
    model.touch()
    result = _run(
        "qwen38l-server",
        "--print-command",
        env={"LLAMA_CPP_PREFIX": str(prefix), "QWEN38_GGUF": str(model)},
    )
    assert result.returncode == 0
    assert "-ngl 99" in result.stdout
    assert "-c 262144" in result.stdout
    assert "--cache-type-k q8_0" in result.stdout
    assert "--host 127.0.0.1" in result.stdout


def test_qwen38_context_override_carries_kv_capacity(tmp_path: Path) -> None:
    """QWEN38_CTX moves the KV capacity with it unless capacity is set outright.

    NInfer sizes the KV pool to the context; leaving capacity pinned at its
    default while the context moved is an incoherent pair, not a smaller one.
    """
    server = tmp_path / "ninfer-serve"
    artifact = tmp_path / "model.ninfer"
    server.write_text("#!/bin/sh\n", encoding="utf-8")
    server.chmod(0o755)
    artifact.touch()
    result = _run(
        "qwen38-server",
        "--print-command",
        env={
            "NINFER_SERVE": str(server),
            "QWEN38_ARTIFACT": str(artifact),
            "QWEN38_CTX": "131072",
        },
    )
    assert result.returncode == 0
    assert "--max-context 131072" in result.stdout
    assert "--kv-capacity 131072" in result.stdout


def test_qwen38l_serial_is_refused_because_the_profile_ignores_it(tmp_path: Path) -> None:
    """--serial only ever sets QWEN36_MAX_CONCURRENCY / QWEN38_MAX_CONCURRENCY,
    which the qwen38l (llama.cpp) command array does not read at all -- it has
    no --parallel/-np flag. The old help text advertised --serial identically
    for every profile ("${profile}-server [--serial] ...") even though it did
    nothing for qwen38l. Refuse rather than silently no-op.
    """
    prefix = tmp_path / "llama.cpp"
    server = prefix / "bin" / "llama-server"
    model = tmp_path / "model.gguf"
    server.parent.mkdir(parents=True)
    server.write_text("#!/bin/sh\n", encoding="utf-8")
    server.chmod(0o755)
    model.touch()
    result = _run(
        "qwen38l-server",
        "--serial",
        "--print-command",
        env={"LLAMA_CPP_PREFIX": str(prefix), "QWEN38_GGUF": str(model)},
    )
    assert result.returncode == 2
    assert "--serial" in result.stderr


def test_qwen38l_help_does_not_advertise_serial() -> None:
    """--serial is documented in the shared show_help(), but it has no effect
    on qwen38l -- the usage line must not list it as an accepted flag for a
    profile that ignores it."""
    result = _run("qwen38l-server", "--help")
    assert result.returncode == 0
    assert "[--serial]" not in result.stdout


def test_qwen36_help_still_advertises_serial() -> None:
    result = _run("qwen36-server", "--help")
    assert result.returncode == 0
    assert "--serial" in result.stdout


def test_qwen38_help_still_advertises_serial() -> None:
    result = _run("qwen38-server", "--help")
    assert result.returncode == 0
    assert "--serial" in result.stdout


def test_qwen38l_context_is_not_driven_by_the_ninfer_override(tmp_path: Path) -> None:
    """The two Qwen3.8 backends have different context ceilings, so QWEN38_CTX
    (NInfer's, capped near 240k on a 5090) must not reconfigure llama.cpp, which
    takes the model's full 262,144. llama.cpp reads QWEN38L_CTX instead."""
    prefix = tmp_path / "llama.cpp"
    server = prefix / "bin" / "llama-server"
    model = tmp_path / "model.gguf"
    server.parent.mkdir(parents=True)
    server.write_text("#!/bin/sh\n", encoding="utf-8")
    server.chmod(0o755)
    model.touch()
    result = _run(
        "qwen38l-server",
        "--print-command",
        env={
            "LLAMA_CPP_PREFIX": str(prefix),
            "QWEN38_GGUF": str(model),
            "QWEN38_CTX": "240000",
        },
    )
    assert result.returncode == 0
    assert "-c 262144" in result.stdout

    override = _run(
        "qwen38l-server",
        "--print-command",
        env={
            "LLAMA_CPP_PREFIX": str(prefix),
            "QWEN38_GGUF": str(model),
            "QWEN38L_CTX": "131072",
        },
    )
    assert override.returncode == 0
    assert "-c 131072" in override.stdout


def _switch_env(tmp_path: Path) -> dict[str, str]:
    tools = tmp_path / "tools"
    tools.mkdir()
    pid_file = tmp_path / "server.pid"
    server = tmp_path / "ninfer-serve"
    artifact = tmp_path / "model.ninfer"
    server.write_text(
        "#!/bin/sh\n"
        f"echo $$ > '{pid_file}'\n"
        "while :; do sleep 1; done\n",
        encoding="utf-8",
    )
    server.chmod(0o755)
    artifact.touch()
    (tools / "lsof").write_text(
        "#!/bin/sh\n"
        f"if test -f '{pid_file}'; then cat '{pid_file}'; fi\n",
        encoding="utf-8",
    )
    (tools / "ps").write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        "  *comm=*) echo ninfer-serve ;;\n"
        "  *args=*) echo 'ninfer-serve model.ninfer --model-id qwen3.6-35b-a3b' ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    (tools / "curl").write_text(
        "#!/bin/sh\n"
        "echo '{\"data\":[{\"id\":\"qwen3.6-35b-a3b\"}]}'\n",
        encoding="utf-8",
    )
    for tool in tools.iterdir():
        tool.chmod(0o755)
    return {
        "PATH": f"{tools}:{os.environ['PATH']}",
        "NINFER_SERVE": str(server),
        "QWEN36_ARTIFACT": str(artifact),
        "QWEN_SWITCH_TIMEOUT": "5",
        "HOME": str(tmp_path),
    }


def test_qwen_switch_starts_existing_profile_and_waits_for_matching_model(tmp_path: Path) -> None:
    env = _switch_env(tmp_path)
    result = subprocess.run(
        ["bash", str(PLUGIN / "bin" / "qwen-switch"), "start", "qwen36"],
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, **env},
    )
    assert result.returncode == 0
    assert "qwen36 is ready" in result.stdout
    pid_file = tmp_path / "server.pid"
    assert pid_file.exists()
    if pid_file.exists():
        subprocess.run(["kill", "-KILL", pid_file.read_text().strip()], check=False)


def test_qwen_switch_status_is_clear_when_no_server_listens(tmp_path: Path) -> None:
    env = _switch_env(tmp_path)
    result = subprocess.run(
        ["bash", str(PLUGIN / "bin" / "qwen-switch"), "status"],
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, **env},
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "no managed Qwen server is listening on the configured ports"


# ===========================================================================
# swapper-cli-u3: the launch guard in scripts/model-server.sh.
#
# Every test here exercises a REAL launch (no --help, no --print-command) of
# the qwen36 profile, with NINFER_SERVE/QWEN36_ARTIFACT pointed at a stub
# "server" so no real model process ever starts. The stub server just prints
# a marker and exits immediately -- since model-server.sh's final step is
# `exec`, that marker reaching this test's captured stdout IS proof the real
# command ran (a refused launch never gets there).
#
# The guard itself is a stub Python script substituted via
# LLM_SCRIPTING_KIT_CLI (run under LLM_SCRIPTING_KIT_PYTHON, defaulted to
# this test's own interpreter) so no plugin venv, no psutil, and no real
# llama-swap are needed. Every stub also touches a marker FILE on invocation,
# so "the guard was never called" is directly observable, not inferred from
# absence of an effect.
# ===========================================================================

_REAL_SERVER_MARKER = "REAL_SERVER_RAN"


def _stub_real_server(tmp_path: Path, name: str = "server") -> dict[str, str]:
    """A stub ninfer-serve + artifact for the qwen36 profile. Prints a marker
    and exits at once -- it stands in for the process `exec` would replace
    this shell with, never for a long-running server."""
    server = tmp_path / f"{name}-ninfer-serve"
    artifact = tmp_path / f"{name}-model.ninfer"
    server.write_text(f"#!/bin/sh\necho {_REAL_SERVER_MARKER}\n", encoding="utf-8")
    server.chmod(0o755)
    artifact.touch()
    return {"NINFER_SERVE": str(server), "QWEN36_ARTIFACT": str(artifact)}


def _write_guard_stub(
    path: Path, marker: Path, exit_code: int, stderr_message: str = ""
) -> None:
    """A Python stub standing in for `llm-scripting-kit swapper guard-launch`.
    Touches `marker` unconditionally (proof the guard ran at all), optionally
    writes `stderr_message`, then exits with `exit_code` -- ignoring argv
    entirely, since these tests assert on the launcher's own branching, not
    on argument plumbing (that is test_swapper_cli.py's job)."""
    lines = [
        "import sys",
        f"open({str(marker)!r}, 'a', encoding='utf-8').close()",
    ]
    if stderr_message:
        lines.append(f"sys.stderr.write({stderr_message!r})")
    lines.append(f"sys.exit({exit_code})")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_unreachable_interpreter_stub(path: Path) -> None:
    """Stands in for LLM_SCRIPTING_KIT_PYTHON itself: an executable that
    `exec`s a path that does not exist, which bash reports as exit 127 --
    a real, observed failure shape distinct from this script's own
    pre-invocation `-x` check (which is exercised by a separate test with no
    interpreter present at all)."""
    path.write_text("#!/bin/sh\nexec /no/such/interpreter-for-swapper-cli-u3 \"$@\"\n", encoding="utf-8")
    path.chmod(0o755)


def test_allowed_launch_calls_guard_and_execs_real_command(tmp_path: Path) -> None:
    marker = tmp_path / "guard-called"
    guard = tmp_path / "guard_allow.py"
    _write_guard_stub(guard, marker, exit_code=0)
    env = {
        **_stub_real_server(tmp_path),
        "LLM_SCRIPTING_KIT_PYTHON": sys.executable,
        "LLM_SCRIPTING_KIT_CLI": str(guard),
    }
    result = _run("qwen36-server", env=env)
    assert result.returncode == 0
    assert _REAL_SERVER_MARKER in result.stdout
    assert marker.exists(), "guard-launch (exit 0) must still be called on a real launch"


def test_refusing_guard_prevents_exec(tmp_path: Path) -> None:
    """LaunchRefused is exit 3 (swapper.py), not 1 -- see
    test_guard_exit_one_proceeds_with_warning for why 1 must NOT refuse."""
    marker = tmp_path / "guard-called"
    guard = tmp_path / "guard_refuse.py"
    _write_guard_stub(guard, marker, exit_code=3, stderr_message="llama-swap is running for this user")
    env = {
        **_stub_real_server(tmp_path),
        "LLM_SCRIPTING_KIT_PYTHON": sys.executable,
        "LLM_SCRIPTING_KIT_CLI": str(guard),
    }
    result = _run("qwen36-server", env=env)
    assert result.returncode == 1
    assert _REAL_SERVER_MARKER not in result.stdout, "a refused launch must never reach exec"
    assert marker.exists()
    assert "launch refused" in result.stderr
    assert "llama-swap is running for this user" in result.stderr


def test_guard_exit_one_proceeds_with_warning(tmp_path: Path) -> None:
    """Exit 1 -- what an UNCAUGHT Python exception also produces -- must
    fail OPEN, not refuse. This is the whole reason LaunchRefused was moved
    off exit 1 onto exit 3 (orchestrator decision, swapper-cli-u3): if this
    test and test_refusing_guard_prevents_exec both passed with the launcher
    refusing on exit 1, a real uncaught bug in the guard's call graph would
    silently block every launch."""
    marker = tmp_path / "guard-called"
    guard = tmp_path / "guard_uncaught.py"
    _write_guard_stub(guard, marker, exit_code=1, stderr_message="Traceback (most recent call last): boom")
    env = {
        **_stub_real_server(tmp_path),
        "LLM_SCRIPTING_KIT_PYTHON": sys.executable,
        "LLM_SCRIPTING_KIT_CLI": str(guard),
    }
    result = _run("qwen36-server", env=env)
    assert result.returncode == 0
    assert _REAL_SERVER_MARKER in result.stdout
    assert marker.exists()
    warning_lines = [
        line for line in result.stderr.splitlines() if line.startswith("model-server.sh:")
    ]
    assert len(warning_lines) == 1, result.stderr
    assert "exit 1" in warning_lines[0]
    assert "proceeding without it" in warning_lines[0]


def test_guard_indeterminate_exit_five_proceeds_with_one_warning_line(tmp_path: Path) -> None:
    """InspectionIndeterminate (exit 5) is the code a missing psutil produces
    (PsutilInspector converts that ImportError itself -- see swapper.py /
    _swapper_process.py) -- it must fail OPEN, not refuse."""
    marker = tmp_path / "guard-called"
    guard = tmp_path / "guard_indeterminate.py"
    _write_guard_stub(guard, marker, exit_code=5)
    env = {
        **_stub_real_server(tmp_path),
        "LLM_SCRIPTING_KIT_PYTHON": sys.executable,
        "LLM_SCRIPTING_KIT_CLI": str(guard),
    }
    result = _run("qwen36-server", env=env)
    assert result.returncode == 0
    assert _REAL_SERVER_MARKER in result.stdout
    assert marker.exists()
    warning_lines = [
        line for line in result.stderr.splitlines() if line.startswith("model-server.sh:")
    ]
    assert len(warning_lines) == 1, result.stderr
    assert "exit 5" in warning_lines[0]
    assert "proceeding without it" in warning_lines[0]


def test_guard_missing_cli_script_proceeds_with_warning(tmp_path: Path) -> None:
    """Python's own "can't open file" (exit 2) is what a missing/uninstalled
    CLI script looks like from here -- must fail OPEN."""
    env = {
        **_stub_real_server(tmp_path),
        "LLM_SCRIPTING_KIT_PYTHON": sys.executable,
        "LLM_SCRIPTING_KIT_CLI": str(tmp_path / "does-not-exist.py"),
    }
    result = _run("qwen36-server", env=env)
    assert result.returncode == 0
    assert _REAL_SERVER_MARKER in result.stdout
    assert "proceeding without it" in result.stderr


def test_guard_exec_failure_127_proceeds_with_warning(tmp_path: Path) -> None:
    """The interpreter itself resolves but cannot run anything (exit 127) --
    must fail OPEN, matching the decision's explicit `exit 127` example."""
    python_stub = tmp_path / "unreachable-python"
    _write_unreachable_interpreter_stub(python_stub)
    env = {
        **_stub_real_server(tmp_path),
        "LLM_SCRIPTING_KIT_PYTHON": str(python_stub),
        "LLM_SCRIPTING_KIT_CLI": str(tmp_path / "irrelevant.py"),
    }
    result = _run("qwen36-server", env=env)
    assert result.returncode == 0
    assert _REAL_SERVER_MARKER in result.stdout
    assert "exit 127" in result.stderr
    assert "proceeding without it" in result.stderr


def test_guard_skipped_with_warning_when_no_interpreter_is_configured(tmp_path: Path) -> None:
    """No plugin venv, no LLM_SCRIPTING_KIT_PYTHON override: the guard must
    never even be attempted, and the launch must still proceed. HOME is
    pointed at a fresh tmp_path so this does not depend on whether THIS
    machine happens to have llm-scripting-kit's own venv provisioned."""
    env = {
        **_stub_real_server(tmp_path),
        "HOME": str(tmp_path),
    }
    result = _run("qwen36-server", env=env)
    assert result.returncode == 0
    assert _REAL_SERVER_MARKER in result.stdout
    assert "launch guard skipped" in result.stderr
    assert "proceeding without it" in result.stderr


def test_help_and_print_command_never_invoke_the_guard(tmp_path: Path) -> None:
    marker = tmp_path / "guard-called"
    guard = tmp_path / "guard_allow.py"
    _write_guard_stub(guard, marker, exit_code=0)
    env = {
        **_stub_real_server(tmp_path),
        "LLM_SCRIPTING_KIT_PYTHON": sys.executable,
        "LLM_SCRIPTING_KIT_CLI": str(guard),
    }

    help_result = _run("qwen36-server", "--help", env=env)
    assert help_result.returncode == 0
    assert not marker.exists()

    print_result = _run("qwen36-server", "--print-command", env=env)
    assert print_result.returncode == 0
    assert "ninfer-serve" in print_result.stdout
    assert not marker.exists()


def test_launch_guard_disabled_skips_the_guard_entirely(tmp_path: Path) -> None:
    """LLM_SCRIPTING_KIT_LAUNCH_GUARD=off is the config seam for a team that
    wants a manual launch beside an active swapper on purpose (plugin-opinion
    razor, swapper-cli-u4 review). off must skip the guard with no
    interpreter call and no warning -- even a REFUSING guard must never run."""
    marker = tmp_path / "guard-called"
    guard = tmp_path / "guard_refuse.py"
    _write_guard_stub(guard, marker, exit_code=3, stderr_message="llama-swap is running for this user")
    env = {
        **_stub_real_server(tmp_path),
        "LLM_SCRIPTING_KIT_PYTHON": sys.executable,
        "LLM_SCRIPTING_KIT_CLI": str(guard),
        "LLM_SCRIPTING_KIT_LAUNCH_GUARD": "off",
    }
    result = _run("qwen36-server", env=env)
    assert result.returncode == 0
    assert _REAL_SERVER_MARKER in result.stdout
    assert not marker.exists(), "the stub-refusing guard must never be invoked when the switch is off"
    warning_lines = [
        line for line in result.stderr.splitlines() if line.startswith("model-server.sh:")
    ]
    assert not warning_lines, result.stderr


def test_removing_the_guard_call_lets_a_refusal_through(tmp_path: Path) -> None:
    """Revert proof for test_refusing_guard_prevents_exec: strip the
    `guard_launch "$$"` call out of a copy of the real script and show the
    same refusing stub no longer stops exec. This is what demonstrates the
    check in the shipped file is load-bearing, not merely present."""
    real_text = (PLUGIN / "scripts" / "model-server.sh").read_text(encoding="utf-8")
    assert 'guard_launch "$$"' in real_text, "the call site moved; update this test's replace target"
    no_guard_text = real_text.replace('guard_launch "$$"\n', "")
    assert no_guard_text != real_text

    no_guard_script = tmp_path / "model-server-no-guard.sh"
    no_guard_script.write_text(no_guard_text, encoding="utf-8")
    no_guard_script.chmod(0o755)

    marker = tmp_path / "guard-called"
    guard = tmp_path / "guard_refuse.py"
    _write_guard_stub(guard, marker, exit_code=3, stderr_message="would have refused")
    env = os.environ.copy()
    for key in [k for k in env if k.startswith(("QWEN36_", "QWEN38_"))]:
        del env[key]
    env.update(_stub_real_server(tmp_path))
    env["LLM_SCRIPTING_KIT_PYTHON"] = sys.executable
    env["LLM_SCRIPTING_KIT_CLI"] = str(guard)

    result = subprocess.run(
        ["bash", str(no_guard_script), "qwen36"],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )
    assert result.returncode == 0
    assert _REAL_SERVER_MARKER in result.stdout, (
        "without the guard_launch call, exec must proceed even though the "
        "stub would have refused -- if this fails, the real file's check "
        "is not what is preventing exec in test_refusing_guard_prevents_exec"
    )
