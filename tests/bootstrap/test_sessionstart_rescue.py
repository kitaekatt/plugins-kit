"""Tests for the SessionStart-missed rescue in bootstrap-display.sh.

On a fresh machine Claude Code can still be syncing the marketplace when
SessionStart fires, so bootstrap's SessionStart hook isn't registered yet and
the provisioning pass never runs that session (observed live 2026-07-16: a
deleted plugins dir re-synced at startup, /plugin showed everything installed,
but no bootstrap pass ever ran). The UserPromptSubmit display hook re-fires
every prompt, so it detects the miss and launches session-bootstrap.sh itself.

Detection signal: session-bootstrap.sh touches sessions/<session_id> at ENTRY.
A missing marker for the prompt's session_id means no SessionStart pass was
invoked this session. Deliberately NOT the Layer-1 last_session_id stamp: that
is a single global slot (two concurrent sessions overwrite it -> perpetual
rescue ping-pong) and bootstrap-reset-cooldown deletes it (a reset must re-arm
the NEXT SessionStart, not fire a mid-session pass).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DISPLAY_HOOK = (
    REPO_ROOT / "plugins" / "bootstrap" / "hooks" / "userpromptsubmit" / "bootstrap-display.sh"
)
SESSION_BOOTSTRAP = (
    REPO_ROOT / "plugins" / "bootstrap" / "hooks" / "sessionstart" / "session-bootstrap.sh"
)

# The session_id extraction pipeline must stay byte-identical in both scripts:
# session-bootstrap.sh stamps the marker from the value ITS pipeline extracts,
# and the rescue compares against the value its own pipeline extracts. Drift =
# the rescue silently fires on every prompt forever.
SID_EXTRACTION = (
    "grep -o '\"session_id\"[[:space:]]*:[[:space:]]*\"[^\"]*\"' "
    "| grep -o '\"[^\"]*\"$' | tr -d '\"'"
)


def _find_bash() -> str | None:
    """Find a POSIX-compatible bash. On Windows, prefer Git Bash over WSL bash
    (which lives at C:\\Windows\\System32\\bash.exe and can't access this VHDX)."""
    candidates = []
    if os.name == "nt":
        candidates.extend([
            r"C:\Program Files\Git\usr\bin\bash.exe",
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files (x86)\Git\usr\bin\bash.exe",
        ])
    found = shutil.which("bash")
    if found:
        candidates.append(found)
    for c in candidates:
        if c and Path(c).exists() and "WindowsApps" not in c and "System32" not in c:
            return c
    return None


BASH = _find_bash()
needs_bash = pytest.mark.skipif(BASH is None, reason="bash not available on this platform")

SID = "sess-abc-123"
HOOK_JSON = json.dumps({"session_id": SID, "hook_event_name": "UserPromptSubmit", "prompt": "hi"})


class TestRescueContract:
    """Static checks -- cheap and platform-independent."""

    def test_detection_is_session_marker_not_guard_stamp(self) -> None:
        text = DISPLAY_HOOK.read_text()
        assert "sessions/" in text.replace('"', ""), "rescue must key on per-session markers"
        # The Layer-1 stamp is a single global slot (multi-session ping-pong) and
        # is deleted by bootstrap-reset-cooldown (mid-session pass on next prompt).
        assert 'cat "$_GUARD_FILE"' not in text, (
            "rescue must not compare against the global last_session_id stamp"
        )

    def test_sid_extraction_identical_in_both_scripts(self) -> None:
        assert SID_EXTRACTION in DISPLAY_HOOK.read_text(), "display-hook extraction drifted"
        assert SID_EXTRACTION in SESSION_BOOTSTRAP.read_text(), "session-bootstrap extraction drifted"

    def test_session_bootstrap_writes_marker_at_entry(self) -> None:
        text = SESSION_BOOTSTRAP.read_text()
        marker_pos = text.find('"$PLUGIN_DATA/sessions/$_SID_SAFE"')
        assert marker_pos != -1, "session-bootstrap must touch sessions/<sid>"
        # Entry-time, i.e. BEFORE the Layer-1 gate: a fast first prompt must see
        # the marker within milliseconds even when the gates later skip the pass.
        layer1_pos = text.find('cat "$_GUARD_FILE"')
        assert layer1_pos != -1
        assert marker_pos < layer1_pos, "marker must be written before the skip gates"

    def test_launch_is_detached_locked_and_feeds_hook_input(self) -> None:
        text = DISPLAY_HOOK.read_text()
        # Detached subshell: the launch must never delay the prompt.
        assert ") >/dev/null 2>&1 &" in text, "rescue launch must be backgrounded"
        # Atomic one-launch-per-session lock (noclobber create).
        assert '( set -C; : > "$_RESCUE_LOCK" ) 2>/dev/null || exit 0' in text
        # The hook JSON is piped in so session-bootstrap.sh writes this session's
        # marker and its gates work normally.
        assert 'printf \'%s\' "$HOOK_INPUT" | bash "$_SB"' in text

    def test_stand_down_paths_run_harvest(self) -> None:
        # The rescue suppresses the foreground harvest; a stand-down must run it
        # so a single-prompt session (claude -p) still converges a pending update.
        text = DISPLAY_HOOK.read_text()
        assert text.count("_run_harvest") >= 4, (
            "harvest must run in both stand-down paths and the no-rescue foreground path"
        )
        assert "-newermt '-120 seconds'" in text, (
            "rescue must stand down when a pass stamped a cooldown moments ago "
            "(covers the no-stdin SessionStart pass that writes no marker)"
        )

    def test_stdin_capture_is_tty_guarded_and_bounded(self) -> None:
        text = DISPLAY_HOOK.read_text()
        assert "[ ! -t 0 ]" in text, "manual terminal invocation must not block"
        assert "read -r -t 10" in text, "stdin read must be time-bounded, not a bare cat"


@needs_bash
class TestRescueBehavior:
    """Behavioral tests: run bootstrap-display.sh from a scaffolded plugin root
    with a stub session-bootstrap.sh that records its invocation + stdin."""

    def _scaffold(self, tmp_path: Path) -> dict:
        plugin_root = tmp_path / "mkt" / "bootstrap" / "0.0.0"
        ups_dir = plugin_root / "hooks" / "userpromptsubmit"
        ss_dir = plugin_root / "hooks" / "sessionstart"
        ups_dir.mkdir(parents=True)
        ss_dir.mkdir(parents=True)
        display = ups_dir / "bootstrap-display.sh"
        display.write_text(DISPLAY_HOOK.read_text(), encoding="utf-8")

        record = tmp_path / "sb_invoked_stdin"
        stub = ss_dir / "session-bootstrap.sh"
        stub.write_text(
            "#!/usr/bin/env bash\n"
            'IN="$(cat)"\n'
            f'printf \'%s\' "$IN" > "{record.as_posix()}"\n',
            encoding="utf-8",
        )

        home = tmp_path / "home"
        home.mkdir()
        data_dir = home / ".claude" / "plugins" / "data" / "mkt" / "bootstrap"
        return {
            "display": display,
            "record": record,
            "home": home,
            "data_dir": data_dir,
            "sessions": data_dir / "sessions",
            "marker": data_dir / "sessions" / SID,
            "lock": data_dir / "sessions" / f"rescue_launched.{SID}",
        }

    def _run(self, s: dict, stdin: str = HOOK_JSON, delay: str = "0") -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["HOME"] = s["home"].as_posix()
        env["BOOTSTRAP_RESCUE_DELAY"] = delay
        return subprocess.run(
            [BASH, str(s["display"])],
            input=stdin, capture_output=True, text=True, env=env, timeout=30,
        )

    def _wait_for(self, path: Path, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists():
                return True
            time.sleep(0.1)
        return False

    def test_launches_when_no_session_marker(self, tmp_path: Path) -> None:
        # Fresh machine: no data dir at all -> rescue fires and the launched
        # session-bootstrap.sh receives the original hook JSON on stdin.
        s = self._scaffold(tmp_path)
        result = self._run(s)
        assert result.returncode == 0, result.stderr
        assert self._wait_for(s["record"]), "rescue should have launched session-bootstrap.sh"
        assert SID in s["record"].read_text(encoding="utf-8")
        assert s["lock"].exists(), "launch must leave the one-launch-per-session lock"

    def test_logs_the_launch(self, tmp_path: Path) -> None:
        s = self._scaffold(tmp_path)
        self._run(s)
        assert self._wait_for(s["record"])
        log = s["data_dir"] / "bootstrap.log"
        assert log.exists(), "rescue must log its outcome (every action logs)"
        assert "sessionstart-rescue" in log.read_text(encoding="utf-8")

    def test_skips_when_own_marker_exists(self, tmp_path: Path) -> None:
        # SessionStart ran (or gate-skipped) normally this session -> no rescue.
        s = self._scaffold(tmp_path)
        s["sessions"].mkdir(parents=True)
        s["marker"].write_text("", encoding="utf-8")
        result = self._run(s)
        assert result.returncode == 0, result.stderr
        time.sleep(1.0)
        assert not s["record"].exists(), "own session marker must suppress the rescue"

    def test_other_sessions_markers_do_not_suppress(self, tmp_path: Path) -> None:
        # Pins the multi-session fix: a CONCURRENT session's marker (or the old
        # global-stamp semantics) must not mask a genuinely missed SessionStart.
        s = self._scaffold(tmp_path)
        s["sessions"].mkdir(parents=True)
        (s["sessions"] / "some-other-session").write_text("", encoding="utf-8")
        self._run(s)
        assert self._wait_for(s["record"]), "another session's marker must not suppress the rescue"

    def test_rescue_lock_caps_launches_at_one(self, tmp_path: Path) -> None:
        s = self._scaffold(tmp_path)
        s["sessions"].mkdir(parents=True)
        s["lock"].write_text("", encoding="utf-8")
        result = self._run(s)
        assert result.returncode == 0, result.stderr
        time.sleep(1.0)
        assert not s["record"].exists(), "an existing rescue lock must block a second launch"

    def test_fresh_cooldown_stamp_stands_down(self, tmp_path: Path) -> None:
        # A cooldown stamped moments ago means a pass is running or just ran
        # (possibly a no-stdin SessionStart that wrote no marker): don't race it.
        s = self._scaffold(tmp_path)
        cooldowns = s["data_dir"] / "cooldowns"
        cooldowns.mkdir(parents=True)
        (cooldowns / "last_run_epoch.abc").write_text(str(int(time.time())), encoding="utf-8")
        result = self._run(s)
        assert result.returncode == 0, result.stderr
        time.sleep(1.0)
        assert not s["record"].exists(), "a fresh cooldown stamp must stand the rescue down"

    def test_stale_cooldown_stamp_does_not_stand_down(self, tmp_path: Path) -> None:
        s = self._scaffold(tmp_path)
        cooldowns = s["data_dir"] / "cooldowns"
        cooldowns.mkdir(parents=True)
        stale = cooldowns / "last_run_epoch.abc"
        stale.write_text("0", encoding="utf-8")
        old = time.time() - 600
        os.utime(stale, (old, old))
        self._run(s)
        assert self._wait_for(s["record"]), "a stale cooldown stamp must not suppress the rescue"

    def test_no_stdin_no_rescue(self, tmp_path: Path) -> None:
        s = self._scaffold(tmp_path)
        result = self._run(s, stdin="")
        assert result.returncode == 0, result.stderr
        time.sleep(1.0)
        assert not s["record"].exists()

    def test_no_session_id_no_rescue(self, tmp_path: Path) -> None:
        s = self._scaffold(tmp_path)
        result = self._run(s, stdin='{"hook_event_name": "UserPromptSubmit"}')
        assert result.returncode == 0, result.stderr
        time.sleep(1.0)
        assert not s["record"].exists()

    def test_inflight_sessionstart_wins_the_race(self, tmp_path: Path) -> None:
        # Fast-start (claude -p) race: SessionStart fired but hadn't written the
        # marker yet when the prompt arrived. It writes it during the rescue's
        # re-check delay, so the detached rescue stands down instead of
        # double-running the pass. Generous 5s delay: the marker lands well
        # inside the window even on a slow bash spawn (the write happens right
        # after subprocess.run returns, typically <1s in).
        s = self._scaffold(tmp_path)
        result = self._run(s, delay="5")
        assert result.returncode == 0, result.stderr
        s["sessions"].mkdir(parents=True, exist_ok=True)
        s["marker"].write_text("", encoding="utf-8")
        time.sleep(8.0)
        assert not s["record"].exists(), "rescue must stand down when the marker catches up"

    def test_display_relay_unaffected(self, tmp_path: Path) -> None:
        # The original job of the hook -- relay bootstrap_display.pending once --
        # must be untouched by the rescue logic.
        s = self._scaffold(tmp_path)
        s["sessions"].mkdir(parents=True)
        s["marker"].write_text("", encoding="utf-8")  # no rescue
        pending = s["data_dir"] / "bootstrap_display.pending"
        pending.write_text('{"systemMessage": "hello"}', encoding="utf-8")
        result = self._run(s)
        assert result.returncode == 0, result.stderr
        assert '{"systemMessage": "hello"}' in result.stdout
        assert not pending.exists()
        assert (s["data_dir"] / "bootstrap_display.displayed").exists()


@needs_bash
class TestSessionBootstrapMarker:
    """session-bootstrap.sh must record the per-session marker at entry, even on
    a gate-skipped invocation. Exercised via the safe skip path (fresh cooldown,
    same session id) exactly like TestCooldownGateBehavior does."""

    def test_gate_skipped_invocation_still_writes_marker(self, tmp_path: Path) -> None:
        fake_home = tmp_path / "home"
        data_dir = fake_home / ".claude" / "plugins" / "data" / "plugins-kit" / "bootstrap"
        cooldowns = data_dir / "cooldowns"
        cooldowns.mkdir(parents=True)
        # Same-session Layer-1 skip: guard stamp already holds this session id.
        (data_dir / "last_session_id").write_text(SID, encoding="utf-8")

        env = os.environ.copy()
        env["HOME"] = fake_home.as_posix()
        result = subprocess.run(
            [BASH, str(SESSION_BOOTSTRAP)],
            input=HOOK_JSON, capture_output=True, text=True, env=env, timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert (data_dir / "sessions" / SID).exists(), (
            "entry-time marker must be written even when the gates skip the pass"
        )
