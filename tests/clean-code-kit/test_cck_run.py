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
    assert {k: doc["target"][k] for k in ("repo", "head", "snapshot", "dirty_paths", "dirty_path_names")} == {
        "repo": str(target_repo.resolve()), "head": head, "snapshot": "head",
        "dirty_paths": 0, "dirty_path_names": []}
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


# --- client feedback: coverage, applicability, listings, dirty paths, preconditions, init ---

CONFIG_REL = Path(".claude") / "clean-code-kit" / "config.yaml"


def _commit_all(repo, message="more"):
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


def _run_doc(repo, capsys, *extra):
    assert _run(repo, *extra) == 0
    out_dir = _report_dir(capsys.readouterr().out)
    return json.loads((out_dir / "findings.json").read_text()), (out_dir / "report.md").read_text()


def test_report_counts_tracked_vs_measured_files_by_extension(target_repo, fake_venv, fake_tools, capsys):
    (target_repo / "tools").mkdir()
    for n in range(3):
        (target_repo / "tools" / f"x{n}.gdshader").write_text("extends Node\n")
    (target_repo / "src" / "pkg" / "empty.py").write_text("X = 1\n")
    _commit_all(target_repo)
    doc, text = _run_doc(target_repo, capsys)
    by_ext = doc["target"]["coverage"]["by_extension"]
    assert by_ext[".gdshader"] == {"tracked": 3, "supported": False, "kind": "source-unmeasured",
                             "measured": {"duplication": 0, "complexity": 0},
                             "skipped": {"duplication": 0, "complexity": 0}}
    # a.py, b.py, empty.py under src; bootstrap_guard.py is excluded; tests/test_a.py is outside source_roots
    assert by_ext[".py"] == {"tracked": 5, "supported": True, "kind": "measured",
                             "measured": {"duplication": 3, "complexity": 3},
                             "skipped": {"duplication": 0, "complexity": 0}}
    assert by_ext[".ts"]["tracked"] == 1 and by_ext[".ts"]["measured"] == {"duplication": 0, "complexity": 0}
    assert "| .gdshader | 3 | 0 | 0 | 0 | 0 | source, unmeasured |" in text
    assert "| .py | 5 | 3 | 3 | 0 | 0 | yes |" in text


def test_coverage_separates_assets_from_source_and_folds_case(target_repo, fake_venv, fake_tools, capsys):
    for name in ("a.PNG", "b.png", "c.JPG", "d.jpg", "m.glb"):
        (target_repo / "src" / name).write_text("x")
    (target_repo / "src" / "g.GDSHADER").write_text("extends Node\n")
    (target_repo / "src" / "h.cs").write_text("int x;\n")
    _commit_all(target_repo)
    doc, text = _run_doc(target_repo, capsys)
    by_ext = doc["target"]["coverage"]["by_extension"]
    assert by_ext[".png"]["tracked"] == 2 and by_ext[".png"]["kind"] == "other"
    assert by_ext[".jpg"]["tracked"] == 2 and ".PNG" not in by_ext and ".JPG" not in by_ext
    assert by_ext[".gdshader"]["kind"] == "source-unmeasured" and by_ext[".py"]["kind"] == "measured"
    section = text.split("## Coverage")[1].split("\n## ")[0]
    assert ".png" not in section and ".glb" not in section and ".jpg" not in section
    assert "| .cs | 1 | 0 | 0 | 0 | 0 | source, unmeasured |" in section
    others = sum(r["tracked"] for r in by_ext.values() if r["kind"] == "other")
    assert f"other files: {others}" in section


def test_duplication_is_not_applicable_when_dryer_reads_fewer_than_two_files(
        target_repo, fake_venv, fake_tools, capsys):
    cfg = target_repo / CONFIG_REL
    cfg.write_text(cfg.read_text().replace("languages: [python]", "languages: [cpp]"))
    (target_repo / "src" / "pkg" / "app.hxx").write_text("export const f = () => 1;\n")
    (target_repo / "src" / "pkg" / "lib.hxx").write_text("export const g = () => 2;\n")
    _commit_all(target_repo)
    doc, text = _run_doc(target_repo, capsys)
    dup, cpx = doc["lanes"]
    assert dup["status"] == "no-comparable-source" and cpx["status"] == "ok"
    assert dup["detail"]["comparable_files"] == 0
    assert dup["detail"]["ignored_extensions"] == {".hxx": 2}
    assert [tool for tool, _cwd in fake_tools["calls"]] == ["crapper"]
    assert "Status: no-comparable-source" in text and ".hxx" in text


def test_duplication_with_one_readable_file_is_not_applicable(target_repo, fake_venv, fake_tools, capsys):
    cfg = target_repo / CONFIG_REL
    cfg.write_text(cfg.read_text().replace('- "**/bootstrap_guard.py"', '- "**/bootstrap_guard.py"\n  - "src/pkg/b.py"'))
    _commit_all(target_repo)
    doc, _text = _run_doc(target_repo, capsys)
    assert doc["lanes"][0]["status"] == "no-comparable-source"
    assert doc["lanes"][0]["detail"]["comparable_files"] == 1


def test_duplication_notes_ignored_extensions_even_when_it_runs(target_repo, fake_venv, fake_tools, capsys):
    cfg = target_repo / CONFIG_REL
    cfg.write_text(cfg.read_text().replace("languages: [python]", "languages: [python, cpp]"))
    (target_repo / "src" / "pkg" / "app.hxx").write_text("export const f = () => 1;\n")
    _commit_all(target_repo)
    doc, text = _run_doc(target_repo, capsys)
    assert doc["lanes"][0]["status"] == "ok"
    assert doc["lanes"][0]["detail"]["ignored_extensions"] == {".hxx": 1}
    assert "dryer ignores" in text


def test_files_with_no_scored_functions_are_listed(target_repo, fake_venv, fake_tools, capsys):
    (target_repo / "src" / "pkg" / "empty.py").write_text("X = 1\n")
    _commit_all(target_repo)
    doc, text = _run_doc(target_repo, capsys)
    cpx = doc["lanes"][1]
    assert cpx["detail"]["files_without_scored_functions"] == ["src/pkg/empty.py"]
    assert "Files with no scored functions: 1" in text and "`src/pkg/empty.py`" in text


def test_tool_skipped_test_files_are_counted_apart_and_not_called_unscored(
        target_repo, fake_venv, fake_tools, capsys):
    tests_dir = target_repo / "src" / "pkg" / "tests"
    tests_dir.mkdir()
    (tests_dir / "t1.py").write_text("X = 1\n")
    (tests_dir / "t2.py").write_text("X = 1\n")
    (target_repo / "src" / "pkg" / "test_c.py").write_text("X = 1\n")
    _commit_all(target_repo)
    doc, text = _run_doc(target_repo, capsys)
    row = doc["target"]["coverage"]["by_extension"][".py"]
    assert row["measured"] == {"duplication": 2, "complexity": 2}
    assert row["skipped"] == {"duplication": 3, "complexity": 3}
    cpx = doc["lanes"][1]["detail"]
    assert cpx["files_without_scored_functions"] == []
    assert cpx["files_selected"] == 2 and cpx["files_skipped_by_tool"] == 3
    assert doc["lanes"][0]["detail"]["files_skipped_by_tool"] == 3
    assert "| .py | 7 | 2 | 2 | 3 | 3 | yes |" in text
    assert "t1.py" not in text.split("Files with no scored functions")[1]


def test_unscored_listing_in_report_is_capped_but_json_is_complete(target_repo, fake_venv, fake_tools, capsys):
    for n in range(25):
        (target_repo / "src" / "pkg" / f"z{n:02d}.py").write_text("X = 1\n")
    _commit_all(target_repo)
    doc, text = _run_doc(target_repo, capsys)
    assert len(doc["lanes"][1]["detail"]["files_without_scored_functions"]) == 25
    assert "Files with no scored functions: 25" in text
    assert "`src/pkg/z19.py`" in text and "`src/pkg/z20.py`" not in text
    assert "5 more are in findings.json" in text


def test_dirty_paths_are_named_and_the_config_is_not_counted(target_repo, fake_venv, fake_tools, capsys):
    git(target_repo, "rm", "-q", "--cached", str(CONFIG_REL))
    git(target_repo, "commit", "-q", "-m", "untrack the config")
    (target_repo / "src" / "pkg" / "extra.py").write_text("def x():\n    return 1\n")
    doc, text = _run_doc(target_repo, capsys)
    assert doc["target"]["dirty_paths"] == 1
    assert doc["target"]["dirty_path_names"] == ["src/pkg/extra.py"]
    assert "Dirty paths in the working tree at run time: 1 (`src/pkg/extra.py`)" in text


def test_every_failed_precondition_is_reported_together(target_repo, tmp_path, fake_venv, fake_tools,
                                                        monkeypatch, capsys):
    (target_repo / CONFIG_REL).unlink()
    (target_repo / ".gitignore").write_text("ignored.txt\n")
    git(target_repo, "commit", "-q", "-am", "stop ignoring local data")
    monkeypatch.setattr(environment, "venv_prefix", lambda: tmp_path / "nowhere")
    assert _run(target_repo) == 2
    err = capsys.readouterr().err
    assert "4 preconditions failed" in err
    assert "no config at" in err and "is not git-ignored" in err and "is not installed at" in err
    assert fake_tools["calls"] == []
    assert not (target_repo / ".local-data").exists()


def test_a_single_failed_precondition_keeps_its_plain_message(target_repo, fake_venv, fake_tools, capsys):
    (target_repo / CONFIG_REL).unlink()
    assert _run(target_repo) == 2
    err = capsys.readouterr().err
    assert err.startswith("clean-code-kit: no config at") and "preconditions failed" not in err


def test_init_prints_a_valid_config_and_writes_nothing(target_repo, capsys):
    from clean_code_kit import config as config_mod

    (target_repo / CONFIG_REL).unlink()
    (target_repo / "tools").mkdir()
    (target_repo / "tools" / "x.gdshader").write_text("shader_type spatial;\n")
    _commit_all(target_repo)
    assert cli.main(["init", "--target", str(target_repo)]) == 0
    text = capsys.readouterr().out
    assert not (target_repo / CONFIG_REL).exists()
    import yaml

    cfg = config_mod.parse(Path("proposed"), yaml.safe_load(text))
    assert cfg.languages == ["python", "typescript"]
    assert cfg.source_roots == ["src/pkg"] and cfg.test_roots == ["tests"]
    assert "# .gdshader: 1 file" in text and "no lane reads" in text


def test_init_proposes_gdscript_and_cpp_when_those_files_are_tracked(tmp_path, capsys):
    import yaml
    from clean_code_kit import config as config_mod

    files = ["game/a.gd", "game/b.gd", "kernel/k.cpp", "kernel/k.hpp"]
    text = _init_text(_bare_repo(tmp_path, files), capsys)
    cfg = config_mod.parse(Path("proposed"), yaml.safe_load(text))
    assert cfg.languages == ["gdscript", "cpp"]


def _init_text(repo, capsys):
    _commit_all(repo)
    assert cli.main(["init", "--target", str(repo)]) == 0
    return capsys.readouterr().out


def _bare_repo(tmp_path, files):
    repo = tmp_path / "proj"
    for rel in files:
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text("x = 1\n")
    git(repo, "init", "-q")
    return repo


def test_init_does_not_root_a_directory_for_one_file_and_lists_it(tmp_path, capsys):
    files = [f"src/m{n}.py" for n in range(3)] + ["godot/extensions/kernel/id.mjs"]
    text = _init_text(_bare_repo(tmp_path, files), capsys)
    assert "source_roots: [src]" in text
    assert "# godot/extensions/kernel/id.mjs" in text and "not proposed" in text


def test_init_proposes_the_deepest_directory_holding_a_small_tree(tmp_path, capsys):
    files = [f"src/m{n}.py" for n in range(3)] + ["tools/a/deep/x.py", "tools/a/deep/y.py"]
    text = _init_text(_bare_repo(tmp_path, files), capsys)
    assert "source_roots: [src, tools/a/deep]" in text


def test_init_includes_hidden_directories_git_tracks(tmp_path, capsys):
    files = [f"src/m{n}.py" for n in range(3)] + [f".claude/hooks/h{n}.mjs" for n in range(3)]
    text = _init_text(_bare_repo(tmp_path, files), capsys)
    assert "source_roots: [.claude/hooks, src]" in text


def test_init_roots_are_posix_even_where_os_path_is_ntpath(tmp_path, capsys, monkeypatch):
    import ntpath
    import types

    from clean_code_kit import init as init_mod

    monkeypatch.setattr(init_mod, "os", types.SimpleNamespace(path=ntpath), raising=False)
    files = [f"src/m{n}.py" for n in range(3)] + ["tools/a/deep/x.py", "tools/a/deep/y.py"]
    roots, _stray = init_mod._source_roots(files, {".py"})
    assert "tools/a/deep" in roots and not any("\\" in r for r in roots)


def test_init_lists_every_source_unmeasured_extension_and_collapses_the_rest(tmp_path, capsys):
    files = [f"src/m{n}.py" for n in range(3)] + ["src/a.go", "src/b.GO", "src/b.rs", "src/c.png", "src/d.png", "src/e.uid"]
    text = _init_text(_bare_repo(tmp_path, files), capsys)
    assert "# .go: 2 files, no lane reads this extension" in text
    assert "# .rs: 1 file, no lane reads this extension" in text
    assert "# other files: 3" in text and ".png" not in text and ".uid" not in text


def test_init_refuses_a_repository_with_no_supported_source(tmp_path, capsys):
    repo = tmp_path / "plain"
    repo.mkdir()
    (repo / "a.gdshader").write_text("extends Node\n")
    git(repo, "init", "-q")
    _commit_all(repo)
    assert cli.main(["init", "--target", str(repo)]) == 2
    assert "no tracked file has an extension a lane reads" in capsys.readouterr().err


def test_report_summarizes_the_complexity_distribution(target_repo, fake_venv, fake_tools, capsys):
    doc, text = _run_doc(target_repo, capsys)
    assert doc["lanes"][1]["detail"]["complexity_distribution"] == {
        "1": 0, "2-5": 4, "6-10": 1, "11-20": 0, "21-50": 0, ">50": 0}
    assert "| 2-5 | 4 |" in text and "| 6-10 | 1 |" in text


def test_min_complexity_filters_the_table_but_not_findings_json(target_repo, fake_venv, fake_tools, capsys):
    cfg = target_repo / CONFIG_REL
    cfg.write_text(cfg.read_text().replace("top: 50}", "top: 50, min_complexity: 4}"))
    _commit_all(target_repo)
    doc, text = _run_doc(target_repo, capsys)
    assert sum(1 for f in doc["findings"] if f["lane"] == "complexity") == 5
    assert "with complexity >= 4" in text
    assert "`branchy`" in text and "`total_price`" not in text


def test_empty_selection_stays_no_source_files_not_no_comparable_source(
        target_repo, fake_venv, fake_tools, capsys):
    cfg = target_repo / CONFIG_REL
    cfg.write_text(cfg.read_text().replace('- "**/bootstrap_guard.py"', '- "src/*"'))
    _commit_all(target_repo)
    fake_tools["mode"] = "empty"
    doc, _text = _run_doc(target_repo, capsys)
    assert [lane["status"] for lane in doc["lanes"]] == ["no-source-files", "no-source-files"]
    assert [tool for tool, _cwd in fake_tools["calls"]] == ["dryer", "crapper"]


def test_init_narrows_a_top_whose_files_share_one_subtree(tmp_path, capsys):
    files = [f"artsource/wood-shader-lab/shaders/s{n}.py" for n in range(4)] \
        + [f"artsource/wood-shader-lab/tools/t{n}.py" for n in range(4)]
    text = _init_text(_bare_repo(tmp_path, files), capsys)
    assert "source_roots: [artsource/wood-shader-lab]" in text
    assert "source root artsource/wood-shader-lab: 8 source files" in text


def test_init_keeps_a_top_whose_files_span_several_subdirectories(tmp_path, capsys):
    files = ["src/a/x.py", "src/a/x2.py", "src/b/y.py", "src/c/z.py"]
    text = _init_text(_bare_repo(tmp_path, files), capsys)
    assert "source_roots: [src]" in text


def test_init_refusal_names_the_real_reason_when_every_source_file_is_at_the_root(tmp_path, capsys):
    repo = tmp_path / "rootonly"
    repo.mkdir()
    for name in ("a.py", "b.py", "c.py"):
        (repo / name).write_text("x = 1\n")
    git(repo, "init", "-q")
    _commit_all(repo)
    assert cli.main(["init", "--target", str(repo)]) == 2
    err = capsys.readouterr().err
    assert "3 supported files at the repository root" in err
    assert "every top-level directory holds a single supported file" not in err
