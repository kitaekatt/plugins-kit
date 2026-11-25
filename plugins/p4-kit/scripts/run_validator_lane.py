#!/usr/bin/env python3
"""Bootstrap and run one code-review validator on a codex harness entry.

This thin wrapper is byte-identical in git-kit and p4-kit. The implementation
is bootstrap_lib.code_review.validator_lane, which reaches the pinned
katsoft-llm-kit package in this plugin's venv through its completion CLI.
Exit codes: 0 verdict line on stdout, 1 call failed, 2 refused as configured,
3 quota halt, 4 answered without a valid verdict (the issue stays unvalidated).
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
from pathlib import Path

from bootstrap_guard import reexec_under_plugin_venv

_VERSION_DIR = re.compile(r"^\d+\.\d+\.\d+")
_EXIT_REFUSED = 2


def _plugin_name() -> str:
    """Return the owning plugin in development and installed layouts."""
    for part in reversed(Path(__file__).resolve().parts[:-1]):
        if part == "scripts" or _VERSION_DIR.match(part):
            continue
        return part
    return Path(__file__).resolve().parents[1].name


_PLUGIN_NAME = _plugin_name()
reexec_under_plugin_venv(_PLUGIN_NAME)

try:
    from bootstrap_lib.code_review.validator_lane import main
except ImportError:
    from bootstrap_guard import require_bootstrap

    require_bootstrap(
        _PLUGIN_NAME,
        feature="code review validation",
        missing="bootstrap_lib.code_review.validator_lane",
        force=True,
    )
    raise

if importlib.util.find_spec("llm_kit") is None:
    print(
        "the katsoft-llm-kit package is not installed in this plugin's venv, "
        "so this codex validator cannot start. Bootstrap installs it from the "
        "plugin's pyproject.toml; run `bootstrap run` (or start a new session) "
        "so bootstrap provisions the venv. An Agent validator needs no llm_kit.",
        file=sys.stderr,
    )
    raise SystemExit(_EXIT_REFUSED)

if __name__ == "__main__":
    # The codex child starts in the review's project root. Without this, its
    # bootstrap hook provisions that directory as a project (bootstrap's
    # BOOTSTRAP_NO_PASS gate, session-bootstrap.sh).
    os.environ["BOOTSTRAP_NO_PASS"] = "1"
    sys.exit(main())
