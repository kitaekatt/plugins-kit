"""Commandlet result correlation tests.

These tests execute the generated invocation wrapper at the fake process
boundary.  They keep the tests independent of an installed Unreal Editor
while exercising the same wrapper and result handling used by the runner.
"""

import json
import runpy
import subprocess
import sys
import traceback
from pathlib import Path

import pytest

_SKILL_DIR = (
    Path(__file__).resolve().parent.parent.parent
    / "plugins"
    / "unreal-kit"
    / "skills"
    / "ue-python-api"
)
_PLUGIN_DIR = _SKILL_DIR.parent.parent
_SCRIPTS_DIR = _SKILL_DIR / "scripts"
_LIB_DIR = _PLUGIN_DIR / "lib"
for p in (_SCRIPTS_DIR, _LIB_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from ue_runner import _run_commandlet


def _config(tmp_path):
    project = tmp_path / "Project" / "Test.uproject"
    project.parent.mkdir(parents=True)
    project.write_text("{}")
    return type(
        "Config",
        (),
        {
            "editor_cmd_exe": str(tmp_path / "UnrealEditor-Cmd.exe"),
            "uproject": str(project),
        },
    )()


def _fake_process_boundary(monkeypatch, *, returncode=0, stderr="", mutate=None):
    def fake_run(command, **kwargs):
        wrapper = next(arg.removeprefix("-script=") for arg in command if arg.startswith("-script="))
        runpy.run_path(wrapper, run_name="__main__")
        if mutate:
            mutate()
        return subprocess.CompletedProcess(command, returncode, "", stderr)

    monkeypatch.setattr("ue_runner.subprocess.run", fake_run)


def test_nonzero_shutdown_after_normal_completion_is_success(tmp_path, monkeypatch):
    script = tmp_path / "script.py"
    marker = tmp_path / "ran"
    script.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('yes')\n")
    _fake_process_boundary(monkeypatch, returncode=17)
    monkeypatch.setattr("ue_runner._get_output_dir", lambda config: tmp_path / "output")

    result = _run_commandlet(str(script), _config(tmp_path))

    assert result.success is True
    assert result.error == ""


def test_nonzero_shutdown_after_partial_output_is_failure(tmp_path, monkeypatch):
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    script = tmp_path / "script.py"
    script.write_text(
        f"from pathlib import Path\n"
        f"Path({str(output_dir / 'partial.yaml')!r}).write_text('partial')\n"
        "raise RuntimeError('after partial output')\n"
    )

    def fake_run(command, **kwargs):
        wrapper = next(arg.removeprefix("-script=") for arg in command if arg.startswith("-script="))
        try:
            runpy.run_path(wrapper, run_name="__main__")
        except BaseException:
            return subprocess.CompletedProcess(command, 1, "", traceback.format_exc())
        return subprocess.CompletedProcess(command, 1, "", "")

    monkeypatch.setattr("ue_runner.subprocess.run", fake_run)
    monkeypatch.setattr("ue_runner._get_output_dir", lambda config: output_dir)

    result = _run_commandlet(str(script), _config(tmp_path))

    assert result.success is False
    assert result.output_file is None


def test_unrelated_yaml_change_without_wrapper_completion_is_failure(tmp_path, monkeypatch):
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    script = tmp_path / "script.py"
    script.write_text("pass\n")

    def fake_run(command, **kwargs):
        (output_dir / "unrelated.yaml").write_text("from another process")
        return subprocess.CompletedProcess(command, 1, "", "")

    monkeypatch.setattr("ue_runner.subprocess.run", fake_run)
    monkeypatch.setattr("ue_runner._get_output_dir", lambda config: output_dir)

    result = _run_commandlet(str(script), _config(tmp_path))

    assert result.success is False
    assert result.output_file is None


def test_wrapper_preserves_file_argv_and_traceback_attribution(tmp_path, monkeypatch):
    context = tmp_path / "context.json"
    script = tmp_path / "script.py"
    script.write_text(
        "import json, sys\n"
        f"json.dump({{'file': __file__, 'argv': sys.argv, 'name': __name__}}, open({str(context)!r}, 'w'))\n"
    )
    wrapper_paths = []

    def fake_run(command, **kwargs):
        wrapper = Path(next(arg.removeprefix("-script=") for arg in command if arg.startswith("-script=")))
        wrapper_paths.append(wrapper)
        runpy.run_path(wrapper, run_name="__main__")
        return subprocess.CompletedProcess(command, 9, "", "")

    monkeypatch.setattr("ue_runner.subprocess.run", fake_run)
    monkeypatch.setattr("ue_runner._get_output_dir", lambda config: tmp_path / "output")

    result = _run_commandlet(str(script), _config(tmp_path))

    assert result.success is True
    data = json.loads(context.read_text())
    assert Path(data["file"]).resolve() == script.resolve()
    assert data["argv"] == [str(script)]
    assert data["name"] == "__main__"
    assert wrapper_paths[0].parent.parent.parent.parent == tmp_path / "Project" / ".local-data"
    assert not wrapper_paths[0].exists()


def test_wrapper_preserves_traceback_attribution(tmp_path, monkeypatch):
    script = tmp_path / "script.py"
    script.write_text("raise RuntimeError('boom')\n")

    def fake_run(command, **kwargs):
        wrapper = next(arg.removeprefix("-script=") for arg in command if arg.startswith("-script="))
        try:
            runpy.run_path(wrapper, run_name="__main__")
        except BaseException:
            return subprocess.CompletedProcess(command, 1, "", traceback.format_exc())
        return subprocess.CompletedProcess(command, 1, "", "")

    monkeypatch.setattr("ue_runner.subprocess.run", fake_run)
    monkeypatch.setattr("ue_runner._get_output_dir", lambda config: tmp_path / "output")

    result = _run_commandlet(str(script), _config(tmp_path))

    assert result.success is False
    assert str(script) in result.stderr


def test_zero_system_exit_is_completion(tmp_path, monkeypatch):
    script = tmp_path / "script.py"
    script.write_text("raise SystemExit(0)\n")
    _fake_process_boundary(monkeypatch, returncode=19)
    monkeypatch.setattr("ue_runner._get_output_dir", lambda config: tmp_path / "output")

    result = _run_commandlet(str(script), _config(tmp_path))

    assert result.success is True


@pytest.mark.parametrize(
    "source,returncode,stderr",
    [
        ("raise RuntimeError('boom')\n", 1, ""),
        ("raise SystemExit(3)\n", 3, ""),
    ],
)
def test_exception_or_nonzero_system_exit_never_confirms_completion(
    tmp_path, monkeypatch, source, returncode, stderr
):
    script = tmp_path / "script.py"
    script.write_text(source)

    def fake_run(command, **kwargs):
        wrapper = next(arg.removeprefix("-script=") for arg in command if arg.startswith("-script="))
        try:
            runpy.run_path(wrapper, run_name="__main__")
        except BaseException as exc:
            return subprocess.CompletedProcess(command, returncode, "", stderr or str(exc))
        return subprocess.CompletedProcess(command, returncode, "", stderr)

    monkeypatch.setattr("ue_runner.subprocess.run", fake_run)
    monkeypatch.setattr("ue_runner._get_output_dir", lambda config: tmp_path / "output")

    result = _run_commandlet(str(script), _config(tmp_path))

    assert result.success is False


def test_stale_or_wrong_completion_token_does_not_confirm(tmp_path, monkeypatch):
    script = tmp_path / "script.py"
    script.write_text("pass\n")

    def fake_run(command, **kwargs):
        wrapper = next(arg.removeprefix("-script=") for arg in command if arg.startswith("-script="))
        runpy.run_path(wrapper, run_name="__main__")
        record = next(Path(wrapper).parent.glob("*.json"))
        data = json.loads(record.read_text())
        data["token"] = "wrong-token"
        record.write_text(json.dumps(data))
        return subprocess.CompletedProcess(command, 7, "", "")

    monkeypatch.setattr("ue_runner.subprocess.run", fake_run)
    monkeypatch.setattr("ue_runner._get_output_dir", lambda config: tmp_path / "output")

    result = _run_commandlet(str(script), _config(tmp_path))

    assert result.success is False


def test_commandlet_timeout_is_forwarded_and_retains_partial_evidence(tmp_path, monkeypatch):
    script = tmp_path / "script.py"
    script.write_text("pass\n")
    calls = []

    def fake_run(command, **kwargs):
        calls.append(kwargs)
        raise subprocess.TimeoutExpired(
            command, kwargs["timeout"], output="partial stdout", stderr="partial stderr"
        )

    monkeypatch.setattr("ue_runner.subprocess.run", fake_run)
    monkeypatch.setattr("ue_runner._get_output_dir", lambda config: tmp_path / "output")

    result = _run_commandlet(str(script), _config(tmp_path), timeout_s=2.5)

    assert result.success is False
    assert result.mode == "commandlet"
    assert result.stdout == "partial stdout"
    assert result.stderr == "partial stderr"
    assert "timed out" in result.error.lower()
    assert result.output_file is None
    assert calls[0]["timeout"] == 2.5
    assert len(calls) == 1


@pytest.mark.parametrize("timeout_s", [0, -1, float("inf"), float("nan")])
def test_commandlet_timeout_must_be_finite_and_positive(tmp_path, monkeypatch, timeout_s):
    script = tmp_path / "script.py"
    script.write_text("pass\n")
    monkeypatch.setattr(
        "ue_runner.subprocess.run",
        lambda *args, **kwargs: pytest.fail("invalid timeout reached subprocess"),
    )

    result = _run_commandlet(str(script), _config(tmp_path), timeout_s=timeout_s)

    assert result.success is False
    assert "timeout" in result.error.lower()


def test_cli_forwards_commandlet_timeout(tmp_path, monkeypatch):
    import sys
    import ue_runner

    config = _config(tmp_path)
    script = tmp_path / "script.py"
    script.write_text("pass\n")
    captured = {}

    monkeypatch.setattr(ue_runner, "load_config", lambda path=None: config)

    def fake_run_ue_script(**kwargs):
        captured.update(kwargs)
        return ue_runner.RunResult(success=True, mode="commandlet")

    monkeypatch.setattr(ue_runner, "run_ue_script", fake_run_ue_script)
    monkeypatch.setattr(
        sys,
        "argv",
        ["ue_runner.py", str(script), "--mode", "commandlet", "--commandlet-timeout", "4.5"],
    )

    with pytest.raises(SystemExit) as exc_info:
        ue_runner.main()

    assert exc_info.value.code == 0
    assert captured["commandlet_timeout_s"] == 4.5
