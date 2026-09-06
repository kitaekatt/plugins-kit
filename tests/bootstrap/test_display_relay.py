"""Tests for the age-stamped display relay (bootstrap_lib/display_relay.py).

The behaviour under test: a bootstrap message delivered to a session cannot be
mistaken for a verdict on the current state of the machine. A freshly produced
message reads as current; an aged one is visibly marked on BOTH surfaces.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from bootstrap_lib import display_relay

REPO_ROOT = Path(__file__).resolve().parents[2]
DISPLAY_HOOK = (
    REPO_ROOT / "plugins" / "bootstrap" / "hooks" / "userpromptsubmit" / "bootstrap-display.sh"
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


def _write_pending(data_dir, payload, age_seconds=0):
    path = os.path.join(data_dir, display_relay.PENDING_NAME)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    if age_seconds:
        stamp = os.path.getmtime(path) - age_seconds
        os.utime(path, (stamp, stamp))
    return path


def _failure_payload():
    return {
        "continue": True,
        "suppressOutput": False,
        "systemMessage": "bootstrap: Setup issues found",
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "bootstrap -> Setup issues found. Fix in order:\n1. install jq",
        },
    }


# --- format_age -------------------------------------------------------------

@pytest.mark.parametrize("seconds,expected", [
    (0, "just now"),
    (59, "just now"),
    (60, "1m ago"),
    (1800, "30m ago"),
    (3600, "1h ago"),
    (86400, "1d ago"),
])
def test_format_age_spellings(seconds, expected):
    assert display_relay.format_age(seconds) == expected


def test_format_age_refuses_to_invent_a_negative_age():
    """Clock skew must not be rendered as a plausible age."""
    out = display_relay.format_age(-500)
    assert "unknown" in out
    assert "ago" not in out


# --- the fresh case ---------------------------------------------------------

def test_fresh_message_reads_as_current(tmp_path, capsys):
    _write_pending(str(tmp_path), _failure_payload(), age_seconds=0)

    assert display_relay.relay(str(tmp_path)) == 0

    out = json.loads(capsys.readouterr().out)
    assert "just now" in out["systemMessage"]
    assert "may not reflect" not in out["systemMessage"]
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "finished just now" in ctx
    assert "Setup issues found. Fix in order:" in ctx


def test_stamp_is_emitted_even_when_fresh(tmp_path, capsys):
    """The marker is unconditional on purpose.

    A marker that only appeared when stale would make its own ABSENCE carry
    meaning -- the silent-report shape this relay exists to remove.
    """
    _write_pending(str(tmp_path), _failure_payload(), age_seconds=0)
    display_relay.relay(str(tmp_path))
    out = json.loads(capsys.readouterr().out)
    assert "bootstrap pass finished" in out["systemMessage"]
    assert "TIMING:" in out["hookSpecificOutput"]["additionalContext"]


# --- the aged case ----------------------------------------------------------

def test_aged_message_is_marked_on_both_surfaces(tmp_path, capsys):
    """The observed bug: a pass 30 minutes old delivered as a current verdict."""
    _write_pending(str(tmp_path), _failure_payload(), age_seconds=1800)

    assert display_relay.relay(str(tmp_path)) == 0

    out = json.loads(capsys.readouterr().out)
    # User-facing surface.
    assert "30m ago" in out["systemMessage"]
    assert "may not reflect the machine's current state" in out["systemMessage"]
    # Claude-facing surface -- systemMessage never reaches Claude, so stamping
    # only one surface would leave the other reader mistaken.
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "30m ago" in ctx
    assert "AS OF THAT MOMENT" in ctx
    assert "re-check before acting on it" in ctx


def test_aged_message_still_carries_the_original_content(tmp_path, capsys):
    _write_pending(str(tmp_path), _failure_payload(), age_seconds=7200)
    display_relay.relay(str(tmp_path))
    out = json.loads(capsys.readouterr().out)
    assert out["continue"] is True
    assert out["suppressOutput"] is False
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "1. install jq" in out["hookSpecificOutput"]["additionalContext"]
    assert "2h ago" in out["systemMessage"]


def test_no_extra_top_level_keys_are_added(tmp_path, capsys):
    """The pending payload is hook JSON that Claude Code schema-validates.

    The age rides inside the existing message strings for that reason; a new
    top-level field would be a change to a validated contract.
    """
    _write_pending(str(tmp_path), _failure_payload(), age_seconds=600)
    display_relay.relay(str(tmp_path))
    out = json.loads(capsys.readouterr().out)
    assert set(out) == {
        "continue", "suppressOutput", "systemMessage", "hookSpecificOutput"}
    assert set(out["hookSpecificOutput"]) == {
        "hookEventName", "additionalContext"}


# --- consumption / handshake ------------------------------------------------

def test_pending_is_consumed_exactly_once(tmp_path, capsys):
    pending = _write_pending(str(tmp_path), _failure_payload())

    assert display_relay.relay(str(tmp_path)) == 0
    capsys.readouterr()

    assert not os.path.exists(pending)
    assert not os.path.exists(os.path.join(tmp_path, display_relay.DISPLAYED_NAME))
    # Second delivery attempt: nothing left to show, and the caller is told to
    # fall back (its own `[ -f "$PENDING" ]` guard has already exited by then).
    assert display_relay.relay(str(tmp_path)) == 1
    assert capsys.readouterr().out == ""


def test_two_relays_deliver_one_pending_message(tmp_path, capsys):
    _write_pending(str(tmp_path), _failure_payload())

    assert display_relay.relay(str(tmp_path)) == 0
    first = capsys.readouterr().out
    assert display_relay.relay(str(tmp_path)) == 1
    second = capsys.readouterr().out

    assert first
    assert second == ""


def test_producer_replacement_after_claim_stays_pending(tmp_path, monkeypatch, capsys):
    pending = os.path.join(str(tmp_path), display_relay.PENDING_NAME)
    _write_pending(str(tmp_path), _failure_payload())
    replacement = _failure_payload()
    replacement["systemMessage"] = "replacement"
    original_replace = display_relay.os.replace
    claimed = []

    def replace_and_produce(source, destination):
        original_replace(source, destination)
        if source == pending:
            claimed.append(destination)
            _write_pending(str(tmp_path), replacement)

    monkeypatch.setattr(display_relay.os, "replace", replace_and_produce)
    assert display_relay.relay(str(tmp_path)) == 0
    assert "Setup issues found" in capsys.readouterr().out
    assert claimed
    assert json.loads(open(pending, encoding="utf-8").read())["systemMessage"] == "replacement"


# --- fallback contract ------------------------------------------------------

def test_missing_pending_hands_back_to_the_caller(tmp_path, capsys):
    assert display_relay.relay(str(tmp_path)) == 1
    assert capsys.readouterr().out == ""


def test_malformed_pending_hands_back_without_consuming(tmp_path, capsys):
    """A message we cannot parse must still reach the user via the plain relay."""
    pending = os.path.join(str(tmp_path), display_relay.PENDING_NAME)
    with open(pending, "w", encoding="utf-8") as fh:
        fh.write("not json at all")

    assert display_relay.relay(str(tmp_path)) == 1
    assert capsys.readouterr().out == ""
    assert os.path.exists(pending)


def test_payload_without_messages_passes_through(tmp_path, capsys):
    """The shell EXIT trap can write a bare payload; there is nothing to qualify."""
    _write_pending(str(tmp_path), {"continue": True, "suppressOutput": True},
                   age_seconds=3600)

    assert display_relay.relay(str(tmp_path)) == 0

    out = json.loads(capsys.readouterr().out)
    assert out == {"continue": True, "suppressOutput": True}


def test_main_returns_fallback_status_for_a_missing_dir(tmp_path):
    missing = os.path.join(str(tmp_path), "nope")
    assert display_relay.main(["--data-dir", missing]) == 1


# --- hook wiring ------------------------------------------------------------

def test_display_hook_prefers_the_relay_and_keeps_the_plain_fallback():
    """Drift guard on hooks/userpromptsubmit/bootstrap-display.sh.

    On a fresh machine there is no Python and the pending file is often the
    message saying so, so the plain `cat` + `mv` relay must survive.
    """
    here = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    hook = os.path.join(here, "plugins", "bootstrap", "hooks",
                        "userpromptsubmit", "bootstrap-display.sh")
    text = open(hook, encoding="utf-8").read()
    assert "bootstrap_lib/display_relay.py" in text
    # Claim pattern: PENDING is renamed to a per-process claim name FIRST
    # (mirrors display_relay.py's os.replace claim), then the claim (not
    # PENDING) is cat'd and finally renamed to .displayed.
    assert 'mv "$PENDING" "$_CLAIM"' in text
    assert 'cat "$_CLAIM"' in text
    assert 'mv -f "$_CLAIM"' in text


@needs_bash
class TestShellFallbackClaimPattern:
    """I4: bootstrap-display.sh's fallback tail (display_relay.py absent) must
    claim the pending file BEFORE catting it, mirroring display_relay.py's
    claim pattern -- so a producer that atomically replaces the pending file
    between claim and emit has its fresh verdict left pending for the next
    prompt, not silently renamed to .displayed unread."""

    def _scaffold(self, tmp_path: Path) -> dict:
        plugin_root = tmp_path / "mkt" / "bootstrap" / "0.0.0"
        ups_dir = plugin_root / "hooks" / "userpromptsubmit"
        ups_dir.mkdir(parents=True)
        display = ups_dir / "bootstrap-display.sh"
        display.write_text(DISPLAY_HOOK.read_text(), encoding="utf-8")
        display.chmod(0o755)
        # Deliberately no bootstrap_lib/display_relay.py under plugin_root ->
        # the relay-preferred branch's file check fails and the plain
        # fallback tail runs.
        home = tmp_path / "home"
        home.mkdir()
        data_dir = home / ".claude" / "plugins" / "data" / "mkt" / "bootstrap"
        data_dir.mkdir(parents=True)
        return {"plugin_root": plugin_root, "display": display, "home": home, "data_dir": data_dir}

    def _run(self, s: dict, env_extra: dict | None = None,
              stdin: str = "") -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["HOME"] = s["home"].as_posix()
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
            [BASH, str(s["display"])],
            input=stdin, capture_output=True, text=True, env=env, timeout=60,
        )

    def test_fallback_emits_pending_and_renames_to_displayed(self, tmp_path: Path) -> None:
        """Baseline (no race): the pending content is emitted once and ends up
        at .displayed, same observable contract as before."""
        s = self._scaffold(tmp_path)
        pending = s["data_dir"] / "bootstrap_display.pending"
        pending.write_text('{"continue": true, "systemMessage": "hello"}', encoding="utf-8")

        result = self._run(s)
        assert result.returncode == 0, result.stderr
        assert "hello" in result.stdout
        assert not pending.exists()
        displayed = s["data_dir"] / "bootstrap_display.displayed"
        assert displayed.exists()
        assert "hello" in displayed.read_text(encoding="utf-8")

    def test_producer_replacement_after_claim_leaves_new_content_pending(self, tmp_path: Path) -> None:
        """The observed bug: an engine that atomically replaces the pending
        file between the plain `cat` and the plain `mv` gets its FRESH verdict
        silently renamed to .displayed, unread. With the claim pattern, the
        first mv wins the OLD content; a producer racing in afterwards writes
        a NEW pending file that is untouched by this invocation and is left
        for the next prompt. The old content is emitted exactly once."""
        s = self._scaffold(tmp_path)
        pending = s["data_dir"] / "bootstrap_display.pending"
        pending.write_text('{"continue": true, "systemMessage": "OLD-VERDICT"}', encoding="utf-8")

        real_mv = shutil.which("mv")
        assert real_mv, "mv must be on PATH to build the race harness"
        fake_bin = tmp_path / "fakebin"
        fake_bin.mkdir()
        fake_mv = fake_bin / "mv"
        fake_mv.write_text(
            "#!/usr/bin/env bash\n"
            'if [[ "$1" == *bootstrap_display.pending ]] && [[ "$2" == *.claim.* ]]; then\n'
            f'    "{real_mv}" "$@"\n'
            "    status=$?\n"
            '    printf \'%s\' "$FRESH_CONTENT" > "$(dirname "$1")/bootstrap_display.pending"\n'
            "    exit $status\n"
            "else\n"
            f'    exec "{real_mv}" "$@"\n'
            "fi\n",
            encoding="utf-8",
        )
        fake_mv.chmod(0o755)

        env_extra = {
            "PATH": f"{fake_bin.as_posix()}:{os.environ.get('PATH', '')}",
            "FRESH_CONTENT": '{"continue": true, "systemMessage": "NEW-VERDICT"}',
        }
        result = self._run(s, env_extra=env_extra)
        assert result.returncode == 0, result.stderr
        assert "OLD-VERDICT" in result.stdout, "the claimed (old) content must be emitted"
        assert "NEW-VERDICT" not in result.stdout, (
            "the fresh content that raced in after the claim must NOT be emitted "
            "by this invocation -- it belongs to the next prompt"
        )
        assert pending.exists(), "the fresh content must be left pending for the next prompt"
        assert "NEW-VERDICT" in pending.read_text(encoding="utf-8")
        displayed = s["data_dir"] / "bootstrap_display.displayed"
        assert displayed.exists()
        assert "OLD-VERDICT" in displayed.read_text(encoding="utf-8")


def test_non_object_json_payload_is_left_for_the_shell_fallback(tmp_path, capsys):
    """A valid-JSON payload that is not an object takes the exit-1 fallback,
    which only works if the pending file is still there for the hook to cat."""
    from bootstrap_lib import display_relay

    pending = tmp_path / display_relay.PENDING_NAME
    pending.write_text("[1, 2, 3]", encoding="utf-8")

    assert display_relay.relay(str(tmp_path)) == 1
    assert capsys.readouterr().out == ""
    assert pending.exists(), "the claim must be handed back for the fallback cat"
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".")]
