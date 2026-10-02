#!/usr/bin/env python3
"""resolve_standards.py -- CLI wrapper over skills_kit_lib.standards_resolve.

The md-domain audit lanes (skill, claude-md, project-doc) call this once per
run, via the plugin venv python, to obtain the resolved standards
configuration for the artifact type they audit:

  - the disabled optional-rule/criterion ids and threshold overrides (threaded
    into the detect lanes as `disabledCriteria` and used by audit.py --config);
  - the applicable *-standards.md file paths per file-type primitive
    (threaded per-file as `standardsPaths`).

Usage:
    python resolve_standards.py --project-root <dir> [--primitive <name> ...]

--primitive is repeatable and filters the `standards` map to the named
file-type primitives (skill_md, claude_md, reference_doc, plain_md); omit it to
return every subject that has standards. A name no lane consumes authored
standards for (a composition id in APPLIES_TO_NOT_CONSUMED, such as
code_directory) or an unknown name is a usage error (exit 2): its answer could
only ever be an empty list, which reads exactly like "nothing authored".
Prints one JSON object:

    {
      "disabled":   ["<rule-id>", ...],
      "thresholds": {"<name>": <int>, ...},
      "standards":  {"<primitive>": ["<abs path>", ...], ...},
      "audit":      {"fix_mode": "apply"|"propose"},
      "lane_models": {"<family>": {"declared": [{"id", "effort"}, ...],
                                   "run":      [{"id", "effort"}, ...],
                                   "dropped":  [{"id", "effort", "reason"}, ...]}},
      "notes":      ["<loud-but-non-fatal diagnostic>", ...]
    }

`lane_models` covers the agent-only lane families (detect, classify, coverage,
generate, remediate): the layered `lane_models` slot resolved by
skills_kit_lib.lane_models, routed through its agent_route. A dropped entry is
not an error; a family with nothing runnable, an incomplete or malformed slot,
or an unimportable bootstrap_lib exits 1 like any other config error.

Stdlib-only argument handling; the actual resolution (pyyaml + schema
validation) lives in skills_kit_lib.standards_resolve. Exit 1, with one line on
stderr and nothing on stdout, when a layer is malformed or when pyyaml is not
importable by the launching interpreter -- the latter names the skills-kit
plugin venv to run under instead.
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


#: The --primitive values that can carry authored standards.
_PRIMITIVE_CHOICES = tuple(
    s for s in standards_resolve.APPLIES_TO_SUBJECTS
    if s not in standards_resolve.APPLIES_TO_NOT_CONSUMED
)


def _check_primitive(parser: argparse.ArgumentParser, name: str) -> None:
    """Reject a --primitive whose answer could only ever be an empty list."""
    canonical = standards_resolve.APPLIES_TO_ALIASES.get(name, name)
    if canonical in standards_resolve.APPLIES_TO_NOT_CONSUMED:
        parser.error(
            f"--primitive {name}: no lane consumes authored standards for "
            f"'{canonical}', so its standards list is always empty; valid "
            f"values: {', '.join(_PRIMITIVE_CHOICES)}"
        )
    if canonical not in standards_resolve.APPLIES_TO_SUBJECTS:
        parser.error(
            f"--primitive {name}: unknown subject; valid values: "
            f"{', '.join(_PRIMITIVE_CHOICES)}"
        )


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
        help="Restrict the `standards` map to this file-type primitive "
             f"({', '.join(_PRIMITIVE_CHOICES)}). Repeatable; omit to return "
             "every subject that has standards.",
    )
    args = parser.parse_args(argv)
    for name in args.primitive or ():
        _check_primitive(parser, name)

    project_root = Path(args.project_root).expanduser()
    try:
        resolved = standards_resolve.resolve(project_root)
    except standards_resolve.StandardsConfigError as exc:
        # A malformed layer, an un-tunable id, or a missing pyyaml
        # (StandardsUnavailableError) is a loud, actionable error -- surface it
        # on stderr and fail rather than emitting a partial or default config.
        print(f"resolve_standards: {exc}", file=sys.stderr)
        return 1

    # lane_models needs bootstrap_lib (EFFORT_LEVELS, model_declaration), which
    # the skills-kit plugin venv links; standards_resolve stays free of it.
    try:
        from skills_kit_lib import lane_models
    except ImportError as exc:
        print(
            f"resolve_standards: cannot resolve lane_models: {exc}. Run under "
            "the skills-kit plugin venv, which links bootstrap_lib.",
            file=sys.stderr,
        )
        return 1
    try:
        routes = lane_models.agent_routes(lane_models.load_lane_models(project_root))
    except lane_models.LaneModelsError as exc:
        print(f"resolve_standards: lane_models: {exc}", file=sys.stderr)
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
        "audit": dict(resolved.audit),
        "lane_models": routes,
        "notes": list(resolved.notes),
    }
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    # lane_models imports bootstrap_lib, which only the skills-kit plugin venv
    # links. The vendored guard is stdlib-only; it re-execs under that venv (a
    # no-op when already there) before main() imports lane_models.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from bootstrap_guard import reexec_under_plugin_venv

    reexec_under_plugin_venv("skills-kit")
    raise SystemExit(main())
