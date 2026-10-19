"""Live `claude -p` expansion tests for the recap move (awesome-kit forwarder, presence-kit recap).

These tests spend model-gateway calls and write session files under the user's real
config directory, so they are opt-in: set PRESENCE_KIT_LIVE_TESTS=1 (and have `claude`
logged in). Without the variable, or without a `claude` executable, they skip.

Scenarios (awesome-kit has no recap skill after S2; presence-kit owns it):
- presence-kit and awesome-kit loaded: `/presence-kit:recap` expands presence-kit's skill and
  `/awesome-kit:recap` expands no plugin skill;
- the same set: bare `/recap` stays the Claude Code built-in (no plugin expansion).

Mechanism, confirmed on Claude Code 2.1.302 (Windows):
- A logged-in `claude -p` child expands a slash command, then fires the
  UserPromptExpansion hook. A child with no login stops after UserPromptSubmit with
  "Not logged in" and never reaches expansion, so these tests use the user's normal
  login (no HOME/USERPROFILE/CLAUDE_CONFIG_DIR redirect, no token variable).
- The UserPromptExpansion payload is JSON on stdin with the fields
  hook_event_name, expansion_type ("slash_command"), command_name (for example
  "presence-kit:recap", no leading slash), command_args, command_source ("plugin"),
  prompt, plus the common session_id, transcript_path, cwd, prompt_id,
  permission_mode. The tests assert on `command_name`.
- The recorder hook answers {"decision": "block"}, so the child stops right after
  expansion and spends no model call for an expanded command.
- `--setting-sources project` keeps the user's skills, plugins and hooks out; a
  `--settings` file supplies only the recorder hooks. That also leaves the
  `bootstrap` and `skills-kit` dependencies of the plugins unsatisfied, and the
  loader then refuses the plugin, so two empty stub plugins by those names are
  loaded with --plugin-dir.
- `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1` in the child env stops ~/.claude/CLAUDE.md from loading
  (it loads as "Project" even under `--setting-sources project`). The InstructionsLoaded hook
  is recorded, and the test asserts that no instructions file outside the scratch dir loaded.
- `MSYS_NO_PATHCONV=1` keeps Git Bash from rewriting a bare `/recap` prompt into a Windows
  path; the test also asserts any recorded UserPromptSubmit prompt equals the prompt sent.
- A built-in local command such as bare `/recap` fires no UserPromptSubmit or
  UserPromptExpansion in a `claude -p` child (observed 2026-10-17, 2.1.302). Only the
  bare-/recap test passes builtin_ok=True, which allows zero such records; plugin-command
  runs still require exactly one literal UserPromptSubmit.
- The child's cwd is a pytest tmp dir with `git init`: without a git root, Claude Code
  walks up from the cwd and loads the ancestor ~/.claude/skills as project skills.
- Cleanup: the child writes ~/.claude/projects/<slug of cwd>/ . Teardown removes only
  the project folders that did not exist before the children ran and whose
  transcript_path the recorder captured for this test's own cwd.
"""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PLUGINS = REPO / "plugins"
AWESOME = PLUGINS / "awesome-kit"
PRESENCE = PLUGINS / "presence-kit"

TIMEOUT_SECONDS = 150
BUILTIN_PLUGIN_PREFIX = "cc-plugin-"
# Session-identity variables of the parent session; a child must not inherit them.
STRIPPED_ENV = (
    "CLAUDECODE",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_BRIDGE_SESSION_ID",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SESSION_ATTENDED",
)
CHILD_ENV = {"CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1", "MSYS_NO_PATHCONV": "1"}

RECORDER = '''\
import json
import os
import sys

raw = sys.stdin.read()
with open(os.environ["PRESENCE_KIT_RECORD"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(json.loads(raw)) + "\\n")
if json.loads(raw)["hook_event_name"] == "UserPromptExpansion":
    print(json.dumps({"decision": "block", "reason": "recorded by test"}))
'''

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("PRESENCE_KIT_LIVE_TESTS") != "1",
        reason="live claude -p test; set PRESENCE_KIT_LIVE_TESTS=1 to run",
    ),
    pytest.mark.skipif(shutil.which("claude") is None, reason="claude executable not found"),
]


def claude_projects_dir() -> Path:
    config = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(config) if config else Path.home() / ".claude") / "projects"


def project_slug(cwd: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


class Workspace:
    """Scratch cwd (git repo), recorder, stub plugins, and the cleanup ledger."""

    def __init__(self, root: Path):
        self.root = root
        self.cwd = root / "cwd"
        self.stubs = root / "stubs"
        self.cwd.mkdir()
        subprocess.run(
            ["git", "init", "-q", str(self.cwd)], check=True, stdin=subprocess.DEVNULL,
            capture_output=True, timeout=60,
        )
        self.recorder = root / "recorder.py"
        self.recorder.write_text(RECORDER, encoding="utf-8")
        command = f'"{Path(sys.executable).as_posix()}" "{self.recorder.as_posix()}"'
        hook = [{"hooks": [{"type": "command", "command": command}]}]
        self.settings = root / "settings.json"
        self.settings.write_text(
            json.dumps({"hooks": {event: hook for event in (
                "SessionStart", "UserPromptSubmit", "UserPromptExpansion", "InstructionsLoaded",
            )}}),
            encoding="utf-8",
        )
        self.stub_dirs = []
        for name in ("bootstrap", "skills-kit"):
            manifest = self.stubs / name / ".claude-plugin" / "plugin.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(
                json.dumps({"name": name, "version": "0.0.0", "description": "test stub"}),
                encoding="utf-8",
            )
            self.stub_dirs.append(self.stubs / name)
        self.projects = claude_projects_dir()
        self.before = set(os.listdir(self.projects)) if self.projects.is_dir() else set()
        self.transcript_dirs = set()
        self.counter = 0

    def child_env(self, record: Path) -> dict:
        env = {k: v for k, v in os.environ.items() if k not in STRIPPED_ENV}
        env.update(CHILD_ENV)
        env["PRESENCE_KIT_RECORD"] = str(record)
        return env

    def run(self, prompt: str, plugin_dirs: list, builtin_ok: bool = False) -> dict:
        """Run one `claude -p`; return {'init': <init message>, 'records': [<hook payloads>]}.

        builtin_ok=True allows zero hook records, for a built-in local command that
        short-circuits before prompt submission.
        """
        self.counter += 1
        record = self.root / f"record-{self.counter}.jsonl"
        cmd = [
            "claude", "-p", prompt,
            "--model", "haiku",
            "--setting-sources", "project",
            "--settings", str(self.settings),
            "--output-format", "stream-json", "--verbose",
            "--max-turns", "1", "--max-budget-usd", "0.10",
        ]
        for plugin_dir in [*plugin_dirs, *self.stub_dirs]:
            cmd += ["--plugin-dir", str(plugin_dir)]
        result = subprocess.run(
            cmd, env=self.child_env(record), cwd=str(self.cwd), stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=TIMEOUT_SECONDS,
        )
        records = []
        if record.exists():
            records = [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()]
        for payload in records:
            self.transcript_dirs.add((Path(payload["transcript_path"]).parent, payload["cwd"]))
        init = None
        for line in result.stdout.splitlines():
            message = json.loads(line)
            if message.get("type") == "system" and message.get("subtype") == "init":
                init = message
        assert init is not None, f"no init message; rc={result.returncode} stderr={result.stderr[:500]}"
        submitted = [r for r in records if r["hook_event_name"] == "UserPromptSubmit"]
        if submitted or not builtin_ok:
            assert [r["prompt"] for r in submitted] == [prompt], f"prompt did not arrive literally: {submitted}"
        assert records or builtin_ok, f"recorder saw no hook payload; rc={result.returncode} stderr={result.stderr[:500]}"
        return {"init": init, "records": records}

    def cleanup(self) -> list:
        """Remove only project folders created for this cwd; return what was removed."""
        removed = []
        for folder, child_cwd in sorted(self.transcript_dirs):
            is_ours = (
                Path(child_cwd).resolve() == self.cwd.resolve()
                and folder.name == project_slug(child_cwd)
                and folder.parent == self.projects
                and folder.name not in self.before
                and folder.is_dir()
            )
            if is_ours:
                shutil.rmtree(folder)
                removed.append(folder.name)
        return removed


@pytest.fixture(scope="module")
def workspace(tmp_path_factory):
    ws = Workspace(tmp_path_factory.mktemp("recap-expansion"))
    try:
        yield ws
    finally:
        removed = ws.cleanup()
        print(f"recap-expansion cleanup removed project folders: {removed}")


def expansions(outcome: dict) -> list:
    return [r for r in outcome["records"] if r["hook_event_name"] == "UserPromptExpansion"]


def is_inside(path: str, root: Path) -> bool:
    return Path(path).resolve().is_relative_to(root.resolve())


def assert_only_test_plugins_loaded(outcome: dict, expected: set, scratch_root: Path) -> None:
    """No user skills or plugins may be present; the loaded plugins are exactly the expected ones."""
    init = outcome["init"]
    loaded = {p["name"] for p in init["plugins"] if not p["name"].startswith(BUILTIN_PLUGIN_PREFIX)}
    assert loaded == expected | {"bootstrap", "skills-kit"}, f"unexpected plugins: {sorted(loaded)}"
    user_skills = sorted(set(init["skills"]) & {p.name for p in (Path.home() / ".claude" / "skills").glob("*")})
    assert not user_skills, f"user skills leaked into the child: {user_skills}"
    outside = [
        r["file_path"] for r in outcome["records"]
        if r["hook_event_name"] == "InstructionsLoaded" and not is_inside(r["file_path"], scratch_root)
    ]
    assert not outside, f"instructions files from outside the scratch dir loaded: {outside}"
    # SessionStart fired from the --settings recorder only; the user's own hooks are absent.
    assert any(r["hook_event_name"] == "SessionStart" for r in outcome["records"])


def assert_expanded(outcome: dict, command_name: str) -> None:
    found = expansions(outcome)
    assert [r["command_name"] for r in found] == [command_name], found
    assert found[0]["command_source"] == "plugin"
    assert found[0]["expansion_type"] == "slash_command"


def test_presence_kit_recap_expands_and_awesome_kit_recap_does_not(workspace):
    dirs = [PRESENCE, AWESOME]
    moved = workspace.run("/presence-kit:recap", dirs)
    assert_only_test_plugins_loaded(moved, {"awesome-kit", "presence-kit"}, workspace.root)
    assert_expanded(moved, "presence-kit:recap")

    gone = workspace.run("/awesome-kit:recap", dirs)
    assert_only_test_plugins_loaded(gone, {"awesome-kit", "presence-kit"}, workspace.root)
    assert [r for r in expansions(gone) if r["command_source"] == "plugin"] == [], expansions(gone)


def test_bare_recap_is_not_a_plugin_skill(workspace):
    # A built-in local command may fire no UserPromptSubmit/UserPromptExpansion at all.
    outcome = workspace.run("/recap", [PRESENCE, AWESOME], builtin_ok=True)
    assert outcome["init"] is not None
    assert_only_test_plugins_loaded(outcome, {"awesome-kit", "presence-kit"}, workspace.root)
    plugin_expansions = [r for r in expansions(outcome) if r["command_source"] == "plugin"]
    assert plugin_expansions == [], plugin_expansions
    assert not [r for r in expansions(outcome) if r["command_name"].endswith(":recap")]
