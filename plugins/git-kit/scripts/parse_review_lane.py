#!/usr/bin/env python3
"""Bootstrap and parse one native code-review lane's JSON output."""

from __future__ import annotations

import re
import sys
from pathlib import Path

from bootstrap_guard import reexec_under_plugin_venv

_VERSION_DIR = re.compile(r"^\d+\.\d+\.\d+")


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
    from bootstrap_lib.code_review.lane_output import main
except ImportError:
    from bootstrap_guard import require_bootstrap

    require_bootstrap(
        _PLUGIN_NAME,
        feature="code review output parsing",
        missing="bootstrap_lib.code_review.lane_output",
        force=True,
    )
    raise


if __name__ == "__main__":
    sys.exit(main())
