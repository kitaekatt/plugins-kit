"""The pre-exec step in front of the run's command.

Usage: ``<python> child_exec.py <run-dir> <runner-pid> <runner-ctime> -- <argv...>``.

The runner starts the llm-kit command through this process so that the command's
identity is on disk before the command exists. In order:

1. record this process's PID and creation time in ``child.json`` (atomically);
2. wait for ``go``, which the runner writes only after it has recorded the child
   in its own tree record; give up without running anything if the runner is no
   longer alive (it died after Popen, so nobody would stop the command) or no ``go``
   arrives in time;
3. become the command (``execvp`` on POSIX, so the PID in ``child.json`` IS the
   command's PID and anything the command later starts is its descendant; on
   Windows, where exec starts a new process, run the command as a child and
   propagate its exit code).

That ordering closes the window the runner alone cannot: a parent-written record
cannot cover the interval between ``Popen`` and the first time the runner looks
for descendants, and the agent runs in its own session, so killing the runner
orphans it. With ``child.json`` written first, a server recovering from a dead
runner always knows where to start.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from task_system import procspawn  # noqa: E402
from task_system.dashboard import proctree, runfiles  # noqa: E402

GO_TIMEOUT_S = 30.0
POLL_S = 0.02
EXIT_RUNNER_GONE = 75
EXIT_NO_GO = 76
EXIT_USAGE = 2


def become(command: list[str], *, windows: bool) -> int:
    """Run ``command`` as this process's successor, and return its exit code.

    POSIX replaces this process (``execvp``), so the command IS the process the
    runner recorded and this call does not return. Windows has no ``exec``: the
    command runs as a child of this process, which therefore stays the recorded
    ancestor, and its exit code is returned. Recovery relies on that parentage.
    """
    if windows:
        return subprocess.call(command)
    os.execvp(command[0], command)  # does not return


def main(argv: list[str]) -> int:
    if len(argv) < 6 or argv[4] != "--":
        print("usage: child_exec.py <run-dir> <runner-pid> <runner-ctime> -- <argv...>", file=sys.stderr)
        return EXIT_USAGE
    run_dir = Path(argv[1])
    runner = proctree.ProcIdent(int(argv[2]), float(argv[3]))
    command = argv[5:]
    me = proctree.ident_of(os.getpid())
    if me is None:
        raise RuntimeError(f"child_exec cannot read its own process identity (pid {os.getpid()})")
    runfiles.write_json(run_dir / runfiles.CHILD_FILE, {"pid": me.pid, "create_time": me.create_time, "runner": runner.to_json()})

    deadline = time.monotonic() + GO_TIMEOUT_S
    while not (run_dir / runfiles.GO_FILE).is_file():
        if proctree.live(runner) is None:
            print("child_exec: the runner is gone; not running the command", file=sys.stderr)
            return EXIT_RUNNER_GONE
        if time.monotonic() >= deadline:
            print("child_exec: no go from the runner; not running the command", file=sys.stderr)
            return EXIT_NO_GO
        time.sleep(POLL_S)
    if proctree.live(runner) is None:
        print("child_exec: the runner is gone; not running the command", file=sys.stderr)
        return EXIT_RUNNER_GONE

    return become(command, windows=procspawn.WINDOWS)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
