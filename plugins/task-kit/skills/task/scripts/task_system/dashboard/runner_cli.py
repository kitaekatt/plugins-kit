"""Entry point the dashboard server spawns as a detached run-owning process.

Usage: ``<python> runner_cli.py <run-dir>``. See runner.py for the contract.
"""

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from task_system.dashboard.runner import run  # noqa: E402

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: runner_cli.py <run-dir>", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(run(Path(sys.argv[1])))
