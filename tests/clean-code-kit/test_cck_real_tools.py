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
