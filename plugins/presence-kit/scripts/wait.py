"""presence-kit waiter: block until a background job's output is done, stalls, or a slice ends.

Usage:
  wait.py (--output FILE | --file PATH --pattern REGEX) --stall-minutes N
          [--path DIR ...] [--pid PID ...] [--slice-seconds S] [--poll-seconds P]

Exit codes:
  0  done: --output mode, the last non-empty line is "[exited with code N]";
     --file mode, REGEX matches a line of the file (^ and $ anchor at line ends).
     Prints that line.
  3  stalled: no size or mtime change for N minutes. Staleness is measured from the
     file's own mtime against the wall clock, so a file already quiet for N minutes
     when a slice starts stalls on the first poll instead of after N more minutes.
     With --path or --pid the job must also show no process activity (below).
  1  process sampling failed (only with --path or --pid); the reason goes to stderr.
  4  slice over: S seconds elapsed (default 150, under the ~180 s after which the
     harness backgrounds a foreground command), still live. Run again.
  2  bad arguments, or the file never appears within N minutes.

Process activity (as in watch.py, which shares scripts/procwatch.py): a quiet file
is not a stall while a process of the job used CPU. With --path DIR (repeatable), a
process whose working directory is inside DIR counts; with --pid PID (repeatable),
that process and its descendants count. A process counts when it used at least 1%
of a core since the previous poll, so the first poll of a slice only baselines and
cannot report a stall. Slices are separate processes, so the time of the last
activity seen is kept in one small file in the temp directory
(presence-kit-wait-<hash of the output path>.json); it is removed on exit 0 and 3.

Without --path and --pid the script samples nothing and writes nothing.
"""
import argparse
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import procwatch  # noqa: E402

# The harness moves a foreground Bash command that runs past about 180 s to the
# background whatever its timeout, which ends a waiter agent's wait: a slice must
# return well inside that window.
DEFAULT_SLICE_SECONDS = 150.0
TRAILER = re.compile(r"\[exited with code \d+\]")


class _BadArguments(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise _BadArguments(message)


def _positive(text):
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}")
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive number: {text!r}")
    return value


def _pid(text):
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value < 1:
        raise argparse.ArgumentTypeError(f"invalid pid {text!r}: a positive integer is required")
    return value


def _build_parser():
    parser = _Parser(prog="wait.py", allow_abbrev=False)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--output", metavar="FILE")
    group.add_argument("--file", metavar="PATH")
    parser.add_argument("--pattern", metavar="REGEX")
    parser.add_argument("--stall-minutes", dest="stall_minutes", type=_positive, required=True)
    parser.add_argument("--slice-seconds", dest="slice_seconds", type=_positive, default=DEFAULT_SLICE_SECONDS)
    parser.add_argument("--poll-seconds", dest="poll_seconds", type=_positive, default=2.0)
    parser.add_argument("--path", dest="paths", action="append", default=[], metavar="DIR")
    parser.add_argument("--pid", dest="pids", action="append", default=[], type=_pid, metavar="PID")
    return parser


def _parse(argv):
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.file is not None and args.pattern is None:
        raise _BadArguments("--file requires --pattern")
    if args.output is not None and args.pattern is not None:
        raise _BadArguments("--pattern goes with --file only")
    if args.pattern is not None:
        try:
            args.regex = re.compile(args.pattern, re.MULTILINE)
        except re.error as exc:
            raise _BadArguments(f"invalid --pattern: {exc}")
    return args


def last_nonempty_line(text):
    """Return the last non-empty line, stripped, or None when there is none."""
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if stripped:
            return stripped
    return None


def _snapshot(path):
    """Return (text, signature, mtime) for the file, or None when it does not exist."""
    try:
        st = os.stat(path)
        with open(path, "rb") as handle:
            data = handle.read()
    except FileNotFoundError:
        return None
    return data.decode("utf-8", errors="replace"), (st.st_size, st.st_mtime_ns), st.st_mtime


def _match_line(args, text):
    """Return the line that satisfies done, or None while not done."""
    if args.output is not None:
        line = last_nonempty_line(text)
        if line is not None and TRAILER.fullmatch(line):
            return line
        return None
    match = args.regex.search(text)
    if match is None:
        return None
    start = text.rfind("\n", 0, match.start()) + 1
    end = text.find("\n", match.start())
    if end == -1:
        end = len(text)
    return text[start:end].rstrip("\r")


def _fmt(number):
    return f"{number:g}"


def _state_file(state_dir, path):
    base = state_dir if state_dir is not None else tempfile.gettempdir()
    digest = hashlib.sha1(os.path.realpath(path).encode("utf-8")).hexdigest()[:20]
    return os.path.join(base, f"presence-kit-wait-{digest}.json")


def _read_last_work(state_file):
    """The wall time of the last process activity an earlier slice saw, or None."""
    try:
        with open(state_file, encoding="utf-8") as handle:
            return float(json.load(handle)["last_work"])
    except FileNotFoundError:
        return None


def _write_last_work(state_file, when):
    os.makedirs(os.path.dirname(state_file), exist_ok=True)
    tmp = f"{state_file}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump({"last_work": when}, handle)
    os.replace(tmp, state_file)


def _remove(state_file):
    try:
        os.remove(state_file)
    except FileNotFoundError:
        pass


def main(argv=None, *, now=time.monotonic, sleep=time.sleep, wall=time.time, stdout=None, stderr=None,
         procs=None, cwds=None, state_dir=None):
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    try:
        args = _parse(sys.argv[1:] if argv is None else argv)
    except _BadArguments as exc:
        print(f"bad arguments: {exc}", file=err)
        return 2

    path = args.output if args.output is not None else args.file
    window = args.stall_minutes * 60.0
    start = now()
    last_sig = None
    last_change = start
    roots = [os.path.realpath(p) for p in args.paths if os.path.isdir(p)]
    sampling = bool(roots or args.pids)
    sample_procs = procs if procs is not None else procwatch._sample_procs
    sample_cwds = cwds if cwds is not None else procwatch._sample_cwds
    state_file = _state_file(state_dir, path) if sampling else None
    last_work = _read_last_work(state_file) if sampling else None  # wall time of the last activity
    prev_cpu = None  # pid -> CPU seconds at the previous sample
    prev_t = None
    baselined = False  # a first process sample exists, so CPU growth can be measured

    while True:
        snap = _snapshot(path)
        t = now()
        if snap is None:
            if t - start >= window:
                print(f"no file {path} within {_fmt(args.stall_minutes)} minutes", file=err)
                return 2
        else:
            text, sig, mtime = snap
            line = _match_line(args, text)
            if line is not None:
                print(line, file=out)
                if sampling:
                    _remove(state_file)
                return 0
            if sig != last_sig:
                last_sig = sig
                # The change happened at the file's mtime, not when this process
                # first looked: age it by the wall clock so staleness carries
                # across slices (a future mtime counts as a change just now).
                last_change = t - max(0.0, wall() - mtime)
            age = t - last_change
            if sampling:
                try:
                    table = sample_procs()
                    if prev_cpu is not None:
                        found = procwatch._working(
                            prev_cpu, table, t - prev_t, procwatch._ancestors(table, os.getpid()),
                            roots, args.pids, sample_cwds)
                        if found:
                            last_work = wall()
                            _write_last_work(state_file, last_work)
                    baselined = prev_cpu is not None
                    prev_cpu = {pid: proc.cpu for pid, proc in table.items()}
                    prev_t = t
                except procwatch.ProcessSampleError as exc:
                    print(f"process sampling failed: {exc}", file=err)
                    return 1
                if last_work is not None:
                    age = min(age, max(0.0, wall() - last_work))
            # The first poll only baselines process CPU, so it cannot rule out a working process.
            if age >= window and (baselined or not sampling):
                print(f"stalled: no change in {path} for {_fmt(args.stall_minutes)} minutes", file=out)
                if sampling:
                    _remove(state_file)
                return 3
        if t - start >= args.slice_seconds:
            print(f"slice over: {path} still live", file=out)
            return 4
        sleep(args.poll_seconds)


if __name__ == "__main__":
    sys.exit(main())
