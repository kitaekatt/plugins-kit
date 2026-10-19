"""task_hold.py -- the dashboard handshake the task skill runs before ``work``.

    task_hold.py <task-ref> [--root PATH] [--session ID]

Registers an interactive hold with a running task-kit dashboard so it does not
start an autonomous run on this task while the Claude Code session lives. It
prints at most ONE line, on stderr, and always exits 0: it can never block
``/task work``. With no dashboard running it prints nothing. See
``task_system/dashboard/handshake.py`` for the full contract.

task.py never imports this script or the dashboard package.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from bootstrap_guard import reexec_under_plugin_venv

reexec_under_plugin_venv("task-kit")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from task_system.dashboard import handshake  # noqa: E402


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="task_hold.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("ref", help="the task ref being worked (as passed to `task.py work`)")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="project root (default: the current directory)")
    parser.add_argument("--session", default=None, help="the Claude Code session id (default: $CLAUDE_SESSION_ID)")
    parser.add_argument("--data-dir", type=Path, default=None,
                        help="task-kit's data directory (default: derived from the running venv)")
    parser.add_argument("--session-pid", type=int, default=None,
                        help="the Claude Code process id (default: found among this command's ancestors)")
    args = parser.parse_args(argv)
    return handshake.run(args.ref, args.root.resolve(), session=args.session, data_dir=args.data_dir,
                         session_pid=args.session_pid)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
