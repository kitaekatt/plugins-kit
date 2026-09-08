"""tag -- write a `skill-type:` value into SKILL.md frontmatter.

Usage:
    python -m skills_kit_lib.tag <path-to-SKILL.md> <skill-type>
    python -m skills_kit_lib.tag <path-to-SKILL.md> <skill-type> --check

Idempotent. Refuses to overwrite an existing value without --force.
Skills without YAML frontmatter are flagged, never patched.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from .document_walker import HAVE_YAML
from .markdown_heuristics import (
    CANONICAL_TYPES,
    FRONTMATTER_RE,
    parse_frontmatter,
)


SKILL_TYPE_LINE_RE = re.compile(r"^skill-type\s*:\s*(.+?)\s*$", re.MULTILINE)


def tag(skill_md_path: Path, new_type: str, force: bool, check_only: bool) -> dict:
    if not skill_md_path.exists():
        return {"ok": False, "error": f"file not found: {skill_md_path}"}
    if new_type not in CANONICAL_TYPES:
        return {
            "ok": False,
            "error": f"invalid skill-type '{new_type}'; expected one of {sorted(CANONICAL_TYPES)}",
        }

    content = skill_md_path.read_text(encoding="utf-8")
    # mode="full" resolves a quoted/multi-line skill-type value correctly
    # (I1); fall back to light (regex) mode without pyyaml.
    fm = parse_frontmatter(content, mode="full" if HAVE_YAML else "light")
    if fm is None:
        return {
            "ok": False,
            "error": "no YAML frontmatter; flagged for manual authoring (tag never invents frontmatter)",
            "action": "flag",
        }

    current = fm.fields.get("skill-type")
    if current == new_type:
        return {"ok": True, "action": "no-op", "skill-type": current}
    if current is not None and current != new_type and not force:
        return {
            "ok": False,
            "action": "refused",
            "error": f"existing skill-type '{current}' differs from requested '{new_type}'; pass --force to overwrite",
            "current": current,
            "requested": new_type,
        }

    if check_only:
        return {
            "ok": True,
            "action": "would-add" if current is None else "would-replace",
            "current": current,
            "requested": new_type,
        }

    m = FRONTMATTER_RE.match(content)
    fm_block = m.group(1)
    if current is None:
        new_fm_block = fm_block.rstrip() + f"\nskill-type: {new_type}"
    else:
        new_fm_block = SKILL_TYPE_LINE_RE.sub(f"skill-type: {new_type}", fm_block, count=1)

    new_content = content[: m.start(1)] + new_fm_block + content[m.end(1):]
    # Explicit newline="\n": open(mode="w") without it translates "\n" to
    # os.linesep, which rewrites the whole file to CRLF on Windows even when
    # the source was LF (I3; same rationale as gen_standards_doc.py).
    skill_md_path.write_text(new_content, encoding="utf-8", newline="\n")
    return {
        "ok": True,
        "action": "added" if current is None else "replaced",
        "previous": current,
        "current": new_type,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Tag a SKILL.md with a skill-type advisory frontmatter value.",
    )
    parser.add_argument("path", help="Path to SKILL.md")
    parser.add_argument("skill_type", help=f"One of: {sorted(CANONICAL_TYPES)}")
    parser.add_argument("--force", action="store_true", help="Overwrite an existing skill-type value")
    parser.add_argument("--check", action="store_true", help="Report what would happen without writing")
    args = parser.parse_args(argv)

    result = tag(Path(args.path), args.skill_type, args.force, args.check)
    if not result.get("ok"):
        msg = result.get("error", "tag failed")
        print(msg, file=sys.stderr)
        return 1

    action = result.get("action")
    if action == "no-op":
        print(f"no-op: skill-type already '{result['skill-type']}'")
    elif action == "added":
        print(f"added skill-type: {result['current']}")
    elif action == "replaced":
        print(f"replaced skill-type: {result['previous']} -> {result['current']}")
    elif action == "would-add":
        print(f"would add skill-type: {result['requested']}")
    elif action == "would-replace":
        print(f"would replace skill-type: {result['current']} -> {result['requested']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
