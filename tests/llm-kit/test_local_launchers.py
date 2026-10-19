"""The front-door launch script of llm-kit, run under bash against a stub CLI.

``plugins/llm-kit/scripts/frontdoor.sh`` is carried over from llm-scripting-kit.
Every test runs the REAL script. The llm-kit CLI is a stub sibling ``bin/llm-kit``
that records its argv; nothing starts a real server or reads the real HOME.

What is pinned:

* ``--print-command`` names the sibling ``bin/llm-kit`` by path plus the
  ``frontdoor`` verb, and runs nothing;
* a launch execs ``bin/llm-kit frontdoor`` with every argument, and the CLI's
  exit status is propagated;
* a missing sibling launcher is reported with exit 1;
* the script names no legacy or production interpreter, and holds to bash 3.2
  constructs and ASCII.

Each class names the edit that turns its checks red.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from test_launchers import BASH

REPO = Path(__file__).resolve().parents[2]
NEW = REPO / "plugins" / "llm-kit"

pytestmark = pytest.mark.skipif(BASH is None, reason="needs a bash (Git Bash on Windows)")

_SCRUB_PREFIXES = ("LLM_SCRIPTING_KIT_", "LLM_KIT_", "CLAUDE_PLUGIN_TEST", "CLAUDE_BOOTSTRAP_DATA_ROOT",
                   "BOOTSTRAP_PYTHON")


# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------

def _posix(path: Path) -> str:
    return path.as_posix()


def _norm(text: str) -> str:
    """One spelling for a path argv element.

    Git Bash rewrites a Windows temp path into /tmp/... when it crosses into an msys
    process, so a path under pytest's temp root is reduced to the part from
    ``pytest-of-`` onward, which every spelling shares.
    """
    text = re.sub(r"/+", "/", text.replace("\\", "/"))
    marker = text.find("pytest-of-")
    if marker >= 0:
        return text[marker:]
    return re.sub(r"^/([a-zA-Z])/", lambda m: m.group(1).upper() + ":/", text)


def _write_exec(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    path.chmod(0o755)
    return path


def _base_env(home: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(_SCRUB_PREFIXES)}
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    return env


def _run(script: Path, args: tuple[str, ...], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run([BASH, str(script), *args], text=True, capture_output=True, check=False, env=env,
                          timeout=120, encoding="utf-8", errors="replace")


class Plugin:
    """A throwaway plugin-shaped tree: the real script, a stub sibling CLI."""

    def __init__(self, tmp: Path, source: Path, cli_name: str, name: str = "plugin") -> None:
        self.root = tmp / name
        self.cli_name = cli_name
        self.log = tmp / f"{name}-{cli_name}.calls"
        self.scripts = self.root / "scripts"
        self.scripts.mkdir(parents=True)
        shutil.copy2(source / "scripts" / "frontdoor.sh", self.scripts / "frontdoor.sh")
        (self.root / "bin").mkdir()

    def stub_cli(self, frontdoor_exit: int = 0) -> Path:
        """Stand-in for the sibling CLI: logs ``$*`` and exits by verb."""
        lines = ["#!/bin/sh", f"printf '%s\\n' \"$*\" >> '{_posix(self.log)}'"]
        lines += [
            'case "$*" in',
            f"  *frontdoor*) exit {frontdoor_exit} ;;",
            "esac",
            "exit 0",
        ]
        return _write_exec(self.root / "bin" / self.cli_name, "\n".join(lines) + "\n")

    def calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []

    def script(self, name: str) -> Path:
        return self.scripts / name

    def bin(self, name: str) -> Path:
        return self.root / "bin" / name


@pytest.fixture
def new(tmp_path: Path) -> Plugin:
    plugin = Plugin(tmp_path, NEW, "llm-kit")
    plugin.stub_cli()
    return plugin


# --------------------------------------------------------------------------
# front door
# --------------------------------------------------------------------------

class TestFrontdoor:
    def test_print_command_names_the_sibling_cli_and_the_frontdoor_verb(self, new, tmp_path):
        result = _run(new.script("frontdoor.sh"), ("--print-command", "--check", "--port", "9"), _base_env(tmp_path))
        assert result.returncode == 0
        tokens = result.stdout.split()
        assert _norm(tokens[0]).endswith("/plugin/bin/llm-kit")
        assert tokens[1:] == ["frontdoor", "--check", "--port", "9"]
        assert new.calls() == []

    def test_launch_execs_the_frontdoor_verb_with_every_argument(self, new, tmp_path):
        result = _run(new.script("frontdoor.sh"), ("--check", "--spill-after", "2"), _base_env(tmp_path))
        assert result.returncode == 0
        assert new.calls() == ["frontdoor --check --spill-after 2"]

    def test_the_cli_exit_status_is_propagated(self, new, tmp_path):
        new.stub_cli(frontdoor_exit=7)
        assert _run(new.script("frontdoor.sh"), ("--check",), _base_env(tmp_path)).returncode == 7

    def test_a_missing_launcher_is_reported(self, new, tmp_path):
        new.bin("llm-kit").unlink()
        result = _run(new.script("frontdoor.sh"), ("--check",), _base_env(tmp_path))
        assert result.returncode == 1
        assert "launcher not found" in result.stderr

    def test_it_names_no_legacy_or_production_interpreter(self):
        text = (NEW / "scripts" / "frontdoor.sh").read_text(encoding="utf-8")
        assert "llm_scripting_kit" not in text and "llm-scripting-kit" not in text
        assert ".venv" not in text and "PYTHONPATH" not in text


class TestPortableShell:
    """bash 3.2 (macOS /bin/bash) and zsh-launched bash: no bash 4+ constructs."""

    _FILES = ("scripts/frontdoor.sh",)
    _BASH4 = (r"declare\s+-A", r"\bmapfile\b", r"\breadarray\b", r"\$\{[^}]*(,,|\^\^)[^}]*\}", r"\bread\s+-[a-z]*p",
              r";&", r"&>>", r"\|&", r"\bcoproc\b", r"\bwait\s+-n\b", r"\bdeclare\s+-n\b", r"\blocal\s+-n\b")

    @pytest.mark.parametrize("rel", _FILES)
    def test_no_bash_four_constructs_and_an_env_bash_shebang(self, rel):
        text = (NEW / rel).read_text(encoding="utf-8")
        assert text.startswith("#!/usr/bin/env bash\n"), rel
        for pattern in self._BASH4:
            assert not re.search(pattern, text), (rel, pattern)
        assert "\r" not in text

    @pytest.mark.parametrize("rel", _FILES)
    def test_ascii_only(self, rel):
        (NEW / rel).read_bytes().decode("ascii")
