"""classify -- infer a SKILL.md's type from its YAML contract or content shape.

Usage:
    python -m skills_kit_lib.classify <path-to-SKILL.md>
    python -m skills_kit_lib.classify <path-to-SKILL.md> --json

Two-path classification:

1. YAML-contract path (preferred): if the SKILL.md carries a fenced YAML
   block with a recognized contract root key, that root key is the
   deterministic type. Multiple roots = mixed-type.

2. Heuristic fallback: for legacy / not-yet-migrated skills without a
   YAML contract block, score the body against each canonical type.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from .document_walker import HAVE_YAML, iter_yaml_blocks, safe_load_block
from .markdown_heuristics import parse_body, parse_frontmatter, type_signals
from .schema_registry import SKILL_TYPE_ROOTS


MIXED_THRESHOLD = 2

# Contract-root -> dashed type name, derived from the registry's skill-type
# roots (single source of truth; do not restate the type list here).
CONTRACT_ROOT_TO_TYPE = {root: root.replace("_", "-") for root in SKILL_TYPE_ROOTS}


def extract_yaml_roots(body_text: str) -> list[str]:
    """Return the list of canonical contract root keys present in any fenced
    YAML block in the body. Block recognition is document_walker's (the one
    canonical fence regex); without pyyaml the roots are regex-detected
    inside each block.
    """
    roots: list[str] = []
    for text in iter_yaml_blocks(body_text):
        data = safe_load_block(text)
        if data is not None:
            for root in CONTRACT_ROOT_TO_TYPE:
                if root in data and root not in roots:
                    roots.append(root)
        elif not HAVE_YAML:
            for root in CONTRACT_ROOT_TO_TYPE:
                if re.search(rf"^{root}\s*:", text, re.MULTILINE) and root not in roots:
                    roots.append(root)
    return roots


def classify(skill_md_path: Path) -> dict:
    if not skill_md_path.exists():
        return {"error": f"file not found: {skill_md_path}"}
    content = skill_md_path.read_text(encoding="utf-8")
    fm = parse_frontmatter(content)
    body = parse_body(content)

    declared = fm.fields.get("skill-type") if fm else None
    yaml_roots = extract_yaml_roots(body.text)

    if len(yaml_roots) >= 2:
        canonical_types = [CONTRACT_ROOT_TO_TYPE[r] for r in yaml_roots]
        return {
            "path": str(skill_md_path),
            "declared_type": declared,
            "suggested_type": None,
            "verdict": "mixed-type",
            "reason": (
                f"YAML contract block contains multiple type roots: "
                + ", ".join(yaml_roots)
                + ". Split the skill along type boundaries."
            ),
            "source": "yaml-contract",
            "yaml_roots": yaml_roots,
            "canonical_types": canonical_types,
            "scores": {},
        }

    if len(yaml_roots) == 1:
        root = yaml_roots[0]
        suggested = CONTRACT_ROOT_TO_TYPE[root]
        if declared and declared != suggested:
            return {
                "path": str(skill_md_path),
                "declared_type": declared,
                "suggested_type": suggested,
                "verdict": "frontmatter-disagreement",
                "reason": (
                    f"YAML contract root '{root}' implies type '{suggested}', "
                    f"but frontmatter declares skill-type: '{declared}'. "
                    "Align the frontmatter and the YAML root."
                ),
                "source": "yaml-contract",
                "yaml_roots": yaml_roots,
                "scores": {},
            }
        return {
            "path": str(skill_md_path),
            "declared_type": declared,
            "suggested_type": suggested,
            "verdict": "single-type",
            "reason": f"YAML contract root '{root}' identifies type deterministically.",
            "source": "yaml-contract",
            "yaml_roots": yaml_roots,
            "scores": {},
        }

    # No YAML contract block; fall back to heuristic scoring.
    scores = type_signals(body.text, fm)

    sorted_types = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    top_type, top_score = sorted_types[0]
    runner_up_score = sorted_types[1][1] if len(sorted_types) > 1 else 0

    high_scoring = [t for t, s in sorted_types if s >= MIXED_THRESHOLD]
    suggestion = top_type if top_score > 0 else None

    if len(high_scoring) >= 2:
        verdict = "mixed-type"
        reason = (
            f"multiple types score >= {MIXED_THRESHOLD}: "
            + ", ".join(f"{t}={scores[t]}" for t in high_scoring)
        )
    elif top_score == 0:
        verdict = "indeterminate"
        reason = "no canonical-type signals detected"
    elif top_score == runner_up_score:
        verdict = "ambiguous"
        reason = (
            f"top types tie at {top_score}: "
            + ", ".join(f"{t}={scores[t]}" for t, s in sorted_types if s == top_score)
        )
    else:
        verdict = "single-type"
        reason = f"top={top_type} with score={top_score}, runner-up={runner_up_score}"

    return {
        "path": str(skill_md_path),
        "declared_type": declared,
        "suggested_type": suggestion,
        "verdict": verdict,
        "reason": reason,
        "source": "heuristic-fallback",
        "yaml_roots": [],
        "scores": scores,
    }


def render_text(report: dict) -> str:
    if "error" in report:
        return report["error"]
    lines = []
    lines.append(f"classify: {report['path']}")
    lines.append(f"declared_type:  {report['declared_type']}")
    lines.append(f"suggested_type: {report['suggested_type']}")
    lines.append(f"verdict:        {report['verdict']}")
    lines.append(f"source:         {report.get('source', 'heuristic-fallback')}")
    lines.append(f"reason:         {report['reason']}")
    if report.get("yaml_roots"):
        lines.append(f"yaml_roots:     {report['yaml_roots']}")
    if report.get("scores"):
        lines.append("")
        lines.append("scores:")
        for t, s in sorted(report["scores"].items(), key=lambda kv: kv[1], reverse=True):
            lines.append(f"  {t:<20} {s}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Classify a SKILL.md by inferring its type from content shape.",
    )
    parser.add_argument("path", help="Path to SKILL.md")
    parser.add_argument("--json", action="store_true", help="Emit JSON")
    args = parser.parse_args(argv)

    report = classify(Path(args.path))
    if "error" in report:
        print(report["error"], file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
