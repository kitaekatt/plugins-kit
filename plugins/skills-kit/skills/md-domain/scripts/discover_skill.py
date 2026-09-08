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
import os
import sys
from pathlib import Path
from typing import Iterable

# The shared walk + frontmatter parser live in skills_kit_lib; make the plugin
# root importable regardless of which interpreter/venv launched this script.
_PLUGIN_ROOT = Path(__file__).resolve().parents[3]
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

# Sibling scripts are imported by module name; make this directory importable
# even when this module is loaded by path (importlib) rather than run directly.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from skills_kit_lib.dirwalk import iter_dirs  # noqa: E402
from skills_kit_lib.markdown_heuristics import parse_frontmatter  # noqa: E402

import vcs_ignore  # noqa: E402
from discover_coverage import _is_within  # noqa: E402 -- reuse the same within-root symlink guard


DESCEND_MAX_DEPTH = 8


def _collect_reference_docs(refs_dir: Path, root: Path) -> list[Path]:
    """*.md documents under one skill's references/ directory.

    Goes through the same bounded walk (`iter_dirs`, which -- like every
    sibling discover script's walk -- does not descend into a directory
    symlink encountered mid-walk, since `os.walk`'s default is
    `followlinks=False`) and the same `vcs_ignore.ignored_paths` filtering the
    other discover scripts use, rather than `Path.rglob`, which is unbounded
    and (pathlib's default) DOES follow symlinks.

    `os.walk`'s no-follow default only protects a symlink encountered as a
    subdirectory partway through a walk; it does nothing for `refs_dir`
    itself being a symlink, since that IS the walk's starting point. So this
    checks that first, explicitly, with the identical within-root test
    `walk_tree` uses for the same purpose (`SKIP_SYMLINK_OUT`).
    """
    # Compare RESOLVED against RESOLVED: the caller's root may carry a symlink
    # component (macOS /tmp -> /private/tmp) or be relative, and a resolved
    # refs_dir would then never be "within" the unresolved root -- every
    # skill's references would be dropped as if they were symlinks out.
    try:
        target = refs_dir.resolve()
        root_resolved = Path(root).resolve()
    except (OSError, RuntimeError):
        return []
    if not _is_within(target, root_resolved):
        return []
    found: list[Path] = []
    for current_path, files in iter_dirs(refs_dir, DESCEND_MAX_DEPTH):
        for fname in files:
            if fname.lower().endswith(".md"):
                found.append(current_path / fname)
    if not found:
        return found
    ignored = vcs_ignore.ignored_paths(found, root=refs_dir)
    if ignored:
        found = [p for p in found if p not in ignored]
    found.sort(key=str)
    return found


def collect_skill_md(
    cwd: Path,
    include_references: bool = False,
    include_dirs: Iterable[str] = (),
    skipped_out: list[Path] | None = None,
) -> list[tuple[Path, str, str, str]]:
    """Walk cwd downward; return (path, name, skill_type, kind) tuples.

    `kind` is the lane's artifact classification -- "skill" for a SKILL.md,
    "skill_reference" for a document under that skill's references/ folder.
    Reference documents are attributed to their owning skill and carry its
    name, so a listing groups by skill without a second lookup.

    `include_dirs` and `skipped_out` pass straight through to `iter_dirs` --
    without them a SKILL.md sitting under a noise-named directory (`tmp/`,
    `Build/`) is silently invisible and nothing says so.
    """
    out: list[tuple[Path, str, str, str]] = []
    for current_path, files in iter_dirs(
        cwd, DESCEND_MAX_DEPTH, include_dirs=include_dirs, skipped_out=skipped_out
    ):
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
                for ref in _collect_reference_docs(refs_dir, cwd):
                    out.append((ref, name, "(skill reference)", "skill_reference"))
    out.sort(key=lambda x: str(x[0]))
    return out


INCLUDE_DIRS_ENV = "MD_DOMAIN_INCLUDE_DIRS"


def _resolve_include_dirs(cli_values: list[str] | None) -> list[str]:
    """--include-dir values, falling back to the MD_DOMAIN_INCLUDE_DIRS
    environment variable (os.pathsep-separated) when the flag was not passed."""
    if cli_values:
        return cli_values
    env = os.environ.get(INCLUDE_DIRS_ENV)
    return [name for name in env.split(os.pathsep) if name] if env else []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument("--cwd", default=None, help="override cwd (for testing)")
    parser.add_argument(
        "--references",
        action="store_true",
        help="also list each skill's references/*.md documents (the lane's second subject shape)",
    )
    parser.add_argument(
        "--include-dir", action="append", default=None, metavar="NAME",
        help="directory NAME to walk into even though it would otherwise be pruned "
             "as noise (repeatable). Falls back to the MD_DOMAIN_INCLUDE_DIRS "
             "environment variable (os.pathsep-separated) when omitted.",
    )
    args = parser.parse_args()

    cwd = Path(args.cwd).resolve() if args.cwd else Path.cwd().resolve()
    include_dirs = _resolve_include_dirs(args.include_dir)
    skipped_dirs: list[Path] = []
    results = collect_skill_md(
        cwd, include_references=args.references,
        include_dirs=include_dirs, skipped_out=skipped_dirs,
    )
    skipped_rel = []
    for p in skipped_dirs:
        try:
            skipped_rel.append(str(p.relative_to(cwd)))
        except ValueError:
            skipped_rel.append(str(p))
    skipped_rel.sort()

    if args.json:
        # Kept as a flat LIST -- see discover_claude_md.py's main() for the
        # same shape decision and the reason (no envelope change when nothing
        # was pruned). A skipped directory is a distinct record ("skipped_dir"
        # key, no "path"/"name") appended after the file records.
        payload = [
            {"index": i + 1, "path": str(p), "name": name,
             "skill_type": skill_type, "kind": kind}
            for i, (p, name, skill_type, kind) in enumerate(results)
        ]
        payload.extend({"skipped_dir": rel, "reason": "noise-name"} for rel in skipped_rel)
        print(json.dumps(payload, indent=2))
        return 0

    if not results and not skipped_rel:
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

    if skipped_rel:
        print(f"\nskipped {len(skipped_rel)} noise-named directory/ies (use --include-dir to opt one back in):")
        for rel in skipped_rel:
            print(f"  - {rel}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
