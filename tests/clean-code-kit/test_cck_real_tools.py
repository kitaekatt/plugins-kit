"""One run against the real pinned tools, when a venv holding them is available.

Set CLEAN_CODE_KIT_TEST_PYTHON to the interpreter of a venv built from
plugins/clean-code-kit/pyproject.toml; otherwise the provisioned plugin venv is
used when it exists, and the test is skipped when neither does. The tools fetch
their parsers on first use, so a cold parser cache needs network once.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from cck_support import SCRIPTS, git

from clean_code_kit import environment


def _interpreter() -> Path | None:
    named = os.environ.get("CLEAN_CODE_KIT_TEST_PYTHON")
    if named:
        return Path(named)
    venv = environment.plugin_data_dir() / ".venv"
    for rel in (("Scripts", "python.exe"), ("bin", "python")):
        if venv.joinpath(*rel).is_file():
            return venv.joinpath(*rel)
    return None


def _audit(repo: Path) -> dict:
    proc = subprocess.run(
        [str(_interpreter()), str(SCRIPTS / "code_audit.py"), "run", "--target", str(repo)],
        capture_output=True, text=True, env=dict(os.environ, _BOOTSTRAP_GUARD_SKIP_REEXEC="1"),
        timeout=600)
    assert proc.returncode == 0, proc.stderr
    out_dir = Path(proc.stdout.strip().splitlines()[-1].removeprefix("REPORT "))
    return json.loads((out_dir / "findings.json").read_text())


needs_tools = pytest.mark.skipif(_interpreter() is None, reason="no venv with the pinned tools")


@needs_tools
def test_real_tools_audit_a_small_repository(target_repo):
    doc = _audit(target_repo)
    assert {lane["status"] for lane in doc["lanes"]} == {"ok"}
    dup = [f for f in doc["findings"] if f["lane"] == "duplication"]
    assert dup and dup[0]["metrics"]["copies"] == 3
    paths = {span["path"] for f in dup for span in f["group"]}
    assert "src/pkg/bootstrap_guard.py" not in paths
    cpx = [f for f in doc["findings"] if f["lane"] == "complexity"]
    assert cpx[0]["symbol"] == "branchy"
    assert cpx[0]["path"] == "src/pkg/b.py" and cpx[0]["start_line"] >= 1
    assert not (target_repo / ".metrics").exists()


@needs_tools
def test_real_tools_empty_selection_is_no_source_files(target_repo):
    (target_repo / "docs").mkdir()
    (target_repo / "docs" / "notes.md").write_text("# notes\n")
    config = target_repo / ".claude" / "clean-code-kit" / "config.yaml"
    config.write_text(config.read_text().replace("source_roots: [src]", "source_roots: [docs]"))
    git(target_repo, "add", "-A")
    git(target_repo, "commit", "-q", "-m", "docs only")
    doc = _audit(target_repo)
    assert [lane["status"] for lane in doc["lanes"]] == ["no-source-files", "no-source-files"]
    assert doc["findings"] == []


_GD_DUP = """func total(items):
    var sum = 0
    for item in items:
        sum += item.price * item.count
        if item.taxed:
            sum += item.price * 0.2
    return sum
"""
_CPP_DUP = """int total(const std::vector<int>& items) {
    int sum = 0;
    for (int i = 0; i < items.size(); ++i) {
        sum += items[i] * 2;
        if (items[i] > 10) {
            sum += items[i] / 3;
        }
    }
    return sum;
}
"""
_JS_DUP = """function total(items) {
  let sum = 0;
  for (const item of items) {
    sum += item.price * item.count;
    if (item.taxed) {
      sum += item.price * 0.2;
    }
  }
  return sum;
}
"""
_GD_BRANCHY = "func branchy_gd(x):\n" + "".join(
    f"    if x == {i}:\n        return {i}\n" for i in range(12)) + "    return -1\n"
_CPP_BRANCHY = "int branchy_cpp(int x) {\n" + "".join(
    f"    if (x == {i}) {{ return {i}; }}\n" for i in range(12)) + "    return -1;\n}\n"


@needs_tools
def test_real_tools_audit_gdscript_cpp_and_javascript(target_repo):
    config = target_repo / ".claude" / "clean-code-kit" / "config.yaml"
    config.write_text(config.read_text().replace(
        "languages: [python]", "languages: [gdscript, cpp, javascript]"))
    src = target_repo / "src" / "multi"
    src.mkdir()
    for ext, body in (("gd", _GD_DUP), ("cpp", _CPP_DUP), ("js", _JS_DUP)):
        (src / f"first.{ext}").write_text(body)
        (src / f"second.{ext}").write_text(body.replace("total", "grand_total"))
    (src / "branchy.gd").write_text(_GD_BRANCHY)
    (src / "branchy.cpp").write_text(_CPP_BRANCHY)
    git(target_repo, "add", "-A")
    git(target_repo, "commit", "-q", "-m", "multi language")
    doc = _audit(target_repo)
    assert {lane["status"] for lane in doc["lanes"]} == {"ok"}
    dup = [f for f in doc["findings"] if f["lane"] == "duplication"]
    for ext in (".gd", ".cpp", ".js"):
        pair = {span["path"] for f in dup for span in f["group"]
                if span["path"].startswith("src/multi/") and span["path"].endswith(ext)}
        assert pair == {f"src/multi/first{ext}", f"src/multi/second{ext}"}, ext
    cpx = {(f["path"], f["symbol"]): f for f in doc["findings"] if f["lane"] == "complexity"}
    for key in (("src/multi/branchy.gd", "branchy_gd"), ("src/multi/branchy.cpp", "branchy_cpp")):
        assert key in cpx, sorted(cpx)
        assert cpx[key]["start_line"] >= 1
    by_ext = doc["target"]["coverage"]["by_extension"]
    for ext in (".gd", ".cpp", ".js"):
        assert by_ext[ext]["supported"] is True
        assert all(n > 0 for n in by_ext[ext]["measured"].values()), (ext, by_ext[ext])
