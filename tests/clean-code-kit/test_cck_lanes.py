"""Lane argv and parsing, and the lane runner's statuses."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from cck_support import FIXTURES

from clean_code_kit import lanes as lanes_mod
from clean_code_kit.errors import ToolFailure
from clean_code_kit.lanes import LANES, complexity, duplication, run_lane
from clean_code_kit.model import LaneContext

DUP = {"enabled": True, "threshold": 0.82, "min_lines": 4, "group_identical": True}
CPX = {"enabled": True, "coverage": False, "top": 50}


def _ctx(root: Path, settings: dict) -> LaneContext:
    return LaneContext(repo=root, analysis_root=root, exec_env=None, settings=settings,
                       source_roots=["src"], test_roots=["tests"], languages=["python"],
                       venv_prefix=root / "venv", parser_cache=root / "parsers")


def _place(root: Path, fixture: str, name: str) -> None:
    (root / ".metrics").mkdir(exist_ok=True)
    shutil.copy(FIXTURES / fixture, root / ".metrics" / name)


def test_duplication_argv():
    argv = duplication.argv(_ctx(Path("/x"), DUP))
    assert argv == ["--root", ".", "--source-root", "src", "--threshold", "0.82",
                    "--min-lines", "4", "--group-identical"]
    assert "--group-identical" not in duplication.argv(_ctx(Path("/x"), dict(DUP, group_identical=False)))


def test_complexity_argv_never_measures_coverage():
    assert complexity.argv(_ctx(Path("/x"), CPX)) == ["--root", ".", "--source-root", "src",
                                                      "--no-coverage"]


def test_executable_defaults_to_the_venv_tool(tmp_path):
    ctx = _ctx(tmp_path, DUP)
    assert duplication.executable(ctx).name.startswith("dryer")
    assert complexity.executable(ctx).name.startswith("crapper")
    assert tmp_path / "venv" in duplication.executable(ctx).parents


def test_duplication_parse_yields_grouped_findings_first(tmp_path):
    _place(tmp_path, "dry_mixed.edn", "dry.edn")
    findings = duplication.parse(_ctx(tmp_path, DUP))
    assert [f.rank for f in findings] == [1, 2, 3, 4]
    first = findings[0]
    assert first.kind == "duplicate"
    assert first.metrics["copies"] == 3
    assert first.group == [
        {"path": "src/pkg/a.py", "start_line": 1, "end_line": 7},
        {"path": "src/pkg/b.py", "start_line": 1, "end_line": 7},
        {"path": "src/pkg/b.py", "start_line": 10, "end_line": 16},
    ]
    assert (first.path, first.start_line, first.end_line) == ("src/pkg/a.py", 1, 7)
    assert all(f.metrics["copies"] == 2 and len(f.group) == 2 for f in findings[1:])
    assert all(f.metrics["score"] == 0.8 for f in findings[1:])


def test_duplication_ranks_copies_before_score(tmp_path):
    (tmp_path / ".metrics").mkdir()
    span = '{:file "%s", :start-line 1, :end-line 5}'
    pair = ('{:score 1.0 :language "python" :left %s :right %s :left-nodes 30 :right-nodes 30}'
            % (span % "p.py", span % "q.py"))
    triple = ('{:score 0.85 :language "python" :left %s :right %s :others [%s] :left-nodes 30'
              ' :right-nodes 30}' % (span % "r.py", span % "s.py", span % "t.py"))
    (tmp_path / ".metrics" / "dry.edn").write_text("{:candidates [%s %s]}" % (pair, triple))
    findings = duplication.parse(_ctx(tmp_path, DUP))
    assert [f.metrics["copies"] for f in findings] == [3, 2]


def test_complexity_parse_ranks_by_complexity_and_carries_location(tmp_path):
    _place(tmp_path, "crap_no_coverage.edn", "crap.edn")
    findings = complexity.parse(_ctx(tmp_path, CPX))
    assert [f.metrics["complexity"] for f in findings] == [6, 3, 3, 3, 3]
    assert [f.rank for f in findings] == [1, 2, 3, 4, 5]
    assert (findings[0].namespace, findings[0].symbol) == ("pkg.b", "branchy")
    assert [(f.path, f.start_line, f.end_line) for f in findings[:2]] == [
        ("src/pkg/b.py", 4, None), ("src/pkg/a.py", 10, None)]
    assert all(f.path and f.start_line and f.group is None for f in findings)
    assert [f.symbol for f in findings[1:]] == ["total_price", "weight", "sum_cost", "sum_cost_again"]


def _fake(monkeypatch, *, code=0, stdout="", stderr="", fixture=None):
    calls = []

    def run_tool(argv, cwd, env):
        calls.append((argv, cwd, env))
        if fixture:
            _place(Path(cwd), *fixture)
        return subprocess.CompletedProcess(argv, code, stdout, stderr)

    monkeypatch.setattr(lanes_mod, "run_tool", run_tool)
    return calls


def test_empty_selection_is_status_no_source_files(tmp_path, monkeypatch):
    _fake(monkeypatch, stdout="No source files to analyze.\n")
    result = run_lane(duplication, _ctx(tmp_path, DUP), "a" * 40)
    assert result.status == "no-source-files"
    assert result.findings == []


def test_a_stale_edn_is_not_read_after_an_empty_selection(tmp_path, monkeypatch):
    _place(tmp_path, "dry_mixed.edn", "dry.edn")
    _fake(monkeypatch, stdout="No source files to analyze.\n")
    assert run_lane(duplication, _ctx(tmp_path, DUP), "a" * 40).status == "no-source-files"


def test_missing_edn_without_the_empty_message_is_a_tool_failure(tmp_path, monkeypatch):
    _fake(monkeypatch, stdout="something else\n")
    with pytest.raises(ToolFailure, match="wrote no"):
        run_lane(duplication, _ctx(tmp_path, DUP), "a" * 40)


def test_lane_runs_in_the_analysis_root_with_the_parser_cache(tmp_path, monkeypatch):
    calls = _fake(monkeypatch, fixture=("crap_no_coverage.edn", "crap.edn"))
    result = run_lane(complexity, _ctx(tmp_path, CPX), "b" * 40)
    argv, cwd, env = calls[0]
    assert cwd == tmp_path
    assert env["TREE_SITTER_LANGUAGE_PACK_CACHE_DIR"] == str(tmp_path / "parsers")
    assert argv[0] == str(complexity.executable(_ctx(tmp_path, CPX)))
    assert result.status == "ok" and result.tool_commit == "b" * 40
    assert result.record()["argv"] == argv


def test_parser_download_failure_adds_the_first_use_hint(tmp_path, monkeypatch):
    stderr = "Traceback ...\ntree_sitter_language_pack.DownloadError: fetch failed\n"
    _fake(monkeypatch, code=1, stderr=stderr)
    with pytest.raises(ToolFailure) as info:
        run_lane(duplication, _ctx(tmp_path, DUP), "a" * 40)
    message = str(info.value)
    assert message.startswith(stderr.rstrip("\n"))
    assert "parsers are fetched on first use and need network once" in message


def test_registry_order():
    assert list(LANES) == ["duplication", "complexity"]
