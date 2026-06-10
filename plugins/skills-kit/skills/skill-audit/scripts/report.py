#!/usr/bin/env python3
"""report.py -- dispatch entry point for the skill-audit reports (/md-audit skill).

Two reports are available; the user picks one by positional argument:

  /md-audit skill roster      Markdown roster grouped by location -> type. Per-type
                               implied frontmatter declared once so per-skill rows
                               don't repeat it. Default: <project-root>/tmp/skill-roster.md.

  /md-audit skill hierarchy   Interactive HTML hierarchy. Collapsible <details>
                               sections, one column per frontmatter key, skill-type
                               hover tooltips. Default: <project-root>/tmp/skill-hierarchy.html.

With no arguments, prints this usage block and exits.

A trailing path or `-` selects the output destination:

  /md-audit skill roster                       -> default tmp/ path
  /md-audit skill roster path/to/file.md       -> that path
  /md-audit skill roster -                     -> stdout

Discovery is shared via the plugin-level `skills_kit_lib.corpus` module so both
reports enumerate the corpus the same way.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

from skills_kit_lib.corpus import (
    PluginEntry,
    SkillCorpus,
    SkillRecord,
    detect_skill_type,
    discover_corpus,
)


REPORT_TYPES = ("roster", "hierarchy")
DEFAULT_FILENAME = {
    "roster": "skill-roster.md",
    "hierarchy": "skill-hierarchy.html",
}


# ----------------------------------------------------------------------------
# Roster (markdown) renderer
# ----------------------------------------------------------------------------


def implied_flags(skill_type: str, variant: str) -> dict:
    """Frontmatter values implied by the (type, variant) contract.

    Per-skill rows surface a flag only when its value differs from this map.
    """
    if skill_type == "technique-skill" and variant == "user-only":
        return {"disable-model-invocation": True, "user-invocable": True}
    return {"disable-model-invocation": False, "user-invocable": False}


def fmt_flag(name: str, value) -> str:
    return f"`{name}: {str(value).lower()}`"


def group_by_type(records: list[SkillRecord]) -> OrderedDict:
    """Group records by (skill_type, variant); each group sorted by name."""
    bucket: dict[tuple[str, str], list[SkillRecord]] = {}
    for rec in records:
        bucket.setdefault(detect_skill_type(rec), []).append(rec)
    for lst in bucket.values():
        lst.sort(key=lambda r: ((r.frontmatter.get("name") or r.path.name)).lower())
    ordered: OrderedDict = OrderedDict()
    for k in sorted(bucket.keys()):
        ordered[k] = bucket[k]
    return ordered


def render_skill_row(rec: SkillRecord, implied: dict) -> list[str]:
    fm = rec.frontmatter or {}
    name = fm.get("name") or rec.path.name
    desc = str(fm.get("description") or "").strip()
    author = fm.get("author")

    tag = f" [author: {author}]" if author else ""
    line = f"- **{name}**{tag}"
    if desc:
        line += f" -- {desc}"
    rows = [line]

    extras: list[str] = []
    for flag in ("disable-model-invocation", "user-invocable"):
        if flag in fm:
            actual = bool(fm[flag])
            expected = bool(implied.get(flag, False))
            if actual != expected:
                extras.append(fmt_flag(flag, fm[flag]))
    if extras:
        rows.append(f"  - {', '.join(extras)}")
    return rows


def render_roster(corpus: SkillCorpus) -> str:
    sections: OrderedDict[str, OrderedDict] = OrderedDict()
    sections["User (~/.claude/skills)"] = group_by_type(corpus.user)
    if corpus.project_skills_root is not None:
        sections[f"Project ({corpus.project_skills_root})"] = group_by_type(
            corpus.project
        )
    for plugin in corpus.plugins:
        if not plugin.skills:
            continue
        label = f"Plugin: {plugin.name} ({plugin.marketplace}, v{plugin.version})"
        sections[label] = group_by_type(plugin.skills)

    lines: list[str] = ["# Skill Roster", ""]
    lines.append(f"Generated {datetime.now().isoformat(timespec='seconds')}")
    lines.append("")

    for loc_name, groups in sections.items():
        lines.append(f"## {loc_name}")
        lines.append("")
        if not groups:
            lines.append("(no skills found)")
            lines.append("")
            continue
        for (skill_type, variant), skills in groups.items():
            heading = skill_type if not variant else f"{skill_type} ({variant})"
            lines.append(f"### {heading}")
            lines.append("")
            implied = implied_flags(skill_type, variant)
            implied_pairs = [fmt_flag(k, v) for k, v in implied.items() if v]
            if implied_pairs:
                lines.append(f"_Implied frontmatter: {', '.join(implied_pairs)}_")
                lines.append("")
            for rec in skills:
                lines.extend(render_skill_row(rec, implied))
            lines.append("")

    return "\n".join(lines)


# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------


USAGE = """\
report.py -- corpus-wide report dispatcher for the skill-audit skill
(reached via /md-audit skill). Pick a subcommand:

  roster [path|-]      Markdown roster grouped by location and type.
                       Per-type implied frontmatter is declared once
                       so per-skill rows don't repeat it.
                       Default output: <project-root>/tmp/skill-roster.md

  hierarchy [path|-]   Interactive HTML hierarchy with collapsible
                       sections, one column per frontmatter key,
                       and skill-type hover tooltips.
                       Default output: <project-root>/tmp/skill-hierarchy.html

Trailing `-` writes the report body to stdout instead of a file.

This script is normally invoked by the skill-audit skill (via /md-audit
skill). For single-skill contract audits, see the audit_skill_md technique
in the skill-audit SKILL.md (different code path: scripts/discover.py + the
skills_kit_lib.audit validator).
"""


def _force_utf8_stdout() -> None:
    """Reconfigure stdout to UTF-8 so descriptions containing em-dashes / smart
    quotes / etc. don't mojibake on Windows consoles (default cp1252)."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass


def main() -> int:
    _force_utf8_stdout()

    # No-args case: print the usage block and exit 0 (informational).
    if len(sys.argv) == 1:
        sys.stdout.write(USAGE)
        return 0

    parser = argparse.ArgumentParser(
        description="Generate a skill inventory report.",
        usage="report.py {roster|hierarchy} [out] [--cwd DIR]",
    )
    parser.add_argument(
        "report_type",
        choices=REPORT_TYPES,
        help="Which report to produce.",
    )
    parser.add_argument(
        "out",
        nargs="?",
        default=None,
        help="Output path, or '-' for stdout. Default: <project-root>/tmp/skill-<type>.{md,html}.",
    )
    parser.add_argument("--cwd", default=os.getcwd(), help="Project root (default: cwd).")
    args = parser.parse_args()

    project_root = Path(args.cwd).resolve()
    corpus = discover_corpus(project_root=project_root)

    if args.report_type == "hierarchy":
        # Sibling module under skills-kit/skills/skill-audit/scripts/.
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from skill_hierarchy_report import render_html  # type: ignore  # noqa: E402
        text = render_html(corpus)
    else:
        text = render_roster(corpus)

    if args.out == "-":
        sys.stdout.write(text)
        sys.stdout.write("\n")
        return 0

    default_path = project_root / "tmp" / DEFAULT_FILENAME[args.report_type]
    out_path = Path(args.out).resolve() if args.out else default_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    skill_bearing = [p for p in corpus.plugins if p.skills]
    print(f"Wrote {out_path}")
    print(f"  user skills:    {len(corpus.user)}")
    print(f"  project skills: {len(corpus.project)}")
    print(f"  plugins:        {len(skill_bearing)} "
          f"({sum(len(p.skills) for p in skill_bearing)} skills)")
    print(f"  total:          {corpus.total_skills}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
