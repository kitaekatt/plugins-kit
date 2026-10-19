"""Entry point llm-kit runs as the job contract (see acceptance.py).

A separate file so the contract command is a plain script path, independent of
the caller's working directory: it puts the task-system scripts directory on
``sys.path`` and hands over to :mod:`task_system.dashboard.acceptance`.
"""

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from task_system.dashboard.acceptance import main  # noqa: E402

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main(sys.argv[1:], sys.stdin.buffer.read().decode("utf-8", errors="replace")))
