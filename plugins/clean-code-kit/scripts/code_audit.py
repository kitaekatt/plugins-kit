"""clean-code-kit code audit: run metric tools over a git repository and write a report.

Usage:
    code_audit.py run [--target DIR] [--lane NAME ...] [--snapshot head|working-tree]
    code_audit.py lanes

Exit codes: 0 report written; 1 a tool failed; 2 usage, config or environment refusal.
The report directory is printed as the last stdout line, ``REPORT <dir>``.
"""
from __future__ import annotations

import sys
from pathlib import Path

# The metric tools live in the plugin's bootstrap-provisioned venv. Under a
# claudx session (CLAUDE_BOOTSTRAP_DATA_ROOT set) the guard re-execs into the
# REDIRECTED venv and exits 2 when that venv is missing. The vendored,
# stdlib-only guard and the clean_code_kit package sit next to this script;
# the directory is put on sys.path explicitly so the imports also resolve when
# this file is loaded by path rather than run as a script.
_SCRIPT_DIR = str(Path(__file__).resolve().parent)
if _SCRIPT_DIR not in sys.path:
    sys.path.append(_SCRIPT_DIR)

from bootstrap_guard import reexec_under_plugin_venv  # noqa: E402

reexec_under_plugin_venv("clean-code-kit")

from clean_code_kit.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
