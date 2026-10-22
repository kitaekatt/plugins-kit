"""Shared setup for the relaunch-kit launcher tests.

Puts plugins/relaunch-kit/lib on sys.path (the plugin directory name has a
dash, so it cannot be imported as a package) and provides a fake `claude`.
"""

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN = _REPO_ROOT / "plugins" / "relaunch-kit"
LIB = PLUGIN / "lib"
BIN = PLUGIN / "bin"
FAKE_CLAUDE_PY = Path(__file__).resolve().parent / "fake_claude.py"

if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

# Variables from a surrounding Claude Code or claudx session that would leak into a launch.
_SCRUBBED = (
    "RELAUNCH_KIT_TOKEN", "RELAUNCH_KIT_DIR", "RELAUNCH_KIT_CLAUDE", "RELAUNCH_KIT_SHIM",
    "CLAUDE_PLUGIN_TEST", "CLAUDE_PLUGIN_TEST_MARKETPLACE", "CLAUDE_PLUGIN_TEST_DIRS",
    "CLAUDE_BOOTSTRAP_DATA_ROOT", "BOOTSTRAP_PYTHON", "BOOTSTRAP_PROJECT_PYTHON",
)

posix_only = pytest.mark.skipif(os.name == "nt", reason="needs a POSIX shell and executable stubs")


class FakeClaude:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.state = root / "state"
        self.state.mkdir()
        self.data = root / "data"
        self.data.mkdir()
        self.exe = root / "claude"
        self.exe.write_text(
            "#!/bin/sh\nexec %s %s \"$@\"\n"
            % (shlex.quote(sys.executable), shlex.quote(str(FAKE_CLAUDE_PY))),
            encoding="utf-8",
        )
        self.exe.chmod(0o755)
        self.script_path = root / "script.json"
        self.script([])

    def script(self, steps) -> None:
        self.script_path.write_text(json.dumps(steps), encoding="utf-8")

    def env(self) -> dict:
        env = {k: v for k, v in os.environ.items() if k not in _SCRUBBED}
        env.update(
            RELAUNCH_KIT_CLAUDE=str(self.exe),
            CLAUDE_BOOTSTRAP_DATA_ROOT=str(self.data),
            FAKE_CLAUDE_STATE=str(self.state),
            FAKE_CLAUDE_SCRIPT=str(self.script_path),
            HOME=str(self.root),
            USERPROFILE=str(self.root),
        )
        return env

    def calls(self) -> list:
        path = self.state / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def rendezvous(self) -> Path:
        return self.data / "plugins-kit" / "relaunch-kit" / "relaunch"


@pytest.fixture
def fake_claude(tmp_path) -> FakeClaude:
    return FakeClaude(tmp_path)


@pytest.fixture
def clean_env(monkeypatch):
    for name in _SCRUBBED:
        monkeypatch.delenv(name, raising=False)
