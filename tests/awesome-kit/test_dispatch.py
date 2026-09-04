"""Tests for the durable Codex dispatch cache."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import time

import dispatch


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
        "gpt-5.6-sol",
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
    monkeypatch.setattr(dispatch.os, "name", "nt")
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
    monkeypatch.setattr(dispatch.os, "name", "posix")
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
