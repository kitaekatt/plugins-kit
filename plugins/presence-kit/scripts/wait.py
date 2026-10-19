"""presence-kit waiter: block until a background job's output is done, stalls, or a slice ends.

Usage:
  wait.py (--output FILE | --file PATH --pattern REGEX) --stall-minutes N
          [--slice-seconds S] [--poll-seconds P]

Exit codes:
  0  done: --output mode, the last non-empty line is "[exited with code N]";
     --file mode, REGEX matches the file. Prints that line.
  3  stalled: no size or mtime change for N minutes.
  4  slice over: S seconds elapsed, still live. Run again.
  2  bad arguments, or the file never appears within N minutes.

The script never writes anything.
"""
import argparse
import math
import os
import re
import sys
import time

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


def _build_parser():
    parser = _Parser(prog="wait.py", allow_abbrev=False)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--output", metavar="FILE")
    group.add_argument("--file", metavar="PATH")
    parser.add_argument("--pattern", metavar="REGEX")
    parser.add_argument("--stall-minutes", dest="stall_minutes", type=_positive, required=True)
    parser.add_argument("--slice-seconds", dest="slice_seconds", type=_positive, default=540.0)
    parser.add_argument("--poll-seconds", dest="poll_seconds", type=_positive, default=2.0)
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
            args.regex = re.compile(args.pattern)
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
    """Return (text, signature) for the file, or None when it does not exist."""
    try:
        st = os.stat(path)
        with open(path, "rb") as handle:
            data = handle.read()
    except FileNotFoundError:
        return None
    return data.decode("utf-8", errors="replace"), (st.st_size, st.st_mtime_ns)


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


def main(argv=None, *, now=time.monotonic, sleep=time.sleep, stdout=None, stderr=None):
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

    while True:
        snap = _snapshot(path)
        t = now()
        if snap is None:
            if t - start >= window:
                print(f"no file {path} within {_fmt(args.stall_minutes)} minutes", file=err)
                return 2
        else:
            text, sig = snap
            line = _match_line(args, text)
            if line is not None:
                print(line, file=out)
                return 0
            if sig != last_sig:
                last_sig = sig
                last_change = t
            if t - last_change >= window:
                print(f"stalled: no change in {path} for {_fmt(args.stall_minutes)} minutes", file=out)
                return 3
        if t - start >= args.slice_seconds:
            print(f"slice over: {path} still live", file=out)
            return 4
        sleep(args.poll_seconds)


if __name__ == "__main__":
    sys.exit(main())
