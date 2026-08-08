#!/usr/bin/env python3
"""discover_coverage.py -- enumerate coverage subjects: (code subtree, its ambient
CLAUDE.md chain).

Usage:
    python discover_coverage.py <directory>
    python discover_coverage.py <directory> --json
    python discover_coverage.py --diff [<git-range>]

The coverage verb's subject is a CODE SUBTREE plus the CLAUDE.md files that
actually LOAD for it -- not a markdown file. This script is the mechanical half:
it resolves the subject set and applies the structural exclusions, and it decides
nothing about what the code means. No whole-repo default: a directory must be
named, or --diff must derive the roots from changed files.

Two properties are load-bearing and easy to get wrong:

  * The ambient chain INCLUDES a CLAUDE.md at the subtree root itself. The
    document lanes' resolver starts at the target's PARENT, because the target is
    the CLAUDE.md. Here the target is a directory, and a CLAUDE.md sitting in it
    is the most ambient file there is.

  * The upward walk stops at the nearest .git. A nested repository's ambient
    chain is its own, never the outer repository's -- so a vendored or submodule
    subtree does not silently inherit ancestors that never load for it.

Exclusions are STRUCTURAL only and are applied before any file is read: vendored
and third-party trees, generated trees, symlinks resolving outside the subtree,
and nested repositories. Deciding that a fact is "already covered by an ambient
claim that resolves" is NOT an exclusion -- establishing it requires reading the
ambient document and usually the source it anchors to, so it belongs to the
assessment step, not here.

Every exclusion is RECORDED and reported, never silently applied.

Stdlib-only.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

# The shared extension set and project-root walk live alongside the CLAUDE.md
# discover script; import rather than copy so the two cannot drift.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from discover_claude_md import CODE_DATA_EXT, find_project_root  # noqa: E402

# Directory basenames that are vendored / third-party or build output. Matched
# against a single path component. `build`/`Build` and `target` are ambiguous
# (a real source dir may be named either), which is exactly why a skip is
# reported rather than assumed correct -- the user can see it and re-scope.
VENDOR_DIR_NAMES = {
    "node_modules", "vendor", "third_party", "thirdparty", "Pods",
    "target", "dist", "build", "Build", "out",
}

# Directory basenames whose contents are generated. An existing modality already
# covers generated content, so it is not this verb's subject.
GENERATED_DIR_NAMES = {"generated", "__generated__", "gen", "autogen"}

# Noise directories: pruned and counted, but not itemized in the report. These
# are never anybody's source of insight and listing them would bury the skips
# that matter.
NOISE_DIR_NAMES = {
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".tox", "Intermediate", "Saved", "Binaries",
    "DerivedDataCache", ".idea", ".vs",
}

MAX_DEPTH = 12

# Skip reasons, reported verbatim.
SKIP_VENDORED = "vendored"
SKIP_GENERATED = "generated"
SKIP_SYMLINK_OUT = "symlink-outside-subtree"
SKIP_NESTED_REPO = "nested-repo"


def is_code_file(path: Path) -> bool:
    """A file counts as code when its extension is in the shared code/data set."""
    return path.suffix.lower() in CODE_DATA_EXT


def ambient_chain(directory: Path) -> list[Path]:
    """Return the CLAUDE.md files ambient for `directory`, root-most first.

    Walks from `directory` itself upward to the repository root, collecting
    CLAUDE.md at each level. The walk stops at the nearest .git -- a nested
    repository's chain is its own. Returns an empty list when nothing covers the
    directory at all, which is the case the coverage verb most exists to surface.
    """
    project_root = find_project_root(directory)
    chain: list[Path] = []
    current = directory
    while True:
        candidate = current / "CLAUDE.md"
        if candidate.is_file():
            chain.append(candidate)
        if project_root is not None and current == project_root:
            break
        if current == current.parent:
            break
        # Outside a repo, do not climb past the named directory's own tree.
        if project_root is None:
            break
        current = current.parent
    chain.reverse()
    return chain


def _skip_reason(dir_name: str) -> str | None:
    if dir_name in VENDOR_DIR_NAMES:
        return SKIP_VENDORED
    if dir_name in GENERATED_DIR_NAMES:
        return SKIP_GENERATED
    return None


def walk_subtree(root: Path) -> tuple[list[Path], list[dict], int]:
    """Collect code files under `root`, recording every structural exclusion.

    Returns (code_files, skipped, noise_pruned). `skipped` entries are
    {"path": <str>, "reason": <str>} for the exclusions a user should see;
    `noise_pruned` counts the ones deliberately not itemized.

    Side-effect free: it stats and lists directories, and reads nothing.
    """
    code_files: list[Path] = []
    skipped: list[dict] = []
    noise_pruned = 0
    root = root.resolve()

    def descend(directory: Path, depth: int) -> None:
        nonlocal noise_pruned
        if depth > MAX_DEPTH:
            skipped.append({"path": str(directory), "reason": "depth-limit"})
            return
        try:
            entries = sorted(directory.iterdir(), key=lambda p: p.name)
        except OSError:
            return

        for entry in entries:
            name = entry.name
            if entry.is_symlink():
                # A symlink is followed only when it stays inside the subtree;
                # otherwise the subject would silently acquire foreign code.
                try:
                    target = entry.resolve()
                except OSError:
                    skipped.append({"path": str(entry), "reason": SKIP_SYMLINK_OUT})
                    continue
                if not _is_within(target, root):
                    skipped.append({"path": str(entry), "reason": SKIP_SYMLINK_OUT})
                    continue

            if entry.is_dir():
                if name in NOISE_DIR_NAMES:
                    noise_pruned += 1
                    continue
                reason = _skip_reason(name)
                if reason is not None:
                    skipped.append({"path": str(entry), "reason": reason})
                    continue
                # A directory carrying its own .git is a separate repository; its
                # ambient chain is not this one's, so it is not part of this
                # subject.
                if (entry / ".git").exists():
                    skipped.append({"path": str(entry), "reason": SKIP_NESTED_REPO})
                    continue
                if name.startswith(".") and name != ".claude":
                    noise_pruned += 1
                    continue
                descend(entry, depth + 1)
            elif entry.is_file() and is_code_file(entry):
                code_files.append(entry)

    descend(root, 0)
    code_files.sort(key=str)
    return code_files, skipped, noise_pruned


def _is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def build_subject(root: Path) -> dict:
    """Assemble one coverage subject for a code subtree."""
    root = root.resolve()
    code_files, skipped, noise_pruned = walk_subtree(root)
    chain = ambient_chain(root)
    return {
        "root": str(root),
        "codeFiles": [str(p) for p in code_files],
        "ambientClaudeMdPaths": [str(p) for p in chain],
        "skipped": skipped,
        "noisePruned": noise_pruned,
    }


def diff_roots(repo: Path, git_range: str | None) -> tuple[list[Path], list[str]]:
    """Derive subject roots from changed files. Returns (roots, notes).

    Read-only: runs `git diff --name-only` and nothing else. One subject per
    distinct directory holding a changed code file.
    """
    cmd = ["git", "-C", str(repo), "diff", "--name-only"]
    if git_range:
        cmd.append(git_range)
    try:
        completed = subprocess.run(
            cmd, capture_output=True, text=True, check=False, timeout=30
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return [], [f"git diff failed: {exc}"]
    if completed.returncode != 0:
        return [], [f"git diff failed: {completed.stderr.strip()}"]

    dirs: dict[Path, None] = {}
    for line in completed.stdout.splitlines():
        rel = line.strip()
        if not rel:
            continue
        path = (repo / rel)
        if not is_code_file(path):
            continue
        parent = path.parent
        if parent.is_dir():
            dirs.setdefault(parent.resolve(), None)
    return list(dirs), []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "directory", nargs="?", default=None,
        help="the code subtree to assess; required unless --diff is given",
    )
    parser.add_argument(
        "--diff", nargs="?", const="", default=None, metavar="RANGE",
        help="derive subject roots from changed files instead of a named directory",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument("--cwd", default=None, help="override cwd (for testing)")
    args = parser.parse_args()

    cwd = Path(args.cwd).resolve() if args.cwd else Path.cwd().resolve()
    notes: list[str] = []

    if args.diff is not None:
        roots, notes = diff_roots(cwd, args.diff or None)
    elif args.directory:
        target = (cwd / args.directory).resolve()
        if not target.is_dir():
            print(f"error: not a directory: {target}", file=sys.stderr)
            return 2
        roots = [target]
    else:
        # No whole-repo default, deliberately -- an unbounded default is how this
        # becomes expensive and non-idempotent.
        print(
            "error: name a directory to assess, or pass --diff. There is no "
            "whole-repo default.",
            file=sys.stderr,
        )
        return 2

    subjects = [build_subject(root) for root in roots]
    payload = {"subjects": subjects, "notes": notes}

    if args.json:
        print(json.dumps(payload, indent=2))
        return 0

    if not subjects:
        print("No coverage subjects resolved.")
        for note in notes:
            print(f"  note: {note}")
        return 0

    for subject in subjects:
        root = subject["root"]
        chain = subject["ambientClaudeMdPaths"]
        print(f"subject: {root}")
        print(f"  code files: {len(subject['codeFiles'])}")
        if chain:
            print(f"  ambient CLAUDE.md chain ({len(chain)}, root-most first):")
            for path in chain:
                print(f"    - {path}")
        else:
            print("  ambient CLAUDE.md chain: NONE -- no CLAUDE.md loads for this subtree")
        if subject["skipped"]:
            print(f"  skipped ({len(subject['skipped'])}):")
            for entry in subject["skipped"]:
                print(f"    - [{entry['reason']}] {entry['path']}")
        if subject["noisePruned"]:
            print(f"  noise directories pruned: {subject['noisePruned']}")
        print()

    for note in notes:
        print(f"note: {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
