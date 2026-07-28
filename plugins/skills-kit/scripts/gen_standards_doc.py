"""Regenerate the generated section of configuring-standards.md from its
single sources of truth: skills_kit_lib/rule_catalog.py (rule ids, buckets,
descriptions) and skills_kit_lib/audit.py THRESHOLDS (threshold defaults).

    uv run python plugins/skills-kit/scripts/gen_standards_doc.py          # rewrite in place
    uv run python plugins/skills-kit/scripts/gen_standards_doc.py --check  # exit 1 on drift, no writes

The generated region is delimited by BEGIN_MARK / END_MARK inside the doc;
hand-written prose outside the markers is never touched.
tests/skills-kit/test_standards_doc_drift.py runs --check in CI.
"""

from __future__ import annotations

import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT))

from skills_kit_lib import rule_catalog  # noqa: E402
from skills_kit_lib.audit import THRESHOLDS  # noqa: E402

DOC = PLUGIN_ROOT / "skills" / "md-audit" / "references" / "configuring-standards.md"

BEGIN_MARK = "<!-- BEGIN GENERATED: rule-catalog (gen_standards_doc.py; SSOT: rule_catalog.py + audit.py THRESHOLDS) -->"
END_MARK = "<!-- END GENERATED: rule-catalog -->"

_NUMBER_WORDS = {
    2: "Two", 3: "Three", 4: "Four", 5: "Five", 6: "Six", 7: "Seven",
    8: "Eight", 9: "Nine", 10: "Ten",
}


def _rows(bucket: str) -> list[str]:
    """Table rows for one bucket, in RULES declaration order (grouped)."""
    return [
        f"| `{rid}` | {desc} |"
        for rid, (b, _group, desc) in rule_catalog.RULES.items()
        if b == bucket
    ]


def render() -> str:
    lines: list[str] = [BEGIN_MARK, ""]

    lines += [
        "### Architectural -- never configurable",
        "",
        "The structural contract. These rules have no config knob and are extended only",
        "through `audit-framework.yaml`.",
        "",
        "| Rule id | What it checks |",
        "|---------|----------------|",
        *_rows("architectural"),
        "",
        "### Optional -- disableable via `rules: {<id>: off}`",
        "",
        "| Rule id | Meaning |",
        "|---------|---------|",
        *_rows("optional"),
        "",
        "### Inoffensive -- no knob",
        "",
        "Mechanical integrity checks. The razor: disabling one could never make a correct",
        "document, so they carry no config knob.",
        "",
        "| Rule id | What it checks |",
        "|---------|----------------|",
        *_rows("inoffensive"),
        "",
        "## Thresholds",
        "",
        f"{_NUMBER_WORDS.get(len(THRESHOLDS), str(len(THRESHOLDS)))} named thresholds"
        " carry the numeric limits some rules apply. Override any of",
        "them in `thresholds:`; an override must be a positive integer.",
        "",
        "| Threshold | Default | Consumed by |",
        "|-----------|---------|-------------|",
        *[
            f"| `{name}` | {THRESHOLDS[name]} | {rule_catalog.THRESHOLD_CONSUMERS[name]} |"
            for name in THRESHOLDS
        ],
        "",
        END_MARK,
    ]
    return "\n".join(lines)


def splice(doc_text: str) -> str:
    begin = doc_text.index(BEGIN_MARK)
    end = doc_text.index(END_MARK) + len(END_MARK)
    return doc_text[:begin] + render() + doc_text[end:]


def main(argv: list[str]) -> int:
    check = "--check" in argv
    current = DOC.read_text(encoding="utf-8")
    if BEGIN_MARK not in current or END_MARK not in current:
        print(f"markers not found in {DOC}", file=sys.stderr)
        return 2
    updated = splice(current)
    if updated == current:
        print("configuring-standards.md: generated section up to date")
        return 0
    if check:
        print(
            "configuring-standards.md: generated section is STALE -- run "
            "gen_standards_doc.py to regenerate",
            file=sys.stderr,
        )
        return 1
    DOC.write_text(updated, encoding="utf-8", newline="\n")
    print(f"regenerated {DOC}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
