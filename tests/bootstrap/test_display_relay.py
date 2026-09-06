"""Tests for the age-stamped display relay (bootstrap_lib/display_relay.py).

The behaviour under test: a bootstrap message delivered to a session cannot be
mistaken for a verdict on the current state of the machine. A freshly produced
message reads as current; an aged one is visibly marked on BOTH surfaces.
"""

import json
import os

import pytest

from bootstrap_lib import display_relay


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
    assert 'cat "$PENDING"' in text
    assert 'mv -f "$PENDING"' in text


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
