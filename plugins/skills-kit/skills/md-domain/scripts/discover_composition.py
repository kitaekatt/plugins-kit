#!/usr/bin/env python3
"""discover_composition.py -- enumerate the composition subject set under a tree
root: every directory that IS a coverage subject, plus every ancestor of one, up
to the named root.

Usage:
    python discover_composition.py <directory>
    python discover_composition.py <directory> --json

The generation lane composes a document at a directory when it, or anything
beneath it, holds code -- deliberately WIDER than the coverage subject rule
(`discover_coverage.py:19-26`), which stays non-recursive and untouched by this
script. A directory such as `godot/` may hold no direct code of its own (only
`project.godot`, a `.tres`, an `.svg`) while its subtree is full of code; it
still needs a composed document, because that is where facts common to its
children belong. Two different subject sets, never to be conflated:

  * COVERAGE subjects -- a directory has DIRECT code files. Non-recursive.
    Which directories get ASSESSED (a report). Unchanged; see
    discover_coverage.py.
  * COMPOSITION subjects -- the directory, OR anything beneath it, has code
    files. Which directories get a document COMPOSED. Strictly contains the
    coverage set, larger by exactly the code-free ancestors.

This is the PRODUCER for the composition rule: a cheap EXISTENCE check down
each subtree, never a re-read of it. It is pure path arithmetic over one
recursive walk (`discover_coverage.walk_tree`): the walk returns the leaves
(directories directly
holding code), and the composition set is those leaves plus every one of
their ancestors up to the named root. No second filesystem pass.

VCS-ignore semantics are inherited from `walk_tree`: an ignored directory is
neither a subject nor an input to the existence check, and it never pulls its
own ancestors into scope on the strength of code that sits underneath it.

A tree with no code anywhere beneath the named root produces an empty
composition set -- not even the root itself, unless the root is also a
coverage subject or has a code-bearing descendant.

No whole-repo default: a directory must be named, matching both sibling
discover scripts.

Stdlib-only.
"""

import argparse
import json
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from discover_coverage import root_exclusion, walk_tree  # noqa: E402


def composition_subjects(leaves: list[Path], root: Path) -> list[Path]:
    """Every leaf, plus every ancestor of a leaf, up to and including `root`.

    Pure path arithmetic -- no filesystem access. `root` need not itself be a
    leaf; it enters the set only when some leaf lies at or beneath it, which is
    always true for a leaf `walk_tree` actually returned (it only ever
    descends from `root`), so `root` is included whenever `leaves` is
    non-empty.
    """
    subjects: set[Path] = set()
    for leaf in leaves:
        current = leaf
        while True:
            subjects.add(current)
            if current == root:
                break
            parent = current.parent
            if parent == current:
                # Defensive: a leaf outside root's ancestry should not occur
                # (walk_tree only descends from root), but never loop forever.
                break
            current = parent
    return sorted(subjects, key=str)


def build_subject(root: Path) -> dict:
    """Assemble the composition subject set for one named tree root."""
    root = root.resolve()
    leaves, claude_mds, skipped, noise_pruned = walk_tree(root)
    subjects = composition_subjects(leaves, root)
    leaf_set = {str(p) for p in leaves}
    return {
        "root": str(root),
        "rootExclusion": root_exclusion(root),
        "compositionSubjects": [str(p) for p in subjects],
        "coverageSubjects": [str(p) for p in leaves],
        "codeFreeCompositionSubjects": [
            str(p) for p in subjects if str(p) not in leaf_set
        ],
        "claudeMdPaths": [str(p) for p in claude_mds],
        "skipped": skipped,
        "noisePruned": noise_pruned,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", nargs="?", default=None, help="the tree root")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument("--cwd", default=None, help="override cwd (for testing)")
    args = parser.parse_args()

    cwd = Path(args.cwd).resolve() if args.cwd else Path.cwd().resolve()

    if not args.directory:
        print(
            "error: name a tree root to enumerate. There is no whole-repo default.",
            file=sys.stderr,
        )
        return 2
    root = (cwd / args.directory).resolve()
    if not root.is_dir():
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 2

    subject = build_subject(root)

    if args.json:
        print(json.dumps(subject, indent=2))
        return 0

    print(f"tree root: {subject['root']}")
    if subject["rootExclusion"]:
        print(
            f"  NOTE: this root is itself {subject['rootExclusion']}; "
            f"resolving it because you named it explicitly"
        )
    print(f"  composition subjects: {len(subject['compositionSubjects'])}")
    for path in subject["compositionSubjects"]:
        marker = "" if path in subject["coverageSubjects"] else "  (code-free; composed from children only)"
        print(f"    - {path}{marker}")
    if not subject["compositionSubjects"]:
        print("  NONE -- no code exists at or beneath this root")
    if subject["skipped"]:
        print(f"  skipped ({len(subject['skipped'])}):")
        for entry in subject["skipped"]:
            print(f"    - [{entry['reason']}] {entry['path']}")
    if subject["noisePruned"]:
        print(f"  noise directories pruned: {subject['noisePruned']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
