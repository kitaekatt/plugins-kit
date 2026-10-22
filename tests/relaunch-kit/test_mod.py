"""relaunch-kit hooks module: `claude plugin validate` and `claude plugin test`.

The module's behaviour lives in tests/relaunch-kit/mod/register.test.ts, which
`claude plugin test` runs against the engine's own `$` with a mocked clock. The
test file is not part of the plugin (everything under plugins/<name>/ ships),
so this wrapper copies the plugin and the test into a scratch folder and runs
the engine's runner there.

Skipped when `claude` is not on PATH. The API these tests pin is early access,
so a Claude Code update that moves it fails here first.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PLUGIN = REPO / "plugins" / "relaunch-kit"
TEST_FILE = Path(__file__).resolve().parent / "mod" / "register.test.ts"
REGISTER = PLUGIN / "hooks" / "register.ts"

CLAUDE = shutil.which("claude")
pytestmark = pytest.mark.skipif(CLAUDE is None, reason="claude is not on PATH")

# Every case the plan's U2 row names, plus prompt.edit, as test-name fragments.
REQUIRED_CASES = [
    "binding: the token and dir are read once",
    "env.set denied -> disabled",
    "no launcher env -> no-launcher",
    "answer -> 3 s countdown -> token file -> exit 300 ms later",
    "refusal clears the arm",
    "aborted clears the arm",
    "error clears the arm",
    "prompt.edit during the countdown cancels it and passes the edit through unchanged",
    "prompt.submit during the countdown cancels it",
    "prompt.submit during committing is dropped",
    "reload during the countdown -> idle, no file",
    "reload after the commit -> the file is rewritten",
    "exit rejected -> tombstone",
    "/relaunch-check hop 2 commits resume with no turn.complete",
    "request_restart from a subagent is denied",
]


def _run(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    assert CLAUDE is not None
    return subprocess.run(
        [CLAUDE, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


def test_validate_is_clean() -> None:
    done = _run(["plugin", "validate", str(PLUGIN)])
    out = done.stdout + done.stderr
    assert done.returncode == 0, out
    assert "Validation passed" in out, out
    # The module is seen as hooking the events the design needs.
    for needle in (
        "prompt.edit",
        "prompt.submit",
        "turn.complete",
        "tool.call{tool=mcp__relaunch-kit__request_restart}",
        "command.run{command=relaunch-check}",
    ):
        assert needle in out, f"validate did not report the hook {needle}\n{out}"


def test_module_tests_pass(tmp_path: Path) -> None:
    scratch = tmp_path / "relaunch-kit"
    shutil.copytree(PLUGIN, scratch)
    (scratch / "tests").mkdir()
    shutil.copy(TEST_FILE, scratch / "tests" / TEST_FILE.name)
    done = _run(["plugin", "test", str(scratch)])
    out = done.stdout + done.stderr
    assert done.returncode == 0, out
    failed = re.search(r"^\s*(\d+) fail", out, re.MULTILINE)
    assert failed is not None and failed.group(1) == "0", out
    assert "(fail)" not in out, out
    for case in REQUIRED_CASES:
        assert f"(pass) {case}" in out, f"case did not run and pass: {case}\n{out}"


def test_writer_version_matches_the_manifest() -> None:
    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    source = REGISTER.read_text(encoding="utf-8")
    match = re.search(r"const WRITER = 'relaunch-kit ([^']+)'", source)
    assert match is not None
    assert match.group(1) == manifest["version"]
    assert manifest["name"] == "relaunch-kit"
    assert manifest["dependencies"] == ["bootstrap"]


def test_module_files_are_ascii() -> None:
    for path in (
        REGISTER,
        PLUGIN / "types" / "index.d.ts",
        PLUGIN / "hooks" / "hooks.json",
        PLUGIN / "tsconfig.json",
        PLUGIN / ".claude-plugin" / "plugin.json",
        TEST_FILE,
        Path(__file__),
    ):
        path.read_bytes().decode("ascii")
