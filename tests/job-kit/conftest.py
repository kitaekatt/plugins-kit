"""Import the job-kit package and its shared completion source for tests."""

import os
from pathlib import Path
import sys


# The CLI entry-point shim re-execs into its plugin venv in a real invocation.
# Keep pytest from replacing its own process if a test calls that shim.
os.environ.setdefault("_BOOTSTRAP_GUARD_VENV_REEXEC", "1")


REPO_ROOT = Path(__file__).resolve().parents[2]
for source in (
    REPO_ROOT / "plugins" / "job-kit" / "lib",
    REPO_ROOT / "plugins" / "llm-scripting-kit" / "lib",
    REPO_ROOT / "plugins" / "bootstrap",
):
    text = str(source)
    if text not in sys.path:
        sys.path.insert(0, text)
