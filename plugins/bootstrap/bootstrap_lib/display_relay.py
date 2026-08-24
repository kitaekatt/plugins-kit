#!/usr/bin/env python3
"""Age-stamped relay for the bootstrap display handshake.

Bootstrap runs in background mode: the engine (or, on the pre-Python paths, the
SessionStart shell hook) writes its verdict to ``bootstrap_display.pending``,
and the UserPromptSubmit hook emits that file as its own stdout on the next
prompt -- in WHATEVER session gets there first.

Those two moments are not the same moment, and nothing in the payload said so.
A pending file survives until some session's first prompt consumes it, so a
session that starts after a skipped pass (cooldown / session guard) can be
handed a verdict produced long before, and "Setup issues found" reads as a
statement about the machine right now. It is not; it is a statement about the
machine when the pass finished.

This relay closes that gap by stamping the message with its own age at DELIVERY
time. The age is an observed fact, not a judgement: it is derived from the
pending file's mtime, which an atomic rename sets to the instant the verdict was
written. Nothing new is persisted, and the stamp is emitted UNCONDITIONALLY --
including on a fresh message, where it reads "just now". A marker that appeared
only when stale would make its own absence carry meaning, which is the
silent-report shape this exists to remove.

Contract with hooks/userpromptsubmit/bootstrap-display.sh:
  exit 0 -- the message was emitted on stdout and the file was renamed to
            ``bootstrap_display.displayed``. The caller must NOT also cat it.
  exit 1 -- nothing was written to stdout and nothing was renamed. The caller
            falls back to the plain `cat` + `mv` relay, which is also what runs
            when this module or a Python interpreter is unavailable at all (a
            fresh machine, where the pending file is often the message saying
            Python is missing).

Stdlib-only, and it imports nothing from ``bootstrap_lib``: it runs on the
UserPromptSubmit path, which must work before any venv exists.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

PENDING_NAME = "bootstrap_display.pending"
DISPLAYED_NAME = "bootstrap_display.displayed"

# Rendering granularity only -- how a duration is spelled, not a verdict about
# when a message stops being trustworthy. See the plugin-opinion razor note in
# the module tests: there is no staleness threshold to configure because the
# relay never decides staleness, it reports the age and lets the reader decide.
_MINUTE = 60
_HOUR = 3600
_DAY = 86400


def format_age(seconds):
    """Spell an age in seconds as a short human phrase (ASCII only)."""
    if seconds < 0:
        # Clock skew (or a file stamped in the future). Say so rather than
        # inventing a plausible-looking age.
        return "at an unknown time (its timestamp is in the future)"
    if seconds < _MINUTE:
        return "just now"
    if seconds < _HOUR:
        return "%dm ago" % (seconds // _MINUTE)
    if seconds < _DAY:
        return "%dh ago" % (seconds // _HOUR)
    return "%dd ago" % (seconds // _DAY)


def _iso_utc(epoch):
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))
    except (ValueError, OSError, OverflowError):
        return "unknown"


def user_suffix(seconds):
    """The user-facing stamp appended to systemMessage."""
    if 0 <= seconds < _MINUTE:
        return " [bootstrap pass finished just now]"
    return (
        " [bootstrap pass finished %s -- this may not reflect the machine's "
        "current state]" % format_age(seconds)
    )


def agent_prefix(seconds, produced_at):
    """The Claude-facing stamp prepended to additionalContext.

    Claims only what the relay observed: when the producing pass finished, and
    that the report describes the machine as of that moment. It adds
    information; it withholds nothing and asks for no checkpoint to be skipped.
    """
    when = _iso_utc(produced_at)
    if 0 <= seconds < _MINUTE:
        return (
            "bootstrap -> TIMING: this report comes from a bootstrap pass that "
            "finished just now (%s), so it describes the machine as of moments "
            "ago." % when
        )
    return (
        "bootstrap -> TIMING: this report comes from a bootstrap pass that "
        "finished %s (%s). It describes the machine AS OF THAT MOMENT, not "
        "necessarily now -- bootstrap delivers a pass's verdict on the next "
        "prompt in whatever session reaches it first. Do not present it to the "
        "user as the machine's current state; if anything may have changed "
        "since (they ran a fix, restarted, installed something), say the report "
        "is %s and re-check before acting on it."
        % (format_age(seconds), when, format_age(seconds))
    )


def annotate(payload, seconds, produced_at):
    """Return the payload with age stamps folded into both message surfaces.

    Both surfaces are stamped because they reach different readers:
    ``systemMessage`` is user-facing only and ``additionalContext`` is
    Claude-facing only. Stamping one would leave the other able to mistake the
    message for a current verdict. Payload shapes that carry neither (the shell
    hook's bare EXIT-trap JSON) pass through untouched -- there is no message
    to qualify.
    """
    out = dict(payload)
    msg = out.get("systemMessage")
    if isinstance(msg, str) and msg:
        out["systemMessage"] = msg + user_suffix(seconds)
    hso = out.get("hookSpecificOutput")
    if isinstance(hso, dict):
        ctx = hso.get("additionalContext")
        if isinstance(ctx, str) and ctx:
            hso = dict(hso)
            hso["additionalContext"] = "%s\n\n%s" % (
                agent_prefix(seconds, produced_at), ctx)
            out["hookSpecificOutput"] = hso
    return out


def relay(data_dir, now=None):
    """Emit the pending display file with an age stamp, then consume it.

    Returns the exit status described in the module docstring.
    """
    pending = os.path.join(data_dir, PENDING_NAME)
    try:
        produced_at = os.path.getmtime(pending)
        with open(pending, "r", encoding="utf-8", errors="replace") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        # Missing, unreadable, or not JSON. The plain relay can still cat a
        # malformed file, and Claude Code's own handling of it is unchanged by
        # us, so hand it back rather than swallowing the message.
        return 1
    if not isinstance(payload, dict):
        return 1

    if now is None:
        now = time.time()
    seconds = int(now - produced_at)
    try:
        text = json.dumps(annotate(payload, seconds, produced_at))
    except (TypeError, ValueError):
        return 1

    # Write first, consume second: a rename-then-write ordering would lose the
    # message outright if the write failed, while write-then-rename can at
    # worst leave the file for one more prompt.
    sys.stdout.write(text)
    sys.stdout.flush()
    try:
        os.replace(pending, os.path.join(data_dir, DISPLAYED_NAME))
    except OSError:
        pass
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Emit the pending bootstrap display message with its age")
    parser.add_argument("--data-dir", required=True, help="bootstrap data dir")
    args = parser.parse_args(argv)
    try:
        return relay(args.data_dir)
    except Exception:
        # Best-effort by contract: any surprise falls back to the plain relay
        # rather than costing the user their bootstrap message.
        return 1


if __name__ == "__main__":
    sys.exit(main())
