"""Import the job-kit package and its shared completion source for tests."""

from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[2]
for source in (
    REPO_ROOT / "plugins" / "job-kit" / "lib",
    REPO_ROOT / "plugins" / "llm-scripting-kit" / "lib",
):
    text = str(source)
    if text not in sys.path:
        sys.path.insert(0, text)
