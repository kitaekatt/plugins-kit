#!/usr/bin/env python3
"""bootstrap -- the command-line face of the bootstrap engine.

Three verbs. The point of the first two is that a bootstrap pass is a
SINGLE-INSTANCE thing (bootstrap_lib.proc_lock):

    bootstrap        Is a pass running right now? Says so -- and if one IS,
                     stays attached and streams it until it finishes.
                     `--json` is the non-blocking scripting form.
    bootstrap run    Apply only the four user/project manifest layers.
                     Refuse while another pass is running; attaching could
                     inherit that pass's broader or different project scope.
    bootstrap reset  Clear the cooldown stamp so the NEXT session start runs a
                     real pass. Not a pass itself -- it is the lever for the
                     one case `run` does not cover, a layered bootstrap.json
                     edit that must converge through a genuine SessionStart.

Why the status probe is not "try to acquire and release": acquiring clears a
stale lock and holds the mutex for an instant, so a mere status check could
make a real launcher stand down. ``proc_lock.lock_holder`` is the read-only
query, applying the identical staleness rules so the two can never disagree.

Why attaching needs a marker file: the pass recorder buffers its events and
writes them at exit (two file writes instead of hundreds), so there is
normally nothing to tail mid-pass. Dropping ``events.watch`` in the data dir
tells the recorder a human is reading and makes it flush as it goes; removing
it on the way out restores the cheap discipline. See records.WATCH_FILENAME.

Stdlib-only, like everything a bootstrap lever can rely on.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

# The tail loop's poll period. Comfortably finer than the recorder's own
# one-second watched-flush floor, so the bound on what a reader waits for is
# the recorder's interval, not this one.
POLL_INTERVAL = 0.25

# How often the tail asks whether the pass is still running. Deliberately much
# coarser than POLL_INTERVAL: see the comment at its use in follow().
LOCK_CHECK_INTERVAL = 1.0

# After the lock clears, keep reading for this long. The recorder's atexit
# flush runs AFTER engine_lock's release (the context manager exits first,
# then interpreter shutdown runs atexit), so the final -- and most
# interesting -- records land slightly after the pass stops being "running".
# Leaving without this grace reliably truncates a pass's verdict.
FINAL_GRACE_SECONDS = 3.0

# This file ships at <plugin root>/scripts/, so bootstrap_lib is one level up.
# Putting it on sys.path here rather than in the shim keeps the module
# runnable by any caller (a test, `python scripts/bootstrap_cli.py`) and not
# only through its PATH lever. An IMPORT path derived from __file__, never a
# WRITE path -- every path this module writes comes from data_root() below.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# IMPORTED, not re-spelled. Both filenames are contracts with the recorder at
# the other end of them: the tail reads what it writes, and the watch marker is
# only honored under the exact name records.py stats for. A second literal here
# would keep working right up until one end was renamed.
from bootstrap_lib.records import EVENTS_FILENAME, WATCH_FILENAME  # noqa: E402


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

def data_root() -> str:
    """Mirror session-bootstrap.sh's BOOTSTRAP_DATA_ROOT derivation exactly.

    CLAUDE_BOOTSTRAP_DATA_ROOT redirects everything bootstrap owns to an
    alternate tree for one session (the claude-plugin-test launcher sets it);
    this lever has to target whatever tree the engine was pointed at, or it
    reports on a different machine state than the one being changed.
    """
    override = os.environ.get("CLAUDE_BOOTSTRAP_DATA_ROOT")
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".claude", "plugins", "data")


def marketplaces() -> list:
    """Marketplace names that have a bootstrap data dir, newest activity first.

    Like bootstrap-reset-cooldown, this does NOT assume plugins-kit: the lever
    is installed into ~/.local/bin as a copy that cannot derive its own
    marketplace from $0. BOOTSTRAP_MARKETPLACE scopes to one.
    """
    scoped = os.environ.get("BOOTSTRAP_MARKETPLACE")
    if scoped:
        return [scoped]
    root = data_root()
    found = []
    try:
        for name in sorted(os.listdir(root)):
            if os.path.isdir(os.path.join(root, name, "bootstrap")):
                found.append(name)
    except OSError:
        pass
    return found or ["plugins-kit"]


def plugin_data_dir(marketplace: str) -> str:
    return os.path.join(data_root(), marketplace, "bootstrap")


def find_plugin_root(marketplace: str, fallback: str = "") -> str:
    """Locate the bootstrap plugin tree whose wrapper should run the pass.

    Prefers the highest installed CACHE version, because that is the code
    Claude Code actually loads -- running the marketplace clone instead would
    provision with a version the session is not using. The clone is the
    fallback for a machine that has the marketplace but no cached install yet.
    """
    # An explicit BOOTSTRAP_PLUGIN_ROOT outranks discovery. It is the only way
    # to point this command at a tree that is not the installed one -- a dev
    # checkout, a worktree -- and a resolver that quietly preferred the cache
    # would run the installed engine while reporting the requested root's
    # basename, which is worse than not honouring the variable at all.
    override = os.environ.get("BOOTSTRAP_PLUGIN_ROOT")
    if override and _is_plugin_root(override):
        return override

    home = os.path.expanduser("~")
    cache = os.path.join(home, ".claude", "plugins", "cache", marketplace, "bootstrap")
    best = None
    try:
        for name in os.listdir(cache):
            candidate = os.path.join(cache, name)
            if not _is_plugin_root(candidate):
                continue
            key = _version_key(name)
            if best is None or key > best[0]:
                best = (key, candidate)
    except OSError:
        pass
    if best is not None:
        return best[1]
    clone = os.path.join(home, ".claude", "plugins", "marketplaces",
                         marketplace, "plugins", "bootstrap")
    if _is_plugin_root(clone):
        return clone
    return fallback if _is_plugin_root(fallback) else ""


def _is_plugin_root(path: str) -> bool:
    return bool(path) and os.path.isfile(
        os.path.join(path, "hooks", "sessionstart", "session-bootstrap.sh"))


def _version_key(name: str) -> tuple:
    """Numeric sort key for a version directory.

    String order is wrong here in a way that bites: "0.98.1" sorts above
    "0.104.0", which would run a superseded engine.
    """
    parts = []
    for chunk in name.split("."):
        parts.append(int(chunk) if chunk.isdigit() else 0)
    return tuple(parts)


def holder(data_dir: str):
    """proc_lock.lock_holder, importable from an installed plugin tree."""
    from bootstrap_lib.proc_lock import lock_holder
    return lock_holder(data_dir)


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------

def describe(marketplace: str) -> dict:
    info = holder(plugin_data_dir(marketplace))
    if info is None:
        return {"marketplace": marketplace, "running": False}
    return {
        "marketplace": marketplace,
        "running": True,
        "pid": info.get("pid"),
        "elapsed_seconds": round(info.get("age") or 0.0, 1),
    }


def cmd_status(args) -> int:
    """Report, and -- when a pass IS running -- stay attached until it ends.

    The bare command does not merely answer the question and leave: if
    something is running, waiting for it is what the asker almost always
    wanted next, and an answer that scrolls away a second before the pass
    finishes is the least useful moment to stop. So it blocks and streams,
    exactly like `run` does when it finds a pass already in flight.

    `--json` is the exception and stays non-blocking: it is the scripting
    surface, and a machine-readable probe that hangs for the minutes a full
    pass takes is not one.
    """
    reports = [describe(m) for m in marketplaces()]
    if args.json:
        print(json.dumps(reports, indent=2))
        return 0

    for r in reports:
        if not r["running"]:
            print("%s: no bootstrap pass is running" % r["marketplace"])
        elif r["pid"] is None:
            print("%s: a bootstrap pass is starting (claiming the lock); "
                  "waiting for it" % r["marketplace"])
        else:
            print("%s: a bootstrap pass is RUNNING (pid %s, %s elapsed); "
                  "waiting for it"
                  % (r["marketplace"], r["pid"], _duration(r["elapsed_seconds"])))

    running = [r for r in reports if r["running"]]
    if len(running) == 1:
        return follow(plugin_data_dir(running[0]["marketplace"]))
    if len(running) > 1:
        # Two passes at once is possible only across marketplaces, each with
        # its own lock. Tailing them interleaved would attribute lines to the
        # wrong engine, so say which and let the caller name one.
        print("\nMore than one marketplace has a pass running; "
              "set BOOTSTRAP_MARKETPLACE=<name> to follow one.")
    # Exit 0 whether or not a pass was running: "is one running" is a question
    # with two correct answers, and a non-zero code for one of them would read
    # as an error to every caller that checks `set -e` or `$?`. Scripts that
    # need the answer machine-readably use --json.
    return 0


def _duration(seconds) -> str:
    seconds = int(seconds or 0)
    if seconds < 60:
        return "%ds" % seconds
    return "%dm%02ds" % (seconds // 60, seconds % 60)


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------

def cmd_run(args) -> int:
    marketplace = marketplaces()[0]
    if len(marketplaces()) > 1 and not os.environ.get("BOOTSTRAP_MARKETPLACE"):
        # Running the wrong marketplace's engine would provision the wrong
        # machine state silently, so refuse rather than guess. Status is
        # happy to report on all of them; a pass has to name one.
        sys.stderr.write(
            "bootstrap run: more than one marketplace has a bootstrap data dir "
            "(%s).\nSet BOOTSTRAP_MARKETPLACE=<name> to choose one.\n"
            % ", ".join(marketplaces()))
        return 2

    data_dir = plugin_data_dir(marketplace)
    info = holder(data_dir)
    if info is not None:
        sys.stderr.write(
            "bootstrap run: another pass is already running; retry after it finishes.\n"
            "It is not attached because its manifest scope may be different.\n")
        return 2

    plugin_root = find_plugin_root(marketplace, args.plugin_root)
    if not plugin_root:
        sys.stderr.write(
            "bootstrap run: no bootstrap plugin tree found for marketplace '%s'.\n"
            "Looked under ~/.claude/plugins/cache/%s/bootstrap and "
            "~/.claude/plugins/marketplaces/%s/plugins/bootstrap.\n"
            % (marketplace, marketplace, marketplace))
        return 2

    runner = Path(plugin_root) / "scripts" / "bootstrap_run.py"
    print("Bootstrap engine: %s" % plugin_root)
    sys.stdout.flush()
    # Launch the shared layered runner directly. The SessionStart wrapper
    # provisions Python and plugins before dispatching its full engine pass;
    # none of that is part of a terminal user/project manifest run.
    return _stream_until_exit(
        data_dir,
        lambda: subprocess.Popen([
            sys.executable, str(runner), "--plugin-root", plugin_root,
            "--data-dir", data_dir, "--project-dir", str(Path.cwd()),
            "--console",
        ] + args.forward))


def _stream_until_exit(data_dir: str, launch) -> int:
    """Start the pass via ``launch()``, printing its records as they land.

    The child inherits stdout, so its own verdict still prints; the tail here
    carries the per-check lines that never reach console stdout. `emit`
    records are skipped -- that IS the verdict the child prints, and showing
    it twice is worse than not showing it here at all.

    The marker and the start offset are both established BEFORE the child
    exists: a record written between launching and attaching would otherwise
    fall in front of the offset and never print, and the recorder needs the
    marker in place to flush its very first records rather than its second
    second's worth.
    """
    events = os.path.join(data_dir, EVENTS_FILENAME)
    watch = os.path.join(data_dir, WATCH_FILENAME)
    offset = _size(events)
    with _watching(watch):
        proc = launch()
        while proc.poll() is None:
            offset = _drain(events, offset, verdict=False)
            time.sleep(POLL_INTERVAL)
        # The recorder's atexit flush lands after the process is reaped, so
        # drain past the exit rather than truncating the pass's last records.
        deadline = time.monotonic() + FINAL_GRACE_SECONDS
        while time.monotonic() < deadline:
            offset = _drain(events, offset, verdict=False)
            time.sleep(POLL_INTERVAL)
        _drain(events, offset, verdict=False)
    return proc.returncode


# --------------------------------------------------------------------------
# reset
# --------------------------------------------------------------------------

def cmd_reset(args) -> int:
    """Clear the cooldown, by DELEGATING to bootstrap-reset-cooldown.sh.

    Re-implementing the reset here would be a second answer to "which stamp
    files are a cooldown": the shell lever hashes $PWD exactly as
    session-bootstrap.sh does, sweeps every marketplace data dir, and clears
    the session-id guard alongside the stamp. Two implementations of that
    would agree until one of those three rules moved.

    So this verb exists for discoverability -- a user who has `bootstrap`
    on PATH should not have to know a second command name -- and every flag
    (`--all`, `--status`, `--project`, `--clear-alerts`, `--force`) is passed
    straight through, `--help` included.
    """
    script = find_reset_script(args.plugin_root)
    if not script:
        sys.stderr.write(
            "bootstrap reset: no bootstrap plugin tree found under %s.\n"
            % os.path.join(os.path.expanduser("~"), ".claude", "plugins"))
        return 2
    # `bash <path>`, not a direct exec: a cached or cloned plugin copy can
    # arrive without its mode bits, the same reason `run` spells it this way.
    return subprocess.call(["bash", script] + args.forward)


def find_reset_script(fallback: str = "") -> str:
    """Path to bootstrap-reset-cooldown.sh in whichever plugin tree is found.

    Tries every marketplace rather than marketplaces()[0]: the reset lever
    itself acts on all of them (or on BOOTSTRAP_MARKETPLACE), so the only
    question here is where a COPY of the script lives, and the first tree that
    has one will do.
    """
    for marketplace in marketplaces():
        plugin_root = find_plugin_root(marketplace, fallback)
        if not plugin_root:
            continue
        script = os.path.join(plugin_root, "scripts", "bootstrap-reset-cooldown.sh")
        if os.path.isfile(script):
            return script
    return ""


# --------------------------------------------------------------------------
# follow
# --------------------------------------------------------------------------

def follow(data_dir: str) -> int:
    """Stream the running pass's records until its lock clears.

    Attaches at the CURRENT end of the event file, so what prints is this
    pass's remaining output and not a replay of previous passes.
    """
    events = os.path.join(data_dir, EVENTS_FILENAME)
    watch = os.path.join(data_dir, WATCH_FILENAME)
    offset = _size(events)

    with _watching(watch):
        deadline = None
        next_lock_check = 0.0
        while True:
            offset = _drain(events, offset)
            if deadline is None:
                # Drain often, but check the LOCK rarely. Each check opens the
                # lock file, and on Windows a reader's open can collide with
                # the holder's own release rename (proc_lock retries through
                # it, but not colliding at all is better). Once a second is far
                # finer than anyone notices a pass ending.
                now = time.monotonic()
                if now < next_lock_check:
                    time.sleep(POLL_INTERVAL)
                    continue
                next_lock_check = now + LOCK_CHECK_INTERVAL
                if holder(data_dir) is None:
                    # The lock is gone but the recorder's atexit flush has not
                    # necessarily landed yet; keep draining for the grace
                    # window rather than cutting the verdict off.
                    deadline = time.monotonic() + FINAL_GRACE_SECONDS
            elif time.monotonic() >= deadline:
                _drain(events, offset)
                break
            time.sleep(POLL_INTERVAL)
    print("Bootstrap pass finished.")
    return 0


class _watching:
    """Hold the watch marker for the duration of a tail, whatever happens.

    Removal matters more than creation: leaving the marker behind would make
    every future pass flush per second for a reader who has gone. Covered on
    the normal path, on an exception, and on the Ctrl-C that is the single
    likeliest way a tail ends.
    """

    def __init__(self, path: str):
        self.path = path
        self._previous = {}

    def __enter__(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w") as f:
                f.write("%d\n" % os.getpid())
        except OSError:
            pass  # a tail that cannot flush faster is still a working tail
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                self._previous[sig] = signal.signal(sig, self._on_signal)
            except (OSError, ValueError, AttributeError):
                pass
        return self

    def _on_signal(self, signum, frame):
        self._cleanup()
        raise KeyboardInterrupt

    def __exit__(self, exc_type, exc, tb):
        self._cleanup()
        for sig, previous in self._previous.items():
            try:
                signal.signal(sig, previous)
            except (OSError, ValueError):
                pass
        return False

    def _cleanup(self):
        try:
            os.remove(self.path)
        except OSError:
            pass


def _size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _drain(path: str, offset: int, verdict: bool = True) -> int:
    """Print records appended since ``offset``; return the new offset.

    A file that SHRANK was rotated mid-tail (records.py keeps one previous
    generation at 2MB); restart from the beginning of the new one rather than
    seeking past its end and going permanently silent.
    """
    size = _size(path)
    if size < offset:
        offset = 0
    if size == offset:
        return offset
    # BINARY, deliberately. In text mode `tell()` returns an opaque cookie
    # and `seek()` accepts nothing else, so byte offsets taken from
    # os.path.getsize would be meaningless there -- and byte offsets are what
    # a tail needs, since the writer appends bytes.
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            chunk = f.read()
    except OSError:
        return offset

    # A partial final line means the writer is mid-append; leave it for the
    # next drain instead of printing half a record.
    raw_lines = chunk.split(b"\n")
    tail = raw_lines.pop()
    consumed = offset + len(chunk) - len(tail)

    for raw in raw_lines:
        line = raw.decode("utf-8", errors="replace").strip()
        if line:
            rendered = _render(line, verdict=verdict)
            if rendered:
                print(rendered)
    sys.stdout.flush()
    return consumed


def _render(line: str, verdict: bool = True):
    """One event line -> one human line, or None to skip it.

    ``verdict=False`` suppresses the pass verdict, for the caller whose child
    process is already printing that same verdict to the same terminal.
    """
    try:
        rec = json.loads(line)
    except ValueError:
        return None
    if rec.get("kind") == "emit":
        # The pass's verdict, as the user would have seen it at a prompt.
        if not verdict:
            return None
        message = rec.get("system_message")
        return "\n%s" % message if message else None
    text = rec.get("text")
    if not text:
        return None
    sev = rec.get("sev") or ""
    section = rec.get("section") or rec.get("plugin") or ""
    stamp = (rec.get("ts") or "")[11:19]
    prefix = " ".join(p for p in (stamp, sev, section) if p)
    return "%s: %s" % (prefix, text) if prefix else text


# --------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="bootstrap",
        description="Inspect lifecycle passes, run user/project manifests, or reset cooldowns.")
    parser.add_argument("--plugin-root", default="",
                        help=argparse.SUPPRESS)  # supplied by the shim
    parser.add_argument("--json", action="store_true",
                        help="machine-readable status")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run",
                   help="apply the four user/project bootstrap manifest layers; "
                        "refuse while another pass is running")
    # add_help=False so `bootstrap reset --help` reaches the lever this verb
    # delegates to and prints ITS flags, rather than argparse printing a
    # subcommand help that lists none of them.
    sub.add_parser("reset", add_help=False,
                   help="clear the cooldown so the next session start runs a "
                        "real pass; trailing flags (e.g. --all, --status) "
                        "pass through to bootstrap-reset-cooldown")

    # parse_known_args, and NO positional to collect the pass-through flags.
    # Neither nargs="*" nor argparse.REMAINDER works for them: a plain list
    # rejects any token starting with a dash, and REMAINDER as a subparser's
    # FIRST positional does not capture a LEADING option-like token either
    # (CPython bpo-17050), so `bootstrap run --verbose` -- the spelling the
    # help text advertises -- died with "unrecognized arguments: --verbose".
    args, extra = parser.parse_known_args(argv)
    if args.command in ("run", "reset"):
        args.forward = extra
        return cmd_run(args) if args.command == "run" else cmd_reset(args)
    # Only `run` and `reset` forward anything, so an unknown flag anywhere
    # else is still an error rather than something silently swallowed.
    if extra:
        parser.error("unrecognized arguments: %s" % " ".join(extra))
    return cmd_status(args)


if __name__ == "__main__":
    sys.exit(main())
