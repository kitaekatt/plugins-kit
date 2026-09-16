#!/usr/bin/env python3
"""Run a skills_kit_lib command (audit, classify, tag) from any directory.

Usage::

    "${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" \\
        "${CLAUDE_PLUGIN_ROOT}/scripts/skills_kit_tool.py" <audit|classify|tag> [args...]

The launcher re-execs under the skills-kit plugin venv, where pyyaml lives.
Without pyyaml the YAML contract checks degrade to judgment-required. It then
puts the plugin root on ``sys.path`` and runs ``skills_kit_lib.<command>`` as
``__main__`` with the remaining arguments, so relative paths in those
arguments resolve against the caller's working directory, not the plugin
root.
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
_PLUGIN_ROOT = _SCRIPTS.parent

COMMANDS = ("audit", "classify", "tag")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in COMMANDS:
        print(f"usage: skills_kit_tool.py {{{'|'.join(COMMANDS)}}} [args...]",
              file=sys.stderr)
        return 2
    command, rest = args[0], args[1:]
    if str(_PLUGIN_ROOT) not in sys.path:
        sys.path.insert(0, str(_PLUGIN_ROOT))
    module = f"skills_kit_lib.{command}"
    sys.argv = [module, *rest]
    try:
        runpy.run_module(module, run_name="__main__", alter_sys=True)
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        print(code, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    # Before any skills_kit_lib import: the vendored guard is stdlib-only.
    sys.path.insert(0, str(_SCRIPTS))
    from bootstrap_guard import reexec_under_plugin_venv

    reexec_under_plugin_venv("skills-kit")
    sys.exit(main())
