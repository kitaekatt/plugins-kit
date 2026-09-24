"""Tests for the durable Codex dispatch cache."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import sys
import time
from types import ModuleType, SimpleNamespace

# bootstrap_lib is a shared library linked onto awesome-kit's provisioned venv
# via a .pth file; a bare pytest run has no such link, so the repo checkout's
# copy is put on sys.path directly, mirroring test_orchestration_guidance.py's
# TestRenderedCodexCommandKeepsItsSilentFailureFlags fixture.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_BOOTSTRAP_DIR = str(_REPO_ROOT / "plugins" / "bootstrap")
if _BOOTSTRAP_DIR not in sys.path:
    sys.path.insert(0, _BOOTSTRAP_DIR)

import pytest

import dispatch


def _registry_module(entries=None, *, omit_discover=False):
    """A stand-in llm_scripting_kit exposing the model registry dispatch reads."""
    module = ModuleType("llm_scripting_kit")
    found = entries if entries is not None else {
        "sol": SimpleNamespace(kind="harness", harness="codex", model="gpt-5.6-sol", effort="high"),
        "luna": SimpleNamespace(kind="harness", harness="codex", model="gpt-5.6-luna", effort="medium"),
        "fable": SimpleNamespace(kind="harness", harness="claude", model="fable", effort=None),
        "or-mini": SimpleNamespace(kind="transport", harness=None, model="openai/gpt-mini", effort=None),
    }
    if not omit_discover:
        module.discover_model_entries = lambda project_root=None: SimpleNamespace(
            entries=found, notes=[]
        )
    return module


@pytest.fixture(autouse=True)
def model_registry(monkeypatch):
    """Every dispatch resolves --model through this registry unless a test swaps it."""
    monkeypatch.setitem(sys.modules, "llm_scripting_kit", _registry_module())


def _brief(tmp_path: Path, text: bytes = b"do the unit\n", name: str = "brief.md") -> Path:
    path = tmp_path / name
    path.write_bytes(text)
    return path


def _run_args(tmp_path: Path, brief: Path, cache: Path, *extra: str) -> list[str]:
    return [
        "--label",
        "unit",
        "--brief",
        str(brief),
        "--model",
        "sol",
        "--effort",
        "high",
        "--cwd",
        str(tmp_path),
        "--cache-dir",
        str(cache),
        *extra,
    ]


def test_cache_miss_writes_entry_and_cache_hit_skips_codex(tmp_path, monkeypatch, capsys):
    brief = _brief(tmp_path)
    cache = tmp_path / "cache"
    calls: list[dict[str, object]] = []

    def fake_run(argv, **kwargs):
        calls.append({"argv": argv, **kwargs})
        Path(argv[argv.index("-o") + 1]).write_text("result\n", encoding="utf-8")
        return dispatch.subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(dispatch.subprocess, "run", fake_run)
    assert dispatch.main(_run_args(tmp_path, brief, cache)) == 0
    first_output = capsys.readouterr()
    entry = Path(first_output.out.splitlines()[0])
    assert (entry / "brief.md").read_bytes() == brief.read_bytes()
    assert (entry / "result.md").read_text(encoding="utf-8") == "result\n"
    assert len(calls) == 1

    assert dispatch.main(_run_args(tmp_path, brief, cache)) == 0
    second_output = capsys.readouterr()
    assert f"CACHE HIT {entry}" in second_output.out
    assert str(entry / "result.md") in second_output.out
    assert len(calls) == 1


def test_cache_key_changes_with_brief_and_options(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    first = _brief(tmp_path, b"one\n", "first.md")
    second = _brief(tmp_path, b"two\n", "second.md")
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        Path(argv[argv.index("-o") + 1]).write_text("result\n", encoding="utf-8")
        return dispatch.subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(dispatch.subprocess, "run", fake_run)
    assert dispatch.main(_run_args(tmp_path, first, cache)) == 0
    assert dispatch.main(_run_args(tmp_path, second, cache)) == 0
    assert dispatch.main(_run_args(tmp_path, second, cache, "--effort", "medium")) == 0
    assert len(calls) == 3


def test_cache_key_changes_with_cwd_and_add_dirs(tmp_path, monkeypatch):
    brief = _brief(tmp_path)
    cache = tmp_path / "cache"
    other_cwd = tmp_path / "other"
    other_cwd.mkdir()
    calls = 0

    def fake_run(argv, **kwargs):
        nonlocal calls
        calls += 1
        Path(argv[argv.index("-o") + 1]).write_text("result\n", encoding="utf-8")
        return dispatch.subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(dispatch.subprocess, "run", fake_run)
    assert dispatch.main(_run_args(tmp_path, brief, cache)) == 0
    different_cwd = _run_args(other_cwd, brief, cache)
    assert dispatch.main(different_cwd) == 0
    assert dispatch.main(_run_args(tmp_path, brief, cache, "--add-dir", str(other_cwd))) == 0
    assert calls == 3


def test_no_cache_forces_a_new_run(tmp_path, monkeypatch, capsys):
    brief = _brief(tmp_path)
    cache = tmp_path / "cache"
    calls = 0

    def fake_run(argv, **kwargs):
        nonlocal calls
        calls += 1
        Path(argv[argv.index("-o") + 1]).write_text("result\n", encoding="utf-8")
        return dispatch.subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(dispatch.subprocess, "run", fake_run)
    assert dispatch.main(_run_args(tmp_path, brief, cache)) == 0
    capsys.readouterr()
    assert dispatch.main(_run_args(tmp_path, brief, cache, "--no-cache")) == 0
    assert calls == 2


def test_sweep_removes_old_entries_keeps_new_and_excludes_current(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    old = cache / "20260101-010101-old-entry"
    unrelated = cache / "old-entry"
    new = cache / "new-entry"
    current = cache / "current-entry"
    for entry in (old, unrelated, new, current):
        entry.mkdir()
    (old / "meta.json").write_text("{}", encoding="utf-8")
    old_time = time.time() - (8 * 86400)
    for entry in (old, unrelated, new, current):
        os.utime(entry, (old_time, old_time))
    os.utime(new, None)
    assert dispatch._sweep(cache, 7, current) == (1, 2)
    assert not old.exists()
    assert unrelated.exists()
    assert new.exists()
    assert current.exists()


def test_print_only_emits_exact_codex_argv_without_launch(tmp_path, monkeypatch, capsys):
    brief = _brief(tmp_path)
    cache = tmp_path / "cache"
    monkeypatch.setattr(dispatch.codex_lib, "resolve_cli", lambda name: ("codex",))
    monkeypatch.setattr(dispatch.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))
    assert dispatch.main(_run_args(tmp_path, brief, cache, "--add-dir", str(tmp_path), "--print-only")) == 0
    output = capsys.readouterr().out.splitlines()
    entry = output[0]
    assert entry.startswith(str(cache) + os.sep)
    assert output[1].startswith("ARGV codex exec -s workspace-write ")
    assert "-c sandbox_workspace_write.network_access=true" in output[1]
    assert "-m gpt-5.6-sol" in output[1]
    assert "-c model_reasoning_effort=high" in output[1]
    assert f"-C {tmp_path}" in output[1]
    assert f"--add-dir {tmp_path}" in output[1]
    assert output[1].endswith("--skip-git-repo-check --color never -")
    assert not Path(entry).exists()


def test_print_only_adds_windows_sandbox_config(tmp_path, monkeypatch):
    monkeypatch.setattr(dispatch.codex_lib, "resolve_cli", lambda name: ("codex",))
    monkeypatch.setattr(dispatch.codex_lib.os, "name", "nt")
    argv_line = shlex.join(
        dispatch._argv(
            model="gpt-5.6-sol",
            effort="high",
            sandbox="workspace-write",
            cwd=tmp_path,
            add_dirs=[],
            result=tmp_path / "result.md",
        )
    )
    assert "-c 'windows.sandbox=\"unelevated\"'" in argv_line


def test_print_only_omits_windows_sandbox_config_on_posix(tmp_path, monkeypatch, capsys):
    brief = _brief(tmp_path)
    cache = tmp_path / "cache"
    monkeypatch.setattr(dispatch.codex_lib, "resolve_cli", lambda name: ("codex",))
    monkeypatch.setattr(dispatch.codex_lib.os, "name", "posix")
    assert dispatch.main(_run_args(tmp_path, brief, cache, "--print-only")) == 0
    argv_line = capsys.readouterr().out.splitlines()[1]
    assert "windows.sandbox" not in argv_line


def test_list_output_has_one_line_per_valid_entry(tmp_path, monkeypatch, capsys):
    brief = _brief(tmp_path)
    cache = tmp_path / "cache"

    def fake_run(argv, **kwargs):
        Path(argv[argv.index("-o") + 1]).write_text("abc", encoding="utf-8")
        return dispatch.subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(dispatch.subprocess, "run", fake_run)
    assert dispatch.main(_run_args(tmp_path, brief, cache)) == 0
    capsys.readouterr()
    assert dispatch.main(["--list", "--cache-dir", str(cache)]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1
    timestamp, label, model, exit_code, result_size, entry, result = lines[0].split()
    assert timestamp[:8].isdigit() and len(timestamp) == 15
    assert (label, model, exit_code, result_size) == ("unit", "gpt-5.6-sol", "0", "3")
    assert Path(entry).is_absolute()
    assert Path(result) == Path(entry) / "result.md"
    meta = json.loads(next(cache.glob("*/meta.json")).read_text(encoding="utf-8"))
    assert meta["cache_source"] == "explicit"


def test_list_missing_cache_is_empty_and_read_only(tmp_path, capsys):
    cache = tmp_path / "missing-cache"

    assert dispatch.main(["--list", "--cache-dir", str(cache)]) == 0
    assert capsys.readouterr().out == ""
    assert not cache.exists()


def test_corrupt_object_meta_is_skipped_by_hit_and_list(tmp_path, capsys):
    cache = tmp_path / "cache"
    entry = cache / "20260101-010101-null-meta"
    entry.mkdir(parents=True)
    (entry / "meta.json").write_text("null", encoding="utf-8")
    (entry / "result.md").write_text("result", encoding="utf-8")
    assert dispatch._cache_hit(cache, "key", tmp_path, []) is None
    dispatch._list_entries(cache)
    assert capsys.readouterr().out == ""


def test_cache_hit_rejects_context_mismatch_in_metadata(tmp_path):
    cache = tmp_path / "cache"
    entry = cache / "20260101-010101-context"
    entry.mkdir(parents=True)
    brief = b"brief\n"
    key = dispatch._cache_key("model", "high", "workspace-write", tmp_path, [], brief)
    (entry / "meta.json").write_text(
        json.dumps({"key": key, "cwd": str(tmp_path / "wrong"), "add_dirs": []}),
        encoding="utf-8",
    )
    (entry / "result.md").write_text("result", encoding="utf-8")
    assert dispatch._cache_hit(cache, key, tmp_path, []) is None


def test_windows_cmd_launcher_argv_begins_with_cmd_slash_c(tmp_path, monkeypatch):
    """dispatch.py must consume the shared bootstrap_lib.codex builder.

    A resolved codex.cmd launcher (an npm/scoop install) is not directly
    executable by CreateProcess, so the shared builder wraps it in `cmd /c`.
    A hand-rolled argv starting with the bare string "codex" cannot express
    this at all -- this pins that dispatch renders through the shared
    resolver rather than its own copy.
    """
    resolved_cmd = str(tmp_path / "codex.cmd")
    monkeypatch.setattr(dispatch.codex_lib, "resolve_cli", lambda name: ("cmd", "/c", resolved_cmd))
    argv = dispatch._argv(
        model="gpt-5.6-sol",
        effort="high",
        sandbox="workspace-write",
        cwd=tmp_path,
        add_dirs=[],
        result=tmp_path / "result.md",
    )
    assert argv[:3] == ["cmd", "/c", resolved_cmd]


def test_dispatch_no_longer_hardcodes_a_bare_codex_argv_head():
    """The hand-rolled second implementation is gone, argv head and all."""
    source = Path(dispatch.__file__).read_text(encoding="utf-8")
    assert '"codex",' not in source
    assert "'codex',\n" not in source


def test_cache_hit_replays_the_recorded_nonzero_exit_code(tmp_path, monkeypatch, capsys):
    """A hit is judged by the -o file (codex exits 0 on silent failure -- see
    the comment in _cache_hit, which this test must not require changing),
    but the RETURN VALUE must replay the recorded status. Before the fix, a
    failed dispatch (exit 3) replayed as a successful cache hit (exit 0).
    """
    brief = _brief(tmp_path)
    cache = tmp_path / "cache"

    def fake_run(argv, **kwargs):
        Path(argv[argv.index("-o") + 1]).write_text("partial result\n", encoding="utf-8")
        return dispatch.subprocess.CompletedProcess(argv, 3)

    monkeypatch.setattr(dispatch.subprocess, "run", fake_run)
    assert dispatch.main(_run_args(tmp_path, brief, cache)) == 3
    capsys.readouterr()

    assert dispatch.main(_run_args(tmp_path, brief, cache)) == 3
    assert "CACHE HIT" in capsys.readouterr().out


def test_list_accepts_cwd_and_finds_what_a_dispatch_from_that_cwd_cached(tmp_path, monkeypatch, capsys):
    """--list accepts --cwd. Without it, a listing run from anywhere but
    the dispatch's own cwd cannot see that dispatch's entries.
    """
    brief = _brief(tmp_path)
    project = tmp_path / "project"
    (project / "tmp").mkdir(parents=True)

    def fake_run(argv, **kwargs):
        Path(argv[argv.index("-o") + 1]).write_text("result\n", encoding="utf-8")
        return dispatch.subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(dispatch.subprocess, "run", fake_run)
    assert dispatch.main(
        ["--label", "unit", "--brief", str(brief), "--model", "sol", "--cwd", str(project)]
    ) == 0
    capsys.readouterr()

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert dispatch.main(["--list", "--cwd", str(project)]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1


# --- --model is a model-declaration id (orchestrate routing names it) ------


def _print_only(tmp_path, capsys, *extra):
    brief = _brief(tmp_path)
    args = [
        "--label", "unit", "--brief", str(brief), "--cwd", str(tmp_path),
        "--cache-dir", str(tmp_path / "cache"), "--print-only", *extra,
    ]
    code = dispatch.main(args)
    return code, capsys.readouterr()


def test_model_is_an_entry_id_resolved_to_its_codex_model(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(dispatch.codex_lib, "resolve_cli", lambda name: ("codex",))
    code, captured = _print_only(tmp_path, capsys, "--model", "luna")
    assert code == 0
    argv = captured.out.splitlines()[1]
    assert "-m gpt-5.6-luna" in argv
    # With no --effort, the entry's own default effort applies.
    assert "-c model_reasoning_effort=medium" in argv


def test_an_explicit_effort_wins_over_the_entry_default(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(dispatch.codex_lib, "resolve_cli", lambda name: ("codex",))
    code, captured = _print_only(tmp_path, capsys, "--model", "luna", "--effort", "low")
    assert code == 0
    assert "-c model_reasoning_effort=low" in captured.out.splitlines()[1]


def test_there_is_no_hardcoded_default_model(tmp_path, capsys):
    assert not hasattr(dispatch, "DEFAULT_MODEL")
    with pytest.raises(SystemExit) as exited:
        _print_only(tmp_path, capsys)
    assert exited.value.code == 2
    assert "--model" in capsys.readouterr().err


@pytest.mark.parametrize("entry_id", ["fable", "or-mini", "gpt-5.6-sol", "ghost"])
def test_an_id_without_a_codex_entry_is_refused(tmp_path, capsys, entry_id):
    with pytest.raises(SystemExit) as exited:
        _print_only(tmp_path, capsys, "--model", entry_id)
    assert exited.value.code == 2
    err = capsys.readouterr().err
    assert entry_id in err
    assert "luna, sol" in err


def test_an_absent_model_library_refuses_and_names_the_owner(tmp_path, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "llm_scripting_kit", None)
    code, captured = _print_only(tmp_path, capsys, "--model", "sol")
    assert code == dispatch.EXIT_REFUSED
    assert "claude plugin install llm-scripting-kit@plugins-kit" in captured.err
    assert not (tmp_path / "cache").exists() or not any((tmp_path / "cache").iterdir())


LLM_FLOOR = "0.46.0"


def test_a_too_old_model_library_is_diagnosed_apart_from_absence(tmp_path, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "llm_scripting_kit", _registry_module(omit_discover=True))
    code, captured = _print_only(tmp_path, capsys, "--model", "sol")
    assert code == dispatch.EXIT_REFUSED
    assert "claude plugin update llm-scripting-kit@plugins-kit" in captured.err
    assert "claude plugin install" not in captured.err
    assert LLM_FLOOR in captured.err


def test_list_needs_no_model_library(tmp_path, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "llm_scripting_kit", None)
    assert dispatch.main(["--list", "--cache-dir", str(tmp_path / "none")]) == 0
