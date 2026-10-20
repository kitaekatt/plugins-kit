"""presence-kit watcher: one stdout line per event, for a Monitor to turn into notifications.

Usage:
  watch.py --path PATH [--path PATH ...]
           [--done-output FILE | --done-file PATH --done-pattern REGEX]
           [--stall-minutes N] [--progress-minutes N] [--poll-seconds S]

Every --poll-seconds (default 300) the watcher takes the newest modification time
across the watched paths: a file's own mtime, or for a directory the newest mtime of
any file under it (`.git` directories are skipped). A missing path contributes
nothing. The --done-output / --done-file path is watched too.

Lines, each flushed as it is printed:
  STALL: no change in watched paths for <N>m
      The newest mtime is at least --stall-minutes (default 20) old by the wall
      clock. Printed once per quiet episode; any newer mtime re-arms it. The
      watcher keeps running. The line reports; it prescribes no response.
  PROGRESS <elapsed>m: last change <age>m ago in <path>
      Every --progress-minutes (default: the progress_minutes value of the
      presence-kit configuration, 20 unless changed), whether or not anything
      changed. `no watched path exists yet` replaces the tail while no watched
      path exists.
  DONE: <line>
      The done condition holds (same detection as wait.py: --done-output, the
      last non-empty line is "[exited with code N]"; --done-file, REGEX matches a
      line). Checked on every poll. The watcher then exits 0. Without a done
      option it runs until killed.

Exit codes: 0 done; 2 bad arguments or invalid configuration.

The script never writes any file.
"""
import argparse
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import presence_config as pc  # noqa: E402
import wait  # noqa: E402

SKIPPED_DIRECTORY = ".git"
PROJECT_ENV = "CLAUDE_PROJECT_DIR"


class _BadArguments(wait._BadArguments):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise _BadArguments(message)


def _build_parser():
    parser = _Parser(prog="watch.py", allow_abbrev=False)
    parser.add_argument("--path", dest="paths", action="append", default=[], metavar="PATH")
    # dest names match what wait._match_line reads (args.output, args.regex).
    parser.add_argument("--done-output", dest="output", metavar="FILE")
    parser.add_argument("--done-file", dest="file", metavar="PATH")
    parser.add_argument("--done-pattern", dest="pattern", metavar="REGEX")
    parser.add_argument("--stall-minutes", dest="stall_minutes", type=wait._positive, default=20.0)
    parser.add_argument("--progress-minutes", dest="progress_minutes", type=wait._positive)
    parser.add_argument("--poll-seconds", dest="poll_seconds", type=wait._positive, default=300.0)
    return parser


def _parse(argv):
    args = _build_parser().parse_args(argv)
    if args.output is not None and args.file is not None:
        raise _BadArguments("--done-output and --done-file are mutually exclusive")
    if args.file is not None and args.pattern is None:
        raise _BadArguments("--done-file requires --done-pattern")
    if args.file is None and args.pattern is not None:
        raise _BadArguments("--done-pattern goes with --done-file only")
    if args.pattern is not None:
        try:
            args.regex = re.compile(args.pattern, re.MULTILINE)
        except re.error as exc:
            raise _BadArguments(f"invalid --done-pattern: {exc}")
    args.done_path = args.output if args.output is not None else args.file
    watched = list(args.paths)
    if args.done_path is not None and args.done_path not in watched:
        watched.append(args.done_path)
    if not watched:
        raise _BadArguments("at least one --path (or a done option) is required")
    args.watched = watched
    return args


def _on_walk_error(exc):
    # A directory that vanishes mid-walk is a race, not a fault; anything else is loud.
    if not isinstance(exc, FileNotFoundError):
        raise exc


def _newest(paths):
    """Return (mtime, path) of the newest file under the watched paths, or None."""
    best = None
    for root in paths:
        candidates = []
        if os.path.isdir(root):
            for directory, subdirs, files in os.walk(root, onerror=_on_walk_error):
                subdirs[:] = [d for d in subdirs if d != SKIPPED_DIRECTORY]
                candidates.extend(os.path.join(directory, name) for name in files)
        else:
            candidates.append(root)
        for candidate in candidates:
            try:
                mtime = os.stat(candidate).st_mtime
            except FileNotFoundError:
                continue
            if best is None or mtime > best[0]:
                best = (mtime, candidate)
    return best


def _minutes(seconds):
    return int(max(0.0, seconds) // 60)


def _project_dir(env, cwd):
    raw = env.get(PROJECT_ENV)
    return Path(raw) if raw else Path(cwd)


def main(argv=None, *, now=time.monotonic, sleep=time.sleep, wall=time.time,
         stdout=None, stderr=None, env=None, cwd=None):
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    env = os.environ if env is None else env
    try:
        args = _parse(sys.argv[1:] if argv is None else argv)
    except wait._BadArguments as exc:
        print(f"bad arguments: {exc}", file=err)
        return 2

    progress_minutes = args.progress_minutes
    if progress_minutes is None:
        try:
            config = pc.resolve(_project_dir(env, os.getcwd() if cwd is None else cwd), env)
        except pc.ConfigError as exc:
            print(exc.text, file=err)
            return 2
        progress_minutes = config.values["progress_minutes"]

    def emit(text):
        print(text, file=out)
        out.flush()

    stall_window = args.stall_minutes * 60.0
    progress_every = progress_minutes * 60.0
    start = now()
    start_wall = wall()
    next_progress = start + progress_every
    stall_reported_at = None  # baseline mtime of the quiet episode already reported

    while True:
        t = now()
        w = wall()

        if args.done_path is not None:
            snap = wait._snapshot(args.done_path)
            if snap is not None:
                line = wait._match_line(args, snap[0])
                if line is not None:
                    emit(f"DONE: {line}")
                    return 0

        newest = _newest(args.watched)
        # With nothing to age, the quiet period runs from the watcher's own start.
        baseline = newest[0] if newest is not None else start_wall
        age = w - baseline

        if stall_reported_at is not None and baseline > stall_reported_at:
            stall_reported_at = None
        if stall_reported_at is None and age >= stall_window:
            stall_reported_at = baseline
            emit(f"STALL: no change in watched paths for {_minutes(age)}m")

        if t >= next_progress:
            if newest is None:
                tail = "no watched path exists yet"
            else:
                tail = f"last change {_minutes(age)}m ago in {newest[1]}"
            emit(f"PROGRESS {_minutes(t - start)}m: {tail}")
            while next_progress <= t:
                next_progress += progress_every

        sleep(args.poll_seconds)


if __name__ == "__main__":
    sys.exit(main())
