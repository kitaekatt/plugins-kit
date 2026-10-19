"""Tests for scripts/retired_plugin_audit.py against a fake DEVROOT.

The legacy names are spelled as fragments so this file does not match its own
patterns when the audit scans plugins-kit.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "retired_plugin_audit.py"
_SPEC = importlib.util.spec_from_file_location("retired_plugin_audit", _SCRIPT)
audit_mod = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
sys.modules["retired_plugin_audit"] = audit_mod
_SPEC.loader.exec_module(audit_mod)

LSK = "llm-scripting-" + "kit"
LSK_MOD = "llm_scripting_" + "kit"
CPK = "content-pipeline-" + "kit"
CPK_MOD = "content_" + "pipeline"
JK = "job-" + "kit"
JK_MOD = "job_" + "kit"
MKT = "plugins-" + "kit"


def _write(path: Path, content: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


@pytest.fixture()
def devroot(tmp_path: Path) -> Path:
    root = tmp_path / "devroot"

    # A git project: tracked-style source, a git-ignored settings file, and
    # things that must be skipped.
    git_proj = root / "gitproj"
    git_proj.mkdir(parents=True)
    _git(git_proj, "init", "-q")
    _write(git_proj / ".gitignore", ".claude/settings.local.json\nignored.txt\n")
    _write(git_proj / "app.py", f"import os\nfrom {LSK_MOD}.completion import call\n")
    _write(
        git_proj / ".claude" / "settings.local.json",
        json.dumps({"enabledPlugins": {f"{LSK}@{MKT}": True}}, indent=2),
    )
    _write(git_proj / "bootstrap.json", json.dumps({"shared_lib_imports": [JK_MOD]}))
    _write(git_proj / "run.sh", f"#!/bin/sh\n{JK} run --all\n")
    _write(git_proj / "README.md", f"We used {CPK} once.\n")
    _write(git_proj / "install.md", f"claude plugin install {CPK}@{MKT}\n")
    _write(git_proj / "launch.py", f'P = "~/.claude/plugins/data/{MKT}/{LSK}/.venv"\n')
    _write(git_proj / "ignored.txt", f"{LSK}@{MKT}\n")
    _write(git_proj / ".venv" / "lib" / "dep.py", f"import {LSK_MOD}\n")
    _write(git_proj / "blob.dat", b"\x00\x01" + f"import {LSK_MOD}".encode())
    _write(git_proj / "node_modules" / "pkg" / "index.js", f"// {LSK}\n")

    # A non-git project: walked, with the same skips plus an oversize file.
    plain = root / "plain"
    _write(plain / "main.py", f"import {CPK_MOD}\n")
    _write(plain / "__pycache__" / "x.py", f"import {JK_MOD}\n")
    _write(plain / ".claude" / "settings.json", json.dumps({"enabledPlugins": {f"{JK}@{MKT}": True}}))
    _write(plain / "huge.txt", f"{LSK}\n" + "x" * (audit_mod.MAX_FILE_BYTES + 1))

    # A marketplace checkout: its own legacy tree is a self-reference.
    marketplace = root / (MKT + "-copy")
    _write(marketplace / "plugins" / JK / "lib" / JK_MOD / "a.py", f"import {JK_MOD}\n")
    _write(marketplace / "tests" / "llm-kit" / "t.py", f"import {LSK_MOD}\n")
    _write(marketplace / "plugins" / "other" / "use.py", f"import {JK_MOD}\n")

    # Hidden children are not projects; plain files in DEVROOT are ignored.
    _write(root / ".hidden" / "x.py", f"import {LSK_MOD}\n")
    _write(root / "loose.py", f"import {LSK_MOD}\n")
    return root


def _by_name(result: dict) -> dict[str, dict]:
    return {p["name"]: p for p in result["projects"]}


def _hits(project: dict) -> set[tuple[str, str]]:
    return {(h["path"], h["category"]) for h in project["hits"]}


def test_git_project_categories_and_exclusions(devroot: Path) -> None:
    project = _by_name(audit_mod.audit([devroot]))["gitproj"]
    assert _hits(project) == {
        ("app.py", "import"),
        (".claude/settings.local.json", "enabled-plugin"),
        ("bootstrap.json", "shared-lib-declaration"),
        ("run.sh", "cli-or-venv-path"),
        ("README.md", "mention"),
        ("install.md", "install-name"),
        ("launch.py", "cli-or-venv-path"),
    }
    assert project["skipped_binary"] == 1
    assert project["git"] is True


def test_git_ignored_settings_file_is_read_but_other_ignored_files_are_not(devroot: Path) -> None:
    project = _by_name(audit_mod.audit([devroot]))["gitproj"]
    paths = {h["path"] for h in project["hits"]}
    assert ".claude/settings.local.json" in paths
    assert "ignored.txt" not in paths


def test_skip_dirs_are_never_scanned(devroot: Path) -> None:
    result = _by_name(audit_mod.audit([devroot]))
    for name in ("gitproj", "plain"):
        for hit in result[name]["hits"]:
            parts = hit["path"].split("/")
            assert not set(parts) & audit_mod.SKIP_DIRS


def test_non_git_project_walks_tree_and_reports_oversize(devroot: Path) -> None:
    project = _by_name(audit_mod.audit([devroot]))["plain"]
    assert project["git"] is False
    assert _hits(project) == {
        ("main.py", "import"),
        (".claude/settings.json", "enabled-plugin"),
    }
    assert project["skipped_large"] == 1


def test_marketplace_checkout_self_references_are_counted_not_listed(devroot: Path) -> None:
    project = _by_name(audit_mod.audit([devroot]))[MKT + "-copy"]
    assert _hits(project) == {("plugins/other/use.py", "import")}
    assert project["self"]["total"] == 2
    assert project["self"]["by_category"]["import"] == 2


def test_self_prefixes_apply_only_to_marketplace_named_projects(devroot: Path) -> None:
    _write(devroot / "elsewhere" / "plugins" / JK / "a.py", f"import {JK_MOD}\n")
    project = _by_name(audit_mod.audit([devroot]))["elsewhere"]
    assert _hits(project) == {(f"plugins/{JK}/a.py", "import")}
    assert project["self"]["total"] == 0


def test_hidden_children_and_loose_files_are_not_projects(devroot: Path) -> None:
    names = set(_by_name(audit_mod.audit([devroot])))
    assert names == {"gitproj", "plain", MKT + "-copy"}


def test_specific_match_owns_the_line_over_bare_name(tmp_path: Path) -> None:
    hits = audit_mod.scan_text("notes.md", f"see {MKT}/{LSK}/bin and {LSK}\n")
    assert [h.category for h in hits] == ["cli-or-venv-path"]
    assert hits[0].patterns == [f"path:{LSK}"]


def test_bare_name_is_a_mention_in_docs_and_a_cli_in_shell() -> None:
    assert [h.category for h in audit_mod.scan_text("a.md", f"run {JK} now")] == ["mention"]
    assert [h.category for h in audit_mod.scan_text("a.cmd", f"{JK} now")] == ["cli-or-venv-path"]
    assert [h.category for h in audit_mod.scan_text("bin/launcher", f"exec {JK}")] == [
        "cli-or-venv-path"
    ]


def test_import_pattern_does_not_match_distribution_name() -> None:
    assert audit_mod.scan_text("a.py", f"import {CPK_MOD}_extra\n") == []
    assert [h.category for h in audit_mod.scan_text("a.py", f"from {CPK_MOD}.x import y\n")] == [
        "import"
    ]


def test_long_lines_are_trimmed_around_the_match() -> None:
    line = "a" * 400 + f" import {LSK_MOD} " + "b" * 400
    (hit,) = audit_mod.scan_text("a.py", line)
    assert len(hit.text) <= audit_mod.TRIM_CHARS
    assert LSK_MOD in hit.text


def test_json_cli_exits_zero_with_findings(devroot: Path, capsys: pytest.CaptureFixture) -> None:
    code = audit_mod.main(["--root", str(devroot), "--format", "json"])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["totals"]["projects_with_hits"] == 3
    assert data["totals"]["skipped_large"] == 1


def test_text_cli_lists_hits_and_summary(devroot: Path, capsys: pytest.CaptureFixture) -> None:
    assert audit_mod.main(["--root", str(devroot)]) == 0
    out = capsys.readouterr().out
    assert "app.py:2:" in out
    assert "== Summary ==" in out
    assert "self: 2" in out


def test_unset_devroot_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEVROOT", raising=False)
    with pytest.raises(SystemExit) as excinfo:
        audit_mod.main([])
    assert excinfo.value.code == 2


def test_devroot_env_is_the_default_root(
    devroot: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setenv("DEVROOT", str(devroot))
    assert audit_mod.main(["--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["roots"] == [str(devroot)]


def test_missing_root_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        audit_mod.audit([tmp_path / "nope"])


def test_failing_git_listing_raises(tmp_path: Path) -> None:
    broken = tmp_path / "root" / "broken"
    (broken / ".git").mkdir(parents=True)
    with pytest.raises(RuntimeError, match="git ls-files failed"):
        audit_mod.audit([tmp_path / "root"])
