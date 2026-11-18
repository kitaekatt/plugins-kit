"""presence-kit watcher: one stdout line per event, for a Monitor to turn into notifications.

Usage:
  watch.py [--task ID] [--path PATH ...] [--pid PID ...]
           [--done-output FILE | --done-file PATH --done-pattern REGEX]
           [--stall-minutes N] [--progress-minutes N] [--poll-seconds S]

--task ID derives the paths of a background task: its output file
`<tmp>/claude-<uid>/<project slug>/<CLAUDE_CODE_SESSION_ID>/tasks/<ID>.output` (<tmp> is
/tmp; the file is found by pattern, so the slug need not be known). A shell task's file is both watched and the done condition
(unless a done option is given). An agent task's file is a symlink to the agent's
transcript, whose records carry the agent's working directory (its worktree): that
directory is watched too. The agent's done condition is its hand-back: the watcher
ends with DONE once the transcript's last record answers a SubagentHandback tool call.
An agent resumed later by SendMessage needs a new watcher. The id must be a whole name of one file; an unset
CLAUDE_CODE_SESSION_ID, an unsupported platform or a missing output file exits 2.

Every --poll-seconds (default 300) the watcher takes the newest modification time
across the watched paths: a file's own mtime, or for a directory the newest mtime of
any file under it (`.git` and tool cache directories such as `.pytest_cache` and
`__pycache__` are skipped). A missing path contributes
nothing. The --done-output / --done-file path is watched too.

A live process is activity too (a long command changes no file). When a --path is a
directory, or --pid is given, each poll counts a process that used CPU (at least 1% of
a core) since the previous poll and whose working directory is inside a watched
directory, or that is a --pid process or its descendant. A process first seen at a
poll is a baseline, so the first poll of a directory watch cannot report a stall. The
watcher and its ancestors never count. A process-sampling error exits 1 loudly.

Lines, each flushed as it is printed:
  STALL: no change in watched paths for <N>m
      The newest mtime, or the latest poll that saw a process using CPU, is at
      least --stall-minutes (default 20) old by the wall clock. Printed once per quiet episode; any newer mtime re-arms it. The
      watcher keeps running. The line reports; it prescribes no response.
  PROGRESS <elapsed>m: last change <age>m ago in <path>
      Every --progress-minutes (default: the progress_minutes value of the
      presence-kit configuration, 0 unless changed; 0 means no PROGRESS lines),
      whether or not anything changed. `no watched path exists yet` replaces the tail while no watched
      path exists; `last activity <age>m ago: process <pid> (<command>) using CPU`
      replaces it when the latest sign of work is a process.
  DONE: <line>
      The done condition holds (same detection as wait.py: --done-output, the
      last non-empty line is "[exited with code N]"; --done-file, REGEX matches a
      line). Checked on every poll. The watcher then exits 0. Without a done
      option it runs until killed.

Exit codes: 0 done; 1 process sampling failed; 2 bad arguments or invalid configuration.

The script never writes any file.
"""
import argparse
import glob
import json
import math
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import presence_config as pc  # noqa: E402
import procwatch  # noqa: E402
import wait  # noqa: E402
# The process-sampling names stay reachable here (tests and main() use them).
from procwatch import (  # noqa: E402,F401
    MIN_CPU_FRACTION, Proc, ProcessSampleError, _ancestors, _descendants, _parse_cpu,
    _run_tool, _sample_cwds, _sample_procs, _within, _working,
)

SKIPPED_DIRECTORIES = frozenset({".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"})
HANDBACK_TOOL = "SubagentHandback"
SESSION_ENV = "CLAUDE_CODE_SESSION_ID"
TASK_ID = re.compile(r"[A-Za-z0-9]+")
PROJECT_ENV = "CLAUDE_PROJECT_DIR"
COMMAND_SHOWN = 60


class _BadArguments(wait._BadArguments):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise _BadArguments(message)


def _pid(text):
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value < 1:
        raise argparse.ArgumentTypeError(f"invalid pid {text!r}: a positive integer is required")
    return value


def _non_negative(text):
    """A --progress-minutes value: a positive number, or 0 for no PROGRESS lines."""
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}")
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError(f"must be zero or a positive number: {text!r}")
    return value


def _build_parser():
    parser = _Parser(prog="watch.py", allow_abbrev=False)
    parser.add_argument("--task", dest="task", metavar="ID")
    parser.add_argument("--path", dest="paths", action="append", default=[], metavar="PATH")
    parser.add_argument("--pid", dest="pids", action="append", default=[], type=_pid, metavar="PID")
    # dest names match what wait._match_line reads (args.output, args.regex).
    parser.add_argument("--done-output", dest="output", metavar="FILE")
    parser.add_argument("--done-file", dest="file", metavar="PATH")
    parser.add_argument("--done-pattern", dest="pattern", metavar="REGEX")
    parser.add_argument("--stall-minutes", dest="stall_minutes", type=wait._positive, default=20.0)
    parser.add_argument("--progress-minutes", dest="progress_minutes", type=_non_negative)
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
    if args.task is not None and TASK_ID.fullmatch(args.task) is None:
        raise _BadArguments(f"invalid task id {args.task!r}: letters and digits only")
    watched = list(args.paths)
    if args.done_path is not None and args.done_path not in watched:
        watched.append(args.done_path)
    if not watched and args.task is None and not args.pids:
        raise _BadArguments("at least one --path, --task, --pid or done option is required")
    args.watched = watched
    args.agent_transcript = None
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
                subdirs[:] = [d for d in subdirs if d not in SKIPPED_DIRECTORIES]
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


def _task_base(tmp_root):
    """`<tmp>/claude-<uid>`: where Claude Code keeps a session's task files (POSIX only)."""
    if not hasattr(os, "getuid"):
        raise _BadArguments("--task needs a POSIX platform: the task output layout is not known elsewhere")
    return Path(tmp_root if tmp_root is not None else "/tmp") / f"claude-{os.getuid()}"


def _agent_cwd(output):
    """The working directory recorded in an agent task's transcript, or None."""
    with open(output, encoding="utf-8") as handle:
        for raw in handle:
            try:
                obj = json.loads(raw)
            except ValueError:
                continue
            if isinstance(obj, dict) and isinstance(obj.get("cwd"), str):
                return obj["cwd"]
    return None


def _agent_finished(transcript):
    """True when the transcript's last record answers a SubagentHandback tool call."""
    records = []
    with open(transcript, encoding="utf-8") as handle:
        for raw in handle:
            try:
                obj = json.loads(raw)
            except ValueError:
                continue
            if isinstance(obj, dict):
                records.append(obj)
    if not records:
        return False
    last = records[-1]
    if last.get("toolEndsTurn") is not True:
        return False
    source = last.get("sourceToolAssistantUUID")
    for obj in reversed(records):
        if obj.get("uuid") == source and obj.get("type") == "assistant":
            content = (obj.get("message") or {}).get("content")
            return isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == HANDBACK_TOOL
                for b in content
            )
    return False


def _resolve_task(args, env, task_root):
    """Add the task's output file (and an agent's working directory) to args.watched."""
    session = env.get(SESSION_ENV)
    if not session:
        raise _BadArguments(f"--task needs {SESSION_ENV} to find the task output file")
    root = _task_base(task_root)
    pattern = os.path.join(glob.escape(str(root)), "*", glob.escape(session), "tasks", f"{args.task}.output")
    found = sorted(glob.glob(pattern))
    if not found:
        raise _BadArguments(
            f"no output file for task {args.task}: nothing matches {root}/*/{session}/tasks/{args.task}.output"
        )
    if len(found) > 1:
        raise _BadArguments(f"task {args.task} output is ambiguous: {', '.join(found)}")
    output = found[0]
    args.watched.append(output)
    real = os.path.realpath(output)
    if real.endswith(".jsonl"):
        cwd = _agent_cwd(real)
        if cwd is None:
            raise _BadArguments(f"agent transcript {real} records no working directory")
        args.watched.append(cwd)
        args.agent_transcript = real
    elif args.done_path is None:
        args.output = output
        args.done_path = output


def _minutes(seconds):
    return int(max(0.0, seconds) // 60)


def _project_dir(env, cwd):
    raw = env.get(PROJECT_ENV)
    return Path(raw) if raw else Path(cwd)


def main(argv=None, *, now=time.monotonic, sleep=time.sleep, wall=time.time,
         stdout=None, stderr=None, env=None, cwd=None, task_root=None, procs=None, cwds=None):
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    env = os.environ if env is None else env
    try:
        args = _parse(sys.argv[1:] if argv is None else argv)
        if args.task is not None:
            _resolve_task(args, env, task_root)
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
    progress_every = progress_minutes * 60.0  # 0: no PROGRESS lines
    start = now()
    start_wall = wall()
    next_progress = start + progress_every
    stall_reported_at = None  # baseline mtime of the quiet episode already reported
    sample_procs = procs if procs is not None else _sample_procs
    sample_cwds = cwds if cwds is not None else _sample_cwds
    prev_cpu = None  # pid -> CPU seconds at the previous sample
    prev_t = None
    last_work = None  # (wall time, pid, command) of the latest process seen using CPU
    baselined = False  # a first process sample exists, so CPU growth can be measured

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

        if args.agent_transcript is not None and _agent_finished(args.agent_transcript):
            emit("DONE: agent handed back its report")
            return 0

        roots = [os.path.realpath(p) for p in args.watched if os.path.isdir(p)]
        if roots or args.pids:
            try:
                table = sample_procs()
                if prev_cpu is not None:
                    own = _ancestors(table, os.getpid())
                    found = _working(prev_cpu, table, t - prev_t, own, roots, args.pids, sample_cwds)
                    if found:
                        last_work = (w, *found[0])
                baselined = prev_cpu is not None
                prev_cpu = {pid: proc.cpu for pid, proc in table.items()}
                prev_t = t
            except ProcessSampleError as exc:
                print(f"process sampling failed: {exc}", file=err)
                return 1

        newest = _newest(args.watched)
        # With nothing to age, the quiet period runs from the watcher's own start.
        baseline = newest[0] if newest is not None else start_wall
        working = last_work is not None and last_work[0] >= baseline
        if working:
            baseline = last_work[0]
        age = w - baseline

        if stall_reported_at is not None and baseline > stall_reported_at:
            stall_reported_at = None
        # The first poll only baselines process CPU, so it cannot rule out a working process.
        sampling = bool(roots or args.pids)
        if stall_reported_at is None and age >= stall_window and (baselined or not sampling):
            stall_reported_at = baseline
            emit(f"STALL: no change in watched paths for {_minutes(age)}m")

        if progress_every > 0 and t >= next_progress:
            if working:
                command = last_work[2][:COMMAND_SHOWN]
                tail = f"last activity {_minutes(age)}m ago: process {last_work[1]} ({command}) using CPU"
            elif newest is None:
                tail = "no watched path exists yet"
            else:
                tail = f"last change {_minutes(age)}m ago in {newest[1]}"
            emit(f"PROGRESS {_minutes(t - start)}m: {tail}")
            while next_progress <= t:
                next_progress += progress_every

        sleep(args.poll_seconds)


if __name__ == "__main__":
    sys.exit(main())
