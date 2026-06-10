#!/usr/bin/env python3
"""discover.py -- enumerate SKILL.md files visible from the current working directory.

Usage:
    python discover.py
    python discover.py --json

Walks downward from cwd up to a depth limit, collecting all SKILL.md files.
Outputs a numbered list (or JSON) with the skill name and declared type when
visible from frontmatter.

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


def collect_skill_md(cwd: Path) -> list[tuple[Path, str, str]]:
    """Walk cwd downward; return (path, name, skill_type) tuples."""
    out: list[tuple[Path, str, str]] = []
    for current_path, files in iter_dirs(cwd, DESCEND_MAX_DEPTH):
        if "SKILL.md" in files:
            path = current_path / "SKILL.md"
            try:
                content = path.read_text(encoding="utf-8")
            except Exception:
                content = ""
            fm = parse_frontmatter(content)
            fields = fm.fields if fm is not None else {}
            out.append((path, fields.get("name", "?"), fields.get("skill-type", "?")))
    out.sort(key=lambda x: str(x[0]))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument("--cwd", default=None, help="override cwd (for testing)")
    args = parser.parse_args()

    cwd = Path(args.cwd).resolve() if args.cwd else Path.cwd().resolve()
    results = collect_skill_md(cwd)

    if args.json:
        print(json.dumps([{"index": i + 1, "path": str(p), "name": name, "skill_type": skill_type}
                          for i, (p, name, skill_type) in enumerate(results)], indent=2))
        return 0

    if not results:
        print(f"No SKILL.md files found under {cwd}.")
        return 0

    print(f"SKILL.md files visible from {cwd}:\n")
    for i, (path, name, skill_type) in enumerate(results, start=1):
        try:
            display = path.relative_to(cwd)
        except ValueError:
            display = path
        print(f"  {i:>3}. [{skill_type:<18}] {name:<24} {display}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
