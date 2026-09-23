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


import pytest


@pytest.fixture(autouse=True)
def _hermetic_declaration_reads(monkeypatch: pytest.MonkeyPatch) -> list:
    """Keep describe() off this machine's registry, CLIs and pinned verdicts.

    Selection runs through llm_scripting_kit.declaration.describe, which by
    default reads the merged model registry, probes each candidate's CLI or
    HTTP endpoint, and (on an observed quota halt) writes the session's pinned
    verdict. None of that may reach the developer's real state from a test:
    the registry defaults to empty, every probe answers "no verdict" (which
    describe treats as usable, fail-open), and verdict write-back is recorded
    in the returned list instead of written. A test that needs entries, a
    probe verdict or the write-back patches the same attributes itself.
    """
    import llm_scripting_kit.declaration as declaration
    import llm_scripting_kit.models as models
    import llm_scripting_kit.usage_budget as usage_budget

    written: list = []
    monkeypatch.setattr(models, "discover_model_entries", lambda **_: {})
    monkeypatch.setattr(declaration, "check_many", lambda *_a, **_k: {})

    def record(entry_id, spec, **kwargs):
        written.append((entry_id, spec, kwargs))
        return None

    monkeypatch.setattr(usage_budget, "record_observed_halt", record)
    return written
