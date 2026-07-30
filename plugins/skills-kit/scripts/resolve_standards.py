#!/usr/bin/env python3
"""resolve_standards.py -- CLI wrapper over skills_kit_lib.standards_resolve.

The md-domain audit lanes (skill, claude-md, project-doc) call this once per
run, via the plugin venv python, to obtain the resolved standards
configuration for the artifact type they audit:

  - the disabled optional-rule/criterion ids and threshold overrides (threaded
    into the detect lanes as `disabledCriteria` and used by audit.py --config);
  - the applicable *-standards.md file paths per audit-framework primitive
    (threaded per-file as `standardsPaths`).

Usage:
    python resolve_standards.py --project-root <dir> [--primitive <name> ...]

--primitive is repeatable and filters the `standards` map to the named
primitives (skill_md, claude_md, reference_doc, plain_md); omit it to return
every primitive that has standards. Prints one JSON object:

    {
      "disabled":   ["<rule-id>", ...],
      "thresholds": {"<name>": <int>, ...},
      "standards":  {"<primitive>": ["<abs path>", ...], ...},
      "notes":      ["<loud-but-non-fatal diagnostic>", ...]
    }

Stdlib-only argument handling; the actual resolution (pyyaml + schema
validation) lives in skills_kit_lib.standards_resolve, which degrades to empty
defaults plus a note when pyyaml is unavailable.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# The resolver lives in skills_kit_lib; make the plugin root importable
# regardless of which interpreter/venv launched this script (same pattern as
# skills/*/scripts/discover.py).
_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from skills_kit_lib import standards_resolve  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Resolve the layered skills-kit standards config to JSON.",
    )
    parser.add_argument(
        "--project-root",
        required=True,
        help="Project root whose <root>/.claude/skills-kit/ layer is resolved "
             "(usually the audit's cwd / nearest project root).",
    )
    parser.add_argument(
        "--primitive",
        action="append",
        default=None,
        metavar="NAME",
        help="Restrict the `standards` map to this audit-framework primitive "
             "(skill_md, claude_md, reference_doc, plain_md). Repeatable; "
             "omit to return every primitive that has standards.",
    )
    args = parser.parse_args(argv)

    project_root = Path(args.project_root).expanduser()
    try:
        resolved = standards_resolve.resolve(project_root)
    except standards_resolve.StandardsConfigError as exc:
        # A malformed layer or an un-tunable id is a loud, actionable error --
        # surface it on stderr and fail rather than emitting a partial config.
        print(f"resolve_standards: {exc}", file=sys.stderr)
        return 1

    by_primitive = resolved.standards_by_primitive
    if args.primitive:
        wanted = args.primitive
    else:
        wanted = sorted(by_primitive)
    standards = {
        prim: [str(sf.path) for sf in by_primitive.get(prim, [])]
        for prim in wanted
    }

    out = {
        "disabled": sorted(resolved.disabled_rules),
        "thresholds": dict(resolved.thresholds),
        "standards": standards,
        "notes": list(resolved.notes),
    }
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
