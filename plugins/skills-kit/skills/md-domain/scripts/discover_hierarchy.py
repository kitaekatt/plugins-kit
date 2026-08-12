#!/usr/bin/env python3
"""discover_hierarchy.py -- enumerate a claude_md_tree subject: a named root, the
CLAUDE.md files governing it, and the persisted candidate reports targeting it.

Usage:
    python discover_hierarchy.py <directory> [--reports <dir>]
    python discover_hierarchy.py <directory> [--reports <dir>] --json

The hierarchy verb's subject is a TREE -- one named directory root plus every
CLAUDE.md beneath it, plus (optionally) one persisted coverage report per
assessed subtree. This script is the mechanical half: it enumerates the leaves,
finds the documents, loads the reports, and builds the input inventory the lane's
verdict is computed from. It decides nothing about what any fact means.

No whole-repo default: a directory must be named.

Why the enumeration is done HERE rather than taken from the caller. The
affirmative verdicts are unemittable while any enumerated leaf has no input
(criterion `input-inventory-complete`), and that check is only worth anything if
the leaf list did not come from the same hands as the reports. A tree's own root
document routinely omits directories from its structure map; a resolution built
on such a list cannot notice what the list already forgot.

Definitions used here, and nowhere else:

  * A LEAF is a directory at or beneath the root that DIRECTLY contains at least
    one code file, after the structural exclusions. It is the unit a coverage
    run assesses, so it is the unit an inventory row is owed for.

  * A REPORT is a JSON file holding one or more coverage subjects, each naming
    its `root` and carrying a `candidates` list. A subject whose candidates list
    is empty is an EXPLICIT ASSESSED-NULL -- materially different from no report
    at all, which is why the two get different inventory statuses.

The recursive tree walk itself (`walk_tree`) is not implemented here: it is
imported from discover_coverage.py, which also hosts discover_composition.py's
copy of the same primitive, so no two verbs can disagree about what is in a
tree or how deep it is read.

Stdlib-only. Reads the report files it was pointed at and nothing else; it
opens no CLAUDE.md and no source file.
"""

import argparse
import json
import os
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from discover_coverage import (  # noqa: E402
    ambient_chain,
    root_exclusion,
    walk_tree,
)

# Inventory statuses. `MISSING` is deliberately the only upper-case one: it is
# the row that makes both affirmative verdicts unemittable, and it should be
# unmissable in a rendered table.
STATUS_REPORT = "report"
STATUS_ASSESSED_NULL = "assessed-null"
STATUS_WRITTEN_DOC = "written-doc"
STATUS_MISSING = "MISSING"


def _key(path: Path) -> str:
    """Comparison key for a filesystem path.

    normcase folds case and separators on Windows, where a report emitted with
    `D:/proj/src` must match a leaf enumerated as `D:\\proj\\src`. Without it the
    inventory reports every leaf MISSING on one platform and none on the other.
    """
    return os.path.normcase(str(path))


def _subjects_from_payload(payload) -> list[dict]:
    """Normalize the accepted persisted-report shapes to a list of subjects.

    Accepted: a bare subject object, a list of them, or an object carrying them
    under `perSubject` or `subjects`. A shape carrying no `root` is rejected by
    the caller rather than guessed at -- a report that cannot say which subtree
    it assessed cannot fill an inventory row.
    """
    if isinstance(payload, list):
        return [s for s in payload if isinstance(s, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("perSubject", "subjects"):
        value = payload.get(key)
        if isinstance(value, list):
            return [s for s in value if isinstance(s, dict)]
    if "root" in payload:
        return [payload]
    return []


def load_reports(reports_dir: Path, tree_root: Path) -> tuple[list[dict], list[str]]:
    """Load every *.json under `reports_dir`. Returns (reports, notes).

    Each returned report is {source, root, candidates, candidateCount,
    assessedNull}. Candidates are passed through untouched except for a stable
    `_id` (`<file stem>#<index>`), which is what the lane's input-accounting
    check counts against -- without a stable identity a dropped candidate is
    indistinguishable from a merged one.
    """
    reports: list[dict] = []
    notes: list[str] = []
    if not reports_dir.is_dir():
        return [], [f"reports directory does not exist: {reports_dir}"]

    for path in sorted(reports_dir.rglob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            notes.append(f"unreadable report {path.name}: {exc}")
            continue
        subjects = _subjects_from_payload(payload)
        if not subjects:
            notes.append(
                f"report {path.name} carries no recognizable coverage subject "
                "(expected a `root` plus `candidates`, or a `perSubject` / "
                "`subjects` list)"
            )
            continue
        for index, subject in enumerate(subjects):
            raw_root = subject.get("root")
            if not raw_root:
                notes.append(f"report {path.name} subject {index} names no root")
                continue
            subject_root = Path(raw_root)
            if not subject_root.is_absolute():
                subject_root = tree_root / subject_root
            candidates = subject.get("candidates")
            if not isinstance(candidates, list):
                candidates = []
            stem = path.stem if len(subjects) == 1 else f"{path.stem}[{index}]"
            tagged = []
            for c_index, candidate in enumerate(candidates):
                if not isinstance(candidate, dict):
                    notes.append(
                        f"report {path.name} candidate {c_index} is not an object; dropped"
                    )
                    continue
                item = dict(candidate)
                item["_id"] = f"{stem}#{c_index}"
                item["_source"] = stem
                tagged.append(item)
            reports.append({
                "source": stem,
                "sourcePath": str(path),
                "root": str(subject_root.resolve()),
                "candidates": tagged,
                "candidateCount": len(tagged),
                "assessedNull": len(tagged) == 0,
            })
    return reports, notes


def build_inventory(
    leaves: list[Path],
    claude_mds: list[Path],
    reports: list[dict],
) -> tuple[list[dict], list[dict]]:
    """Return (inventory, unmatchedReports).

    One inventory row per enumerated leaf, in leaf order. A report whose root
    matches no enumerated leaf is NOT quietly ignored: it means the reports and
    the tree disagree about what exists, which is an inventory failure of the
    same kind as a missing report and is surfaced as its own list.
    """
    by_root: dict[str, list[dict]] = {}
    for report in reports:
        by_root.setdefault(_key(Path(report["root"])), []).append(report)

    doc_dirs = {_key(p.parent) for p in claude_mds}
    leaf_keys = {_key(leaf) for leaf in leaves}

    inventory: list[dict] = []
    for leaf in leaves:
        key = _key(leaf)
        matched = by_root.get(key) or []
        if matched:
            has_candidates = any(r["candidateCount"] for r in matched)
            inventory.append({
                "leaf": str(leaf),
                "status": STATUS_REPORT if has_candidates else STATUS_ASSESSED_NULL,
                "sources": [r["source"] for r in matched],
            })
        elif key in doc_dirs:
            inventory.append({
                "leaf": str(leaf),
                "status": STATUS_WRITTEN_DOC,
                "sources": [],
            })
        else:
            inventory.append({
                "leaf": str(leaf),
                "status": STATUS_MISSING,
                "sources": [],
            })

    unmatched = [
        {"source": r["source"], "root": r["root"]}
        for r in reports
        if _key(Path(r["root"])) not in leaf_keys
    ]
    return inventory, unmatched


def build_subject(root: Path, reports_dir: Path | None) -> dict:
    root = root.resolve()
    leaves, claude_mds, skipped, noise_pruned = walk_tree(root)
    reports: list[dict] = []
    notes: list[str] = []
    if reports_dir is not None:
        reports, notes = load_reports(reports_dir.resolve(), root)
    inventory, unmatched = build_inventory(leaves, claude_mds, reports)
    return {
        "root": str(root),
        "rootExclusion": root_exclusion(root),
        "leaves": [str(p) for p in leaves],
        "claudeMdPaths": [str(p) for p in claude_mds],
        "ambientAbove": [
            str(p) for p in ambient_chain(root) if p.parent.resolve() != root
        ],
        "reports": reports,
        "inventory": inventory,
        "unmatchedReports": unmatched,
        "candidateTotal": sum(r["candidateCount"] for r in reports),
        "skipped": skipped,
        "noisePruned": noise_pruned,
        "notes": notes,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", nargs="?", default=None, help="the tree root")
    parser.add_argument(
        "--reports", default=None, metavar="DIR",
        help="directory of persisted coverage reports (JSON); absence selects "
             "the chain-audit face",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument("--cwd", default=None, help="override cwd (for testing)")
    args = parser.parse_args()

    cwd = Path(args.cwd).resolve() if args.cwd else Path.cwd().resolve()

    if not args.directory:
        print(
            "error: name a tree root to resolve. There is no whole-repo default.",
            file=sys.stderr,
        )
        return 2
    root = (cwd / args.directory).resolve()
    if not root.is_dir():
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 2

    reports_dir = (cwd / args.reports).resolve() if args.reports else None
    subject = build_subject(root, reports_dir)

    if args.json:
        print(json.dumps(subject, indent=2))
        return 0

    print(f"tree root: {subject['root']}")
    if subject["rootExclusion"]:
        print(
            f"  NOTE: this root is itself {subject['rootExclusion']}; "
            f"resolving it because you named it explicitly"
        )
    print(f"  leaves (code directories): {len(subject['leaves'])}")
    print(f"  CLAUDE.md files in tree: {len(subject['claudeMdPaths'])}")
    for path in subject["claudeMdPaths"]:
        print(f"    - {path}")
    if subject["ambientAbove"]:
        print(f"  ambient above the root ({len(subject['ambientAbove'])}):")
        for path in subject["ambientAbove"]:
            print(f"    - {path}")
    print(
        f"  reports loaded: {len(subject['reports'])} "
        f"({subject['candidateTotal']} candidate(s))"
    )
    print("  input inventory:")
    for row in subject["inventory"]:
        sources = f"  <- {', '.join(row['sources'])}" if row["sources"] else ""
        print(f"    [{row['status']}] {row['leaf']}{sources}")
    missing = sum(1 for r in subject["inventory"] if r["status"] == STATUS_MISSING)
    if missing:
        print(
            f"  {missing} leaf/leaves have NO input -- affirmative verdicts are "
            f"unemittable (input-inventory-complete)"
        )
    if subject["unmatchedReports"]:
        print(f"  reports matching no enumerated leaf ({len(subject['unmatchedReports'])}):")
        for row in subject["unmatchedReports"]:
            print(f"    - {row['source']} -> {row['root']}")
    if subject["skipped"]:
        print(f"  skipped ({len(subject['skipped'])}):")
        for entry in subject["skipped"]:
            print(f"    - [{entry['reason']}] {entry['path']}")
    if subject["noisePruned"]:
        print(f"  noise directories pruned: {subject['noisePruned']}")
    for note in subject["notes"]:
        print(f"  note: {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
