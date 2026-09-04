#!/usr/bin/env python3
"""discover_human_html.py -- the CK-2 shared discovery scan for the human-html artifact.

Usage:
    python discover_human_html.py <repository-root>
    python discover_human_html.py <repository-root> <directory>
    python discover_human_html.py <repository-root> --json      (the default form)

One scan answers every question the analyze lane, the generate lane and
`human_html_check.py` ask about a repository's human-html state: which
directories are subjects, what each subject's DR-2 source stamp is, what its
decision record says, which generated files exist beside it, where its
navigation spine points, and whether a descendant went stale under TS-2.

READ-ONLY. The script runs git query commands and writes nothing, so it is safe
to run against a tree another session is editing.

Why one scan and not two (CK-2 rationale): the generator and the checker must
agree about ordering and about navigation targets. A checker with its own walk
can disagree with the generator that produced the page it is judging, and the
disagreement surfaces as a navigation FAIL nobody can reproduce.

Imports: the Python standard library plus `skills_kit_lib.human_html`, and
nothing else (CK-2). The record schema, the source stamp and the navigation
computation are all owned by that package interface; this script contributes
the walk and the ordering.

Three properties are load-bearing:

  * A SUBJECT IS A DIRECTORY WITH AT LEAST ONE ANALYSIS INPUT IN ITS SUBTREE.
    Analysis inputs are the repository's non-ignored files minus DR-2's excluded
    set (`.databench/`, `human.html`, `human.<slug>.html`). That single
    definition delivers three of CK-2's requirements at once: VCS metadata and
    ignored paths never appear (git's own ignore rules decide, via
    `git ls-files --exclude-standard`), `.databench/` is excluded, and a
    directory holding only generated output is excluded because it contributes
    no input.

  * ORDER IS DEEPEST-FIRST (TS-1). Every descendant precedes its ancestors, so a
    caller walking the emitted list in order always reads a finished child
    before composing its parent. Ties break on path for determinism.

  * NAVIGATION AND STALENESS ARE COMPUTED OVER EVERY RECORD IN THE REPOSITORY,
    not only over the emitted scope. Narrowing the scope to one directory must
    not change where that directory's up-link points, so the record map is
    always loaded whole.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

# skills_kit_lib lives at the plugin root; make it importable regardless of
# which interpreter launched the script. `skills_kit_lib.human_html` is
# stdlib-only by contract (CK-1), so this adds no runtime dependency.
_PLUGIN_ROOT = Path(__file__).resolve().parents[3]
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from skills_kit_lib import human_html as hh  # noqa: E402


RECORD_STATUS_MISSING = "missing"
RECORD_STATUS_INVALID = "invalid"
RECORD_STATUS_FRESH = "fresh"
RECORD_STATUS_STALE = "stale"

# A record status that is anything but `fresh` makes the directory itself stale,
# and TS-2 propagates that to every dependent ancestor.
_UNFRESH = (RECORD_STATUS_MISSING, RECORD_STATUS_INVALID, RECORD_STATUS_STALE)


class DiscoveryError(Exception):
    """The repository could not be scanned. The message names what failed."""


# ---------------------------------------------------------------------------
# Repository walk
# ---------------------------------------------------------------------------

def _git(repo_root: Path, args: list[str]) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise DiscoveryError(
            "git %s failed in %s: %s" % (" ".join(args), repo_root, proc.stderr.strip())
        )
    return proc.stdout


def is_generated_name(name: str) -> bool:
    """True for a name this contract generates: `human.html` or `human.<slug>.html`."""
    if name == hh.PAGE_FILENAME:
        return True
    return name.startswith(hh.REFERENCE_PREFIX) and name.endswith(hh.REFERENCE_SUFFIX)


def reference_slug(name: str) -> str | None:
    """Return the RD-1 slug of a `human.<slug>.html` filename, or None."""
    if name == hh.PAGE_FILENAME or not is_generated_name(name):
        return None
    slug = name[len(hh.REFERENCE_PREFIX):-len(hh.REFERENCE_SUFFIX)]
    return slug if hh.SLUG_RE.match(slug) else None


def is_analysis_input(rel_path: str) -> bool:
    """True when `rel_path` is a DR-2 analysis input rather than generated output."""
    parts = rel_path.split("/")
    if hh.RECORD_ROOT.split("/")[0] in parts:
        return False
    return not is_generated_name(parts[-1])


def repository_files(repo_root: Path) -> list[str]:
    """Every non-ignored repository file, as POSIX paths relative to the root.

    `--cached --others --exclude-standard` is the pairing that answers "what
    does this repository contain, minus what it ignores": tracked files plus
    untracked files that no ignore rule covers. `.git/` is never listed, so VCS
    metadata needs no separate exclusion.
    """
    out = _git(
        repo_root,
        ["ls-files", "--cached", "--others", "--exclude-standard", "--full-name", "-z"],
    )
    return sorted({entry for entry in out.split("\0") if entry})


def subject_directories(files: list[str]) -> list[str]:
    """Every directory holding an analysis input somewhere in its subtree.

    A directory with no analysis input under it is not a subject: that covers a
    directory holding only generated output, a `.databench/` record tree, and an
    ignored tree alike.
    """
    subjects: set[str] = set()
    for rel in files:
        if not is_analysis_input(rel):
            continue
        parts = rel.split("/")[:-1]
        subjects.add(hh.ROOT_DIRECTORY)
        for depth in range(1, len(parts) + 1):
            subjects.add("/".join(parts[:depth]))
    return sorted(subjects)


def depth_of(directory: str) -> int:
    """Directory depth below the repository root, which is depth 0."""
    return 0 if directory == hh.ROOT_DIRECTORY else len(directory.split("/"))


def deepest_first(directories: list[str]) -> list[str]:
    """Order directories so every descendant precedes its ancestors (TS-1)."""
    return sorted(directories, key=lambda item: (-depth_of(item), item))


def in_scope(directory: str, root: str) -> bool:
    """True when `directory` is `root` or lives beneath it."""
    if root == hh.ROOT_DIRECTORY:
        return True
    return directory == root or directory.startswith(root + "/")


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

def _record_directory(repo_root: Path, path: Path) -> str:
    relative = path.parent.relative_to(repo_root / hh.RECORD_ROOT)
    return hh.normalize_directory(relative.as_posix())


def load_records(repo_root: Path) -> tuple[dict[str, hh.Record], dict[str, str]]:
    """Load every decision record in the repository.

    Returns `(records, errors)`: the valid records keyed by normalized
    directory, and a parallel mapping of directory to the validation error
    message for each record that failed DR-1. An unreadable or invalid record is
    never silently dropped, because "missing" and "invalid" are different
    findings for CK-1.
    """
    records: dict[str, hh.Record] = {}
    errors: dict[str, str] = {}
    base = repo_root / hh.RECORD_ROOT
    if not base.is_dir():
        return records, errors
    for path in sorted(base.rglob(hh.RECORD_FILENAME)):
        directory = _record_directory(repo_root, path)
        try:
            record = hh.load_record(path)
        except hh.HumanHtmlError as exc:
            errors[directory] = str(exc)
            continue
        if record.directory != directory:
            errors[directory] = (
                "record: directory field %r does not match its record path %r"
                % (record.directory, directory)
            )
            continue
        records[directory] = record
    return records, errors


# ---------------------------------------------------------------------------
# Per-directory scan
# ---------------------------------------------------------------------------

def generated_files(repo_root: Path, directory: str) -> tuple[str | None, list[str]]:
    """Return `(page_file, reference_files)` present on disk for `directory`."""
    base = repo_root if directory == hh.ROOT_DIRECTORY else repo_root / directory
    if not base.is_dir():
        return None, []
    page = hh.PAGE_FILENAME if (base / hh.PAGE_FILENAME).is_file() else None
    references = sorted(
        item.name
        for item in base.iterdir()
        if item.is_file() and item.name != hh.PAGE_FILENAME and is_generated_name(item.name)
    )
    return page, references


def scan(repo_root: str | Path, directory: str | Path = hh.ROOT_DIRECTORY) -> dict:
    """Scan `repo_root` and return the CK-2 discovery result.

    `directory` narrows which subjects are EMITTED. Records, navigation and
    stale-child propagation are always computed over the whole repository, so
    narrowing never changes a directory's answers.
    """
    root_path = Path(repo_root).resolve()
    scope = hh.normalize_directory(directory)
    files = repository_files(root_path)
    subjects = subject_directories(files)
    records, record_errors = load_records(root_path)

    stamps: dict[str, tuple[str | None, bool, str | None]] = {}
    for subject in subjects:
        try:
            sha, dirty = hh.source_stamp(root_path, subject)
            stamps[subject] = (sha, dirty, None)
        except hh.HumanHtmlError as exc:
            # No committed analysis input yet. Nothing identifies the judged
            # content, which is exactly the DR-2 dirty state.
            stamps[subject] = (None, True, str(exc))

    statuses: dict[str, str] = {}
    for subject in subjects:
        if subject in record_errors:
            statuses[subject] = RECORD_STATUS_INVALID
        elif subject not in records:
            statuses[subject] = RECORD_STATUS_MISSING
        elif records[subject].source_sha != stamps[subject][0]:
            statuses[subject] = RECORD_STATUS_STALE
        else:
            statuses[subject] = RECORD_STATUS_FRESH

    # TS-2: a stale or missing child makes every dependent ancestor stale.
    stale_children: dict[str, list[str]] = {}
    for subject in subjects:
        stale_children[subject] = sorted(
            other
            for other in subjects
            if other != subject
            and in_scope(other, subject)
            and statuses[other] in _UNFRESH
        )

    emitted = []
    for subject in deepest_first([s for s in subjects if in_scope(s, scope)]):
        sha, dirty, stamp_error = stamps[subject]
        status = statuses[subject]
        record = records.get(subject)
        page_file, reference_files = generated_files(root_path, subject)
        up, down = hh.navigation_targets(records, subject)
        emitted.append(
            {
                "directory": subject,
                "depth": depth_of(subject),
                "source_sha": sha,
                "dirty": dirty,
                "stamp_error": stamp_error,
                "record": {
                    "status": status,
                    "path": str(
                        hh.record_path(root_path, subject).relative_to(root_path).as_posix()
                    ),
                    "decision": record.decision if record else None,
                    "identity": record.identity if record else None,
                    "source_sha": record.source_sha if record else None,
                    "dirty": record.dirty if record else None,
                    "instructions": record.instructions if record else None,
                    "references": [
                        {"slug": ref.slug, "title": ref.title, "file": ref.file}
                        for ref in (record.references if record else ())
                    ],
                    "error": record_errors.get(subject),
                },
                "page_file": page_file,
                "reference_files": reference_files,
                "nearest_page_ancestor": up,
                "nearest_page_descendants": down,
                "stale_children": stale_children[subject],
                "stale_child": bool(stale_children[subject]),
                "stale": status in _UNFRESH or bool(stale_children[subject]),
            }
        )

    return {
        "repo_root": str(root_path),
        "scope": scope,
        "count": len(emitted),
        "directories": emitted,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Discover the human-html state of a repository (CK-2).",
    )
    parser.add_argument("repo_root", help="repository root to scan")
    parser.add_argument(
        "directory",
        nargs="?",
        default=hh.ROOT_DIRECTORY,
        help="optional repository-relative directory to narrow the emitted scope",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit JSON (the default and only output form; accepted for grammar symmetry)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = scan(args.repo_root, args.directory)
    except (DiscoveryError, hh.HumanHtmlError) as exc:
        print("discover_human_html: %s" % exc, file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
