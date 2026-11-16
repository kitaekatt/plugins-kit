"""A superseded bootstrap copy hands its SessionStart pass to the newest one.

Claude Code binds ``${CLAUDE_PLUGIN_ROOT}`` when a session starts, so a session
that was running before a bootstrap update keeps firing the OLD copy's hooks for
the rest of its life -- on every SessionStart it sees, including the
``SessionStart:compact`` a long-running subagent's compaction fires. Every copy
shares one data dir and one bootstrap venv, so an old copy's full pass and the
installed copy's passes undo each other: each finds the venv's editable install
pointing at the other version and re-runs ``uv sync`` (observed 2026-11-16:
0.167.2 and 0.169.0 alternating, ~50 s each, and the ``bootstrap`` lever
re-pointed at 0.167.2).

The fix: session-bootstrap.sh in a cache-layout copy looks for a strictly newer
complete copy beside it and ``exec``s that copy's hook with the same arguments
and stdin. These tests run the decision helper directly (it is pure) and run
the real hook only in a cache-layout tree under CLAUDE_PLUGIN_TEST, so a copy
that does NOT hand off stands down instead of provisioning this machine.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = REPO_ROOT / "plugins" / "bootstrap"
SESSION_HOOK = BOOTSTRAP / "hooks" / "sessionstart" / "session-bootstrap.sh"
HELPER = BOOTSTRAP / "hooks" / "sessionstart" / "newest-copy.sh"
# macOS /bin/bash is 3.2, the oldest bash the hook must survive.
BASH = "/bin/bash" if Path("/bin/bash").exists() else (shutil.which("bash") or "bash")

STUB_HOOK = """#!/usr/bin/env bash
cat > "$HANDOFF_MARK"
printf 'args:%s\\n' "$*" >> "$HANDOFF_MARK"
printf '{"continue": true, "suppressOutput": true}\\n'
"""


def _copy(cache: Path, version: str, *, plugin_version: str | None = None,
          engine: bool = True, real_hook: bool = False) -> Path:
    """Lay out one bootstrap copy at <cache>/<version>."""
    root = cache / version
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps(
        {"name": "bootstrap", "version": plugin_version or version}))
    hooks = root / "hooks"
    (hooks / "sessionstart").mkdir(parents=True)
    shutil.copy(BOOTSTRAP / "hooks" / "marketplace-name.sh", hooks / "marketplace-name.sh")
    hook = hooks / "sessionstart" / "session-bootstrap.sh"
    if real_hook:
        shutil.copy(SESSION_HOOK, hook)
        if HELPER.exists():
            shutil.copy(HELPER, hooks / "sessionstart" / "newest-copy.sh")
    else:
        hook.write_text(STUB_HOOK)
    if engine:
        (root / "engine").mkdir()
        (root / "engine" / "bootstrap_engine.py").write_text("")
    return root


@pytest.fixture
def cache(tmp_path: Path) -> Path:
    c = tmp_path / ".claude" / "plugins" / "cache" / "plugins-kit" / "bootstrap"
    c.mkdir(parents=True)
    return c


def _newer(root: Path) -> str:
    res = subprocess.run(
        [BASH, "-c", '. "$1" && bootstrap_newer_copy "$2"', "_", str(HELPER), str(root)],
        capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    return res.stdout.strip()


def _run_old_hook(old: Path, tmp_path: Path, mark: Path, stdin: str, *args: str):
    env = dict(os.environ)
    env.update({
        # Safety net: a cache copy that does not hand off stands down here
        # instead of running a real pass against this machine.
        "CLAUDE_PLUGIN_TEST": "1",
        "CLAUDE_BOOTSTRAP_DATA_ROOT": str(tmp_path / "data"),
        "HANDOFF_MARK": str(mark),
    })
    return subprocess.run(
        [BASH, str(old / "hooks" / "sessionstart" / "session-bootstrap.sh"), *args],
        input=stdin, capture_output=True, text=True, timeout=60, env=env)


class TestHookHandsOff:
    def test_superseded_copy_hands_off_to_newest_installed(self, cache, tmp_path):
        old = _copy(cache, "0.167.2", real_hook=True)
        _copy(cache, "0.168.1")
        _copy(cache, "0.169.0")
        mark = tmp_path / "handoff.txt"
        hook_json = '{"session_id": "abc", "source": "compact"}'

        res = _run_old_hook(old, tmp_path, mark, hook_json)

        assert res.returncode == 0, res.stderr
        assert mark.exists(), (
            "the 0.167.2 hook ran its own pass instead of handing off to 0.169.0: "
            f"{res.stdout[:300]} {res.stderr[:300]}")
        got = mark.read_text()
        # The newest copy received the hook's stdin untouched...
        assert got.startswith(hook_json)
        # ...and the hand-off is logged, naming both versions.
        log = (tmp_path / "data" / "plugins-kit" / "bootstrap" / "bootstrap.log").read_text()
        assert "0.167.2" in log and "0.169.0" in log and "hand" in log
        # Hook stdout stays the newest copy's JSON only.
        assert res.stdout.strip() == '{"continue": true, "suppressOutput": true}'

    def test_console_args_pass_through_and_nothing_is_logged(self, cache, tmp_path):
        old = _copy(cache, "0.167.2", real_hook=True)
        _copy(cache, "0.169.0")
        mark = tmp_path / "handoff.txt"

        res = _run_old_hook(old, tmp_path, mark, "", "--console", "--verbose")

        assert res.returncode == 0, res.stderr
        assert "args:--console --verbose" in mark.read_text()
        # Console runs write no bootstrap state; the hand-off is printed instead.
        assert not (tmp_path / "data" / "plugins-kit" / "bootstrap" / "bootstrap.log").exists()
        assert "0.169.0" in res.stdout

    def test_newest_copy_runs_itself(self, cache, tmp_path):
        _copy(cache, "0.167.2")
        newest = _copy(cache, "0.169.0", real_hook=True)
        mark = tmp_path / "handoff.txt"

        res = _run_old_hook(newest, tmp_path, mark, "")

        assert res.returncode == 0, res.stderr
        assert not mark.exists(), "the newest copy handed off to an older one"


class TestNewerCopyDecision:
    def test_picks_the_numerically_highest(self, cache):
        old = _copy(cache, "0.98.1")
        _copy(cache, "0.104.0")
        _copy(cache, "0.99.9")
        assert _newer(old) == str(cache / "0.104.0")

    def test_nothing_when_newest(self, cache):
        _copy(cache, "0.168.1")
        newest = _copy(cache, "0.169.0")
        assert _newer(newest) == ""

    def test_skips_an_incomplete_copy(self, cache):
        old = _copy(cache, "0.168.1")
        _copy(cache, "0.169.0", engine=False)
        assert _newer(old) == ""

    def test_skips_a_dir_whose_manifest_disagrees_with_its_name(self, cache):
        old = _copy(cache, "0.168.1")
        _copy(cache, "0.169.0", plugin_version="0.168.0")
        assert _newer(old) == ""

    def test_skips_non_version_dirs(self, cache):
        old = _copy(cache, "0.168.1")
        _copy(cache, "0.170.0-rc1")
        assert _newer(old) == ""

    def test_dev_layout_never_hands_off(self, tmp_path):
        # <checkout>/plugins/bootstrap: not a version dir, so no sibling scan.
        plugins = tmp_path / "checkout" / "plugins"
        dev = _copy(plugins, "bootstrap", plugin_version="0.169.0")
        _copy(plugins, "9.9.9")
        assert _newer(dev) == ""
