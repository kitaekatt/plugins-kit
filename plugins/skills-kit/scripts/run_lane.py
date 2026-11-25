#!/usr/bin/env python3
"""run_lane.py -- run one md-domain lane on a codex harness entry.

The md-lane-bridge executor agent runs this once per codex lane, with the
lane payload on stdin and the digest the lane script computed over it:

    "${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" \
      "${SKILLS_KIT_ROOT:?requires a bootstrap engine pass; run bootstrap run}/scripts/run_lane.py" \
      --input-digest <d> <<'EOF'
    {"v": 1, "entry": {...}, "access": "...", "prompt": "...", "schema": {...}}
    EOF

A direct caller (no Workflow tool) runs it on a file lane-prompt.cjs wrote:

    "${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" \
      "${SKILLS_KIT_ROOT:?requires a bootstrap engine pass; run bootstrap run}/scripts/run_lane.py" \
      --input-file <i>.lane.json --entry luna --effort high

It prints one JSON status line on stdout and writes no file. Payload, sandbox
policy, output and exit codes: skills_kit_lib/lane_bridge.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))


def main(argv: list[str] | None = None) -> int:
    from skills_kit_lib import lane_bridge

    return lane_bridge.main(argv)


if __name__ == "__main__":
    # llm_kit is installed in the skills-kit plugin venv only. The vendored
    # guard is stdlib-only; it re-execs under that venv (a no-op when already
    # there) before main() imports llm_kit.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from bootstrap_guard import reexec_under_plugin_venv

    reexec_under_plugin_venv("skills-kit")
    raise SystemExit(main())
