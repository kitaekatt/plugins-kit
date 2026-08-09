#!/usr/bin/env python3
"""discover_skill.py -- enumerate the `skill` artifact's subjects under the cwd.

Usage:
    python discover_skill.py
    python discover_skill.py --json
    python discover_skill.py --references

Walks downward from cwd up to a depth limit, collecting all SKILL.md files.
Outputs a numbered list (or JSON) with the skill name and declared type when
visible from frontmatter.

The `audit_skill` lane has TWO subject shapes -- the SKILL.md contract root and
the skill's own `references/*.md` documents (skill-standards.md section 10).
`--references` adds the reference documents to the listing, each attributed to
its owning skill; without it the listing is SKILL.md files only, which is what
the skill-roster inventory wants.

No third-party dependencies (the shared walk + frontmatter parser come from
the plugin's own skills_kit_lib).
"""

import argparse
import json
import sys
from pathlib import Path

# The shared walk + frontmatter parser live in skills_kit_lib; make the plugin
# root importable regardless of which interpreter/venv launched this script.
_PLUGIN_ROOT = Path(__file__).resolve().parents[3]
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from skills_kit_lib.dirwalk import iter_dirs  # noqa: E402
from skills_kit_lib.markdown_heuristics import parse_frontmatter  # noqa: E402


DESCEND_MAX_DEPTH = 8


def collect_skill_md(cwd: Path, include_references: bool = False) -> list[tuple[Path, str, str, str]]:
    """Walk cwd downward; return (path, name, skill_type, kind) tuples.

    `kind` is the lane's artifact classification -- "skill" for a SKILL.md,
    "skill_reference" for a document under that skill's references/ folder.
    Reference documents are attributed to their owning skill and carry its
    name, so a listing groups by skill without a second lookup.
    """
    out: list[tuple[Path, str, str, str]] = []
    for current_path, files in iter_dirs(cwd, DESCEND_MAX_DEPTH):
        if "SKILL.md" not in files:
            continue
        path = current_path / "SKILL.md"
        try:
            content = path.read_text(encoding="utf-8")
        except Exception:
            content = ""
        fm = parse_frontmatter(content)
        fields = fm.fields if fm is not None else {}
        name = fields.get("name", "?")
        out.append((path, name, fields.get("skill-type", "?"), "skill"))
        if include_references:
            refs_dir = current_path / "references"
            if refs_dir.is_dir():
                for ref in sorted(refs_dir.rglob("*.md")):
                    out.append((ref, name, "(skill reference)", "skill_reference"))
    out.sort(key=lambda x: str(x[0]))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument("--cwd", default=None, help="override cwd (for testing)")
    parser.add_argument(
        "--references",
        action="store_true",
        help="also list each skill's references/*.md documents (the lane's second subject shape)",
    )
    args = parser.parse_args()

    cwd = Path(args.cwd).resolve() if args.cwd else Path.cwd().resolve()
    results = collect_skill_md(cwd, include_references=args.references)

    if args.json:
        print(json.dumps([{"index": i + 1, "path": str(p), "name": name,
                           "skill_type": skill_type, "kind": kind}
                          for i, (p, name, skill_type, kind) in enumerate(results)], indent=2))
        return 0

    if not results:
        print(f"No SKILL.md files found under {cwd}.")
        return 0

    subjects = "Skill-artifact subjects" if args.references else "SKILL.md files"
    print(f"{subjects} visible from {cwd}:\n")
    for i, (path, name, skill_type, _kind) in enumerate(results, start=1):
        try:
            display = path.relative_to(cwd)
        except ValueError:
            display = path
        print(f"  {i:>3}. [{skill_type:<18}] {name:<24} {display}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
