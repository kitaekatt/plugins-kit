"""The ``run`` command end to end with faked tools: report, refusals, cleanup."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from cck_support import FIXTURES, git, make_site

from clean_code_kit import cli, environment
from clean_code_kit import lanes as lanes_mod
from clean_code_kit.report import REPORTS_REL

EDN = {"dryer": ("dry_mixed.edn", "dry.edn"), "crapper": ("crap_no_coverage.edn", "crap.edn")}


@pytest.fixture
def fake_venv(tmp_path, monkeypatch):
    """A venv prefix holding both tool executables at their pinned commits."""
    prefix = tmp_path / "venv"
    for tool in ("dryer", "crapper"):
        exe = environment.tool_path(prefix, tool)
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_text("")
    site = make_site(tmp_path / "site", environment.read_pins())
    monkeypatch.setattr(environment, "venv_prefix", lambda: prefix)
    monkeypatch.setattr(environment, "site_path", lambda: [str(site)])
    monkeypatch.setattr(environment, "parser_cache_dir", lambda: tmp_path / "parsers")
    return site


@pytest.fixture
def fake_tools(monkeypatch):
    """Replace the tool subprocess: write the captured EDN sample for that tool."""
    behaviour = {"mode": "ok", "calls": []}

    def run_tool(argv, cwd, env):
        tool = Path(argv[0]).name.split(".")[0]
        behaviour["calls"].append((tool, Path(cwd)))
        if behaviour["mode"] == "fail":
            return subprocess.CompletedProcess(
                argv, 1, "", "Traceback\ntree_sitter_language_pack.DownloadError: offline\n")
        if behaviour["mode"] == "empty":
            return subprocess.CompletedProcess(argv, 0, "No source files to analyze.\n", "")
        fixture, name = EDN[tool]
        (Path(cwd) / ".metrics").mkdir(exist_ok=True)
        shutil.copy(FIXTURES / fixture, Path(cwd) / ".metrics" / name)
        return subprocess.CompletedProcess(argv, 0, "report text\n", f"Wrote {name}\n")

    monkeypatch.setattr(lanes_mod, "run_tool", run_tool)
    return behaviour


def _run(repo, *extra):
    return cli.main(["run", "--target", str(repo), *extra])


def _report_dir(out: str) -> Path:
    last = out.strip().splitlines()[-1]
    assert last.startswith("REPORT ")
    return Path(last[len("REPORT "):])


def test_run_writes_the_report_under_local_data(target_repo, fake_venv, fake_tools, capsys):
    before = git(target_repo, "status", "--porcelain", "--ignored")
    assert _run(target_repo) == 0
    out_dir = _report_dir(capsys.readouterr().out)
    assert out_dir.is_absolute()
    rel = out_dir.relative_to(target_repo.resolve())
    assert rel.parent.as_posix() == REPORTS_REL.as_posix() == ".local-data/plugins-kit/clean-code-kit/reports"
    head = git(target_repo, "rev-parse", "HEAD").strip()
    assert re.fullmatch(r"\d{8}T\d{6}Z-" + head[:8], rel.name)
    doc = json.loads((out_dir / "findings.json").read_text())
    assert doc["schema"] == "clean-code-kit.findings/v1"
    assert doc["target"] == {"repo": str(target_repo.resolve()), "head": head,
                             "snapshot": "head", "dirty_paths": 0}
    assert [lane["name"] for lane in doc["lanes"]] == ["duplication", "complexity"]
    pins = environment.read_pins()
    assert [lane["tool_commit"] for lane in doc["lanes"]] == [pins["dryer"], pins["crapper"]]
    assert {lane["status"] for lane in doc["lanes"]} == {"ok"}
    assert sum(1 for f in doc["findings"] if f["lane"] == "duplication") == 4
    cpx = [f for f in doc["findings"] if f["lane"] == "complexity"]
    assert all(f["path"] and f["start_line"] for f in cpx)
    text = (out_dir / "report.md").read_text()
    assert "location unavailable" not in text and "| 1 | 3 | 1.00 |" in text
    assert f"`{cpx[0]['path']}:{cpx[0]['start_line']}`" in text
    # nothing else was written inside the target
    after = git(target_repo, "status", "--porcelain", "--ignored")
    assert set(after.splitlines()) == set(before.splitlines()) | {"!! .local-data/"}


def test_tools_scan_the_pruned_snapshot_not_the_target(target_repo, fake_venv, fake_tools):
    assert _run(target_repo) == 0
    roots = {cwd for _tool, cwd in fake_tools["calls"]}
    assert len(roots) == 1
    (root,) = roots
    assert target_repo.resolve() not in root.resolve().parents and root.resolve() != target_repo
    assert not (target_repo / ".metrics").exists()


def test_scratch_directory_removed_when_the_tool_fails(target_repo, fake_venv, fake_tools,
                                                       monkeypatch, capsys):
    made = []
    real = tempfile.mkdtemp

    def mkdtemp(*args, **kwargs):
        made.append(Path(real(*args, **kwargs)))
        return str(made[-1])

    monkeypatch.setattr(tempfile, "mkdtemp", mkdtemp)
    fake_tools["mode"] = "fail"
    assert _run(target_repo) == 1
    err = capsys.readouterr().err
    assert "tree_sitter_language_pack.DownloadError: offline" in err
    assert "parsers are fetched on first use and need network once" in err
    assert len(made) == 1 and made[0].name.startswith("clean-code-kit-")
    assert not made[0].exists()
    assert not (target_repo / ".local-data").exists()


def test_empty_selection_reports_no_source_files(target_repo, fake_venv, fake_tools, capsys):
    fake_tools["mode"] = "empty"
    assert _run(target_repo) == 0
    doc = json.loads((_report_dir(capsys.readouterr().out) / "findings.json").read_text())
    assert [lane["status"] for lane in doc["lanes"]] == ["no-source-files", "no-source-files"]
    assert doc["findings"] == []


def test_provenance_mismatch_exits_2_naming_both_shas(target_repo, tmp_path, fake_venv, fake_tools,
                                                      monkeypatch, capsys):
    pins = environment.read_pins()
    wrong = "0" * 40
    site = make_site(tmp_path / "other-site", dict(pins, dryer=wrong))
    monkeypatch.setattr(environment, "site_path", lambda: [str(site)])
    assert _run(target_repo) == 2
    err = capsys.readouterr().err
    assert "installed tool does not match the pinned commit; run bootstrap" in err
    assert wrong in err and pins["dryer"] in err
    assert fake_tools["calls"] == []


def test_missing_tool_executable_exits_2(target_repo, tmp_path, fake_venv, fake_tools,
                                         monkeypatch, capsys):
    monkeypatch.setattr(environment, "venv_prefix", lambda: tmp_path / "nowhere")
    assert _run(target_repo) == 2
    assert "is not installed at" in capsys.readouterr().err


def test_report_path_refused_when_not_git_ignored(target_repo, fake_venv, fake_tools, capsys):
    (target_repo / ".gitignore").write_text("ignored.txt\n")
    git(target_repo, "commit", "-q", "-am", "stop ignoring local data")
    assert _run(target_repo) == 2
    err = capsys.readouterr().err
    assert "is not git-ignored" in err and ".git/info/exclude" in err.replace("\\", "/")
    assert not (target_repo / ".local-data").exists()
    assert fake_tools["calls"] == []


def test_report_path_accepted_when_ignored_through_info_exclude(target_repo, fake_venv, fake_tools):
    (target_repo / ".gitignore").write_text("ignored.txt\n")
    git(target_repo, "commit", "-q", "-am", "stop ignoring local data")
    (target_repo / ".git" / "info").mkdir(exist_ok=True)
    (target_repo / ".git" / "info" / "exclude").write_text(".local-data/\n")
    assert _run(target_repo) == 0


def test_missing_config_exits_2(target_repo, fake_venv, fake_tools, capsys):
    (target_repo / ".claude" / "clean-code-kit" / "config.yaml").unlink()
    assert _run(target_repo) == 2
    assert "references/configuring.md" in capsys.readouterr().err


def test_missing_source_root_in_snapshot_exits_2(target_repo, fake_venv, fake_tools, capsys):
    shutil.rmtree(target_repo / "tests")
    git(target_repo, "commit", "-q", "-am", "drop tests")
    assert _run(target_repo) == 2
    assert "test_roots entry 'tests' does not exist in the head snapshot" in capsys.readouterr().err


def test_lane_option_selects_lanes(target_repo, fake_venv, fake_tools, capsys):
    assert _run(target_repo, "--lane", "complexity") == 0
    doc = json.loads((_report_dir(capsys.readouterr().out) / "findings.json").read_text())
    assert [lane["name"] for lane in doc["lanes"]] == ["complexity"]
    assert [tool for tool, _cwd in fake_tools["calls"]] == ["crapper"]


def test_snapshot_option_overrides_config(target_repo, fake_venv, fake_tools, capsys):
    (target_repo / "src" / "pkg" / "extra.py").write_text("def x():\n    return 1\n")
    assert _run(target_repo, "--snapshot", "working-tree") == 0
    doc = json.loads((_report_dir(capsys.readouterr().out) / "findings.json").read_text())
    assert doc["target"]["snapshot"] == "working-tree"
    assert doc["target"]["dirty_paths"] == 1


def test_unknown_lane_is_a_usage_error(target_repo, fake_venv, fake_tools):
    with pytest.raises(SystemExit) as info:
        _run(target_repo, "--lane", "mutation")
    assert info.value.code == 2


def test_target_outside_a_repository_exits_2(tmp_path, fake_venv, fake_tools, capsys):
    outside = tmp_path / "plain"
    outside.mkdir()
    assert _run(outside) == 2
    assert "rev-parse --show-toplevel failed" in capsys.readouterr().err


def test_lanes_command_prints_registered_names(capsys):
    assert cli.main(["lanes"]) == 0
    assert capsys.readouterr().out.splitlines() == ["duplication", "complexity"]


def test_second_run_in_the_same_second_on_the_same_head_is_refused(target_repo, fake_venv, fake_tools,
                                                                   monkeypatch, capsys):
    from datetime import datetime, timezone

    from clean_code_kit import report

    fixed = datetime(2026, 11, 14, 12, 0, 0, tzinfo=timezone.utc)
    real = report.report_dir
    monkeypatch.setattr(report, "report_dir", lambda repo, head: real(repo, head, now=fixed))
    assert _run(target_repo) == 0
    first = _report_dir(capsys.readouterr().out)
    before = (first / "findings.json").read_text()
    calls = len(fake_tools["calls"])
    assert _run(target_repo) == 2
    err = capsys.readouterr().err
    assert f"report directory {first} already exists" in err
    assert len(fake_tools["calls"]) == calls  # refused before any tool ran
    assert (first / "findings.json").read_text() == before


def test_report_write_refuses_an_existing_directory(tmp_path):
    from clean_code_kit import report
    from clean_code_kit.errors import Refusal

    out = tmp_path / "r"
    out.mkdir()
    (out / "findings.json").write_text("old\n")
    with pytest.raises(Refusal, match="already exists"):
        report.write(out, {"schema": "x"}, "text\n")
    assert (out / "findings.json").read_text() == "old\n"
