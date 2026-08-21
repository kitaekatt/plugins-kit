"""Tests pinning two coupled defects in the `claude --bg` launch banner path
(content_pipeline.execution.drivers.claude_bg):

1. ``_BG_LAUNCH_BANNER_RE`` required a literal ``*`` separator. The real
   ``claude --bg`` banner (CLI 2.1.238) uses U+00B7 MIDDLE DOT instead. A
   live capture recorded, via `cat -A`, exactly::

       backgrounded M-BM-7 ff97012c$

   `cat -A`'s `M-BM-7` is its rendering of the UTF-8 bytes ``C2 B7`` --
   U+00B7 MIDDLE DOT -- so the real banner text is
   ``"backgrounded"`` + U+00B7 + ``" ff97012c"``.

2. ``_default_runner`` called ``subprocess.run(..., capture_output=True,
   text=True)`` with no ``encoding=``, so on Windows the output was decoded
   with the locale codepage (cp1252) instead of UTF-8. Decoding the same
   ``C2 B7`` bytes as cp1252 yields TWO mojibaked characters
   (U+00C2 followed by U+00B7) instead of the one real character -- so a
   regex fix
   ALONE is insufficient on Windows: this was demonstrated live during the
   defect investigation, where a shim matching a real middle dot did not
   fire, because the bytes never arrived correctly decoded in the first
   place.

Both fixes are pinned here independently, plus a regression case for the
historical ``*``-separated form so old banners (and this module's own
extensive `* <id>` test fixtures elsewhere in this directory) keep parsing.
"""

from __future__ import annotations

import subprocess

import pytest

from content_pipeline.execution.drivers.claude_bg import (
    ClaudeCli,
    _default_runner,
    _parse_launch_session_id,
)

# Captured live from claude CLI 2.1.238 on 2026-08-20. `cat -A` showed
# exactly `backgrounded M-BM-7 ff97012c$` -- the UTF-8 bytes C2 B7 (U+00B7
# MIDDLE DOT) rendered byte-by-byte because the terminal recognized them as
# a multi-byte UTF-8 sequence. Written as an escape, never as a literal
# non-ASCII byte, per this repo's ASCII-only-tracked-files convention.
REAL_BANNER = "backgrounded \u00b7 ff97012c"

# The SAME bytes (C2 B7), misdecoded as cp1252 instead of UTF-8 -- exactly
# what `_default_runner` used to hand back on Windows before the
# `encoding="utf-8"` fix. 0xC2 -> U+00C2 (<U+00C2>), 0xB7 -> U+00B7 (<U+00B7>) under
# cp1252, so the one real character becomes two.
MOJIBAKED_BANNER = "backgrounded \u00c2\u00b7 ff97012c"

# The historical form this module's other tests script everywhere; must
# keep parsing so none of those fixtures need to change.
ASTERISK_BANNER = "backgrounded * a47add3f"

# Captured 2026-08-21 at the real process boundary -- through the SHIPPED
# `launch_bg` / `_default_runner` path on claude CLI 2.1.238, via a standalone
# one-shot probe (not the gate run; the gate itself stayed untouched). This is
# the FULL multi-line (stdout, stderr, rc) triple the driver actually
# receives from `subprocess.run`, unlike REAL_BANNER above, which is a
# single-line `cat -A` rendering of just the banner line. Recorded verbatim
# in dev/tasks/cpk-session-recipients/log.md, section "THE REAL BANNER,
# captured at the process boundary". The separator is a single U+00B7 MIDDLE
# DOT and it arrived un-mojibaked -- the UTF-8 `C2 B7` decoding fix works on
# this machine's real subprocess call. Written as escapes constructed via
# chr(), never as a literal non-ASCII byte, per this repo's
# ASCII-only-tracked-files convention.
REAL_LAUNCH_STDOUT = (
    "backgrounded " + chr(0xB7) + " f77605d2\n"
    "  claude agents             list sessions\n"
    "  claude attach f77605d2    open in this terminal\n"
    "  claude logs f77605d2      show recent output\n"
    "  claude stop f77605d2      stop this session\n"
)
# `errors="replace"` doing its documented job on a byte `_default_runner`'s
# UTF-8 decoding could not map cleanly -- not a defect, and not exercised by
# `_parse_launch_session_id` (which only reads stdout), but pinned here for
# fidelity to the real triple.
REAL_LAUNCH_STDERR = "Starting background service" + chr(0xFFFD) + "\n"
REAL_LAUNCH_RC = 0


def test_parses_the_real_middle_dot_banner():
    assert _parse_launch_session_id(REAL_BANNER) == "ff97012c"


def test_parses_the_mojibaked_windows_banner():
    """What Windows actually delivered before the encoding fix -- the
    regex must be tolerant of this shape too, since a decoding fix alone
    cannot retroactively fix bytes some OTHER process already mis-decoded
    (e.g. a banner read from a log file written before the fix)."""
    assert _parse_launch_session_id(MOJIBAKED_BANNER) == "ff97012c"


def test_parses_the_historical_asterisk_banner():
    assert _parse_launch_session_id(ASTERISK_BANNER) == "a47add3f"


def test_does_not_match_noise_without_a_hex_id():
    assert _parse_launch_session_id("backgrounded and ready") is None


def test_does_not_match_an_id_shorter_than_the_eight_char_floor():
    # "abc123" is only 6 hex characters -- below the observed 8-char floor.
    assert _parse_launch_session_id("backgrounded * abc123") is None


def test_default_runner_passes_utf8_encoding(monkeypatch):
    """Pins the one place the untested process boundary CAN be pinned
    without spawning anything: patch `subprocess.run` and inspect the
    kwargs `_default_runner` passes it."""
    captured = {}

    class _FakeCompleted:
        stdout = "backgrounded \u00b7 ff97012c"
        stderr = ""
        returncode = 0

    def _fake_run(argv, **kwargs):
        captured.update(kwargs)
        return _FakeCompleted()

    monkeypatch.setattr(subprocess, "run", _fake_run)

    stdout, stderr, rc = _default_runner(["claude", "--bg", "do the thing"])

    assert captured["encoding"] == "utf-8"
    assert captured["errors"] == "replace"
    assert captured["text"] is True
    assert (stdout, stderr, rc) == ("backgrounded \u00b7 ff97012c", "", 0)


def test_parses_the_real_full_multiline_stdout():
    """The FULL multi-line stdout `_default_runner` actually hands back, not
    just the banner line -- pins `_parse_launch_session_id` against the
    real capture end to end (extra trailing lines, real session id)."""
    assert _parse_launch_session_id(REAL_LAUNCH_STDOUT) == "f77605d2"


def test_claude_cli_launch_bg_triple_parses_to_the_real_session_id():
    """Routes the real capture through the actual `ClaudeCli.launch_bg` ->
    `_parse_launch_session_id` path (the one `dispatch_unit` uses), via a
    fake `runner` returning exactly the captured triple -- real code-path
    coverage without inventing dispatch/store/agents-json scaffolding."""

    def _fake_runner(argv, **kwargs):
        return REAL_LAUNCH_STDOUT, REAL_LAUNCH_STDERR, REAL_LAUNCH_RC

    cli = ClaudeCli(executable="claude", runner=_fake_runner)
    stdout, stderr, rc = cli.launch_bg("do the thing")

    assert rc == REAL_LAUNCH_RC
    assert stderr == REAL_LAUNCH_STDERR
    assert _parse_launch_session_id(stdout) == "f77605d2"
