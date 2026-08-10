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

A file whose extension is recognized as neither code, doc, nor a common asset
type is also RECORDED, never dropped with no trace: it is counted (aggregated
by extension) into `unknownExtensions`. Silently dropping such a file made a
whole subtree of an unrecognized language read as an EMPTY, well-formed
subject -- the strongest verdict, over a subtree that was never read at all.

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

from discover_claude_md import CODE_DATA_EXT, _MD_LIKE, find_project_root  # noqa: E402

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

# Extensions that are never code: images, fonts, audio, video, archives, and
# compiled binaries/lockfiles. Excluded from `unknownExtensions` so that signal
# stays readable -- a 105-PNG directory should not drown out the one `.gd` file
# CODE_DATA_EXT is missing. A MISSING entry here only causes NOISE (an ordinary
# asset extension shows up in unknownExtensions), never a MISSED SUBJECT (a real
# code extension is never added here) -- that is the safe direction to err in.
ASSET_BINARY_EXT = {
    # images
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".svg", ".webp",
    ".tiff", ".tga", ".psd", ".heic", ".avif",
    # fonts
    ".ttf", ".otf", ".woff", ".woff2", ".eot",
    # audio
    ".wav", ".mp3", ".ogg", ".flac", ".aac", ".m4a",
    # video
    ".mp4", ".mov", ".avi", ".webm", ".mkv",
    # archives
    ".zip", ".tar", ".gz", ".7z", ".rar", ".bz2", ".xz",
    # compiled binaries / build byproducts / lockfiles
    ".exe", ".dll", ".so", ".dylib", ".pdb", ".pyc", ".pyd", ".class",
    ".o", ".obj", ".a", ".lib", ".lock",
}

# Extensionless files that are never code, matched on NAME. `Path(".gitignore")`
# and `Path("LICENSE")` both have an empty suffix, so without this every repo
# convention file would land in `unknownExtensions` under "" -- and a
# genuinely code-free directory holding only a LICENSE and a .gitignore would
# be reported as a discovery FAILURE rather than as the empty subject it is.
# Extensionless files NOT listed here (Makefile, Dockerfile, an extensionless
# shell script) stay counted: those plausibly are code, and the safe direction
# is to surface them.
EXTENSIONLESS_NON_CODE = {
    "license", "licence", "notice", "copying", "authors", "contributors",
    "changelog", "codeowners", "version", "readme", "patents", "owners",
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


def walk_subtree(root: Path) -> tuple[list[Path], list[dict], int, dict[str, int]]:
    """Collect code files under `root`, recording every structural exclusion.

    Returns (code_files, skipped, noise_pruned, unknown_extensions).
    `skipped` entries are {"path": <str>, "reason": <str>} for the exclusions a
    user should see; `noise_pruned` counts the ones deliberately not itemized.
    `unknown_extensions` is {<ext>: <count>} for every file whose extension is
    in neither CODE_DATA_EXT, `_MD_LIKE` (already accounted for as docs), nor
    ASSET_BINARY_EXT (never code) -- aggregated by extension, never itemized
    per file, so a directory of 105 PNGs does not become 105 report lines. A
    file counted here was NOT read as code and NOT recognized as a doc or
    asset: the subtree was not fully accounted for, and that must never be
    mistaken for "nothing else is here" (see the coverage lane's refusal rule).

    Side-effect free: it stats and lists directories, and reads nothing.
    """
    code_files: list[Path] = []
    skipped: list[dict] = []
    noise_pruned = 0
    unknown_extensions: dict[str, int] = {}
    root = root.resolve()
    # Real directories already descended into, keyed by resolved path. Two
    # aliases of one directory (a symlink, or a Windows junction) would
    # otherwise be walked twice, emitting every file under them twice.
    visited: set[Path] = {root}

    def descend(directory: Path, depth: int) -> None:
        nonlocal noise_pruned, unknown_extensions
        if depth > MAX_DEPTH:
            skipped.append({"path": str(directory), "reason": "depth-limit"})
            return
        try:
            entries = sorted(directory.iterdir(), key=lambda p: p.name)
        except OSError:
            return

        for entry in entries:
            name = entry.name
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

                # Containment is tested on the RESOLVED path for every directory,
                # not only ones is_symlink() admits to. A Windows directory
                # junction carries IO_REPARSE_TAG_MOUNT_POINT, which islink()
                # does not report, so an is_symlink()-gated check lets a junction
                # pointing outside the subtree pull in foreign code.
                #
                # RuntimeError is not redundant with OSError here: Path.resolve()
                # deliberately re-raises an ELOOP OSError as
                # RuntimeError("Symlink loop from ...") (pathlib's check_eloop),
                # so catching OSError alone lets a circular link crash the walk
                # instead of being recorded and skipped.
                try:
                    target = entry.resolve()
                except (OSError, RuntimeError):
                    skipped.append({"path": str(entry), "reason": SKIP_SYMLINK_OUT})
                    continue
                if not _is_within(target, root):
                    skipped.append({"path": str(entry), "reason": SKIP_SYMLINK_OUT})
                    continue
                if target in visited:
                    # An alias of a directory already walked. Not an exclusion
                    # worth reporting -- its files are already in the subject.
                    continue
                visited.add(target)
                descend(entry, depth + 1)
            elif entry.is_file() and is_code_file(entry):
                code_files.append(entry)
            elif entry.is_file():
                ext = entry.suffix.lower()
                if ext in _MD_LIKE or ext in ASSET_BINARY_EXT:
                    continue
                # A dotfile (.gitignore, .editorconfig) has an empty suffix and
                # is configuration, not an unrecognized language. So does a
                # convention file like LICENSE. Neither means the subtree went
                # unread, so neither may trip the lane's discovery-failure rule.
                if not ext:
                    if name.startswith(".") or name.lower() in EXTENSIONLESS_NON_CODE:
                        continue
                unknown_extensions[ext] = unknown_extensions.get(ext, 0) + 1

    descend(root, 0)
    code_files.sort(key=str)
    return code_files, skipped, noise_pruned, unknown_extensions


def _is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def root_exclusion(root: Path) -> str | None:
    """Return the exclusion reason the ROOT itself matches, or None.

    walk_subtree tests the directories it descends into, never the root it was
    handed -- so a root that is itself vendored, generated, or a nested
    repository would be scanned in full with no exclusion recorded. Naming such
    a directory explicitly is honoured (the user asked for it), but it is always
    REPORTED; a root derived from a diff is filtered before it gets here.
    """
    reason = _skip_reason(root.name)
    if reason is not None:
        return reason
    return None


def build_subject(root: Path) -> dict:
    """Assemble one coverage subject for a code subtree."""
    root = root.resolve()
    code_files, skipped, noise_pruned, unknown_extensions = walk_subtree(root)
    chain = ambient_chain(root)
    return {
        "root": str(root),
        "rootExclusion": root_exclusion(root),
        "codeFiles": [str(p) for p in code_files],
        "ambientClaudeMdPaths": [str(p) for p in chain],
        "skipped": skipped,
        "noisePruned": noise_pruned,
        "unknownExtensions": unknown_extensions,
    }


def diff_roots(repo: Path, git_range: str | None) -> tuple[list[Path], list[str]]:
    """Derive subject roots from changed files. Returns (roots, notes).

    Read-only: runs `git diff --name-only` and nothing else. One subject per
    distinct directory holding a changed code file.
    """
    # `git diff --name-only` prints paths relative to the WORKTREE ROOT, not to
    # -C's directory, so names must be joined to the toplevel. Joining them to a
    # subdirectory silently yields nonexistent paths and an empty subject set.
    toplevel = _git_toplevel(repo)
    if toplevel is None:
        return [], ["not a git worktree; --diff needs one"]

    # -c core.quotePath=false: git C-quotes non-ASCII names by default
    # ("caf\303\251.c"), which would not resolve on disk and would be dropped.
    # --end-of-options: a range is user input and must never be read as a flag;
    # without it a value like --output=<path> makes this read-only probe write.
    cmd = [
        "git", "-c", "core.quotePath=false", "-C", str(toplevel),
        "diff", "--name-only", "--end-of-options",
    ]
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
    notes: list[str] = []
    excluded = 0
    for line in completed.stdout.splitlines():
        rel = line.strip()
        if not rel:
            continue
        path = toplevel / rel
        if not is_code_file(path):
            continue
        parent = path.parent
        if not parent.is_dir():
            continue
        # A diff-derived root gets the structural exclusions applied to it. An
        # explicitly named root is honoured-and-reported instead; nobody asked
        # for this one by name.
        if any(_skip_reason(part) for part in parent.relative_to(toplevel).parts):
            excluded += 1
            continue
        dirs.setdefault(parent.resolve(), None)
    if excluded:
        notes.append(f"{excluded} changed file(s) skipped: vendored or generated path")
    return list(dirs), notes


def _git_toplevel(start: Path) -> Path | None:
    """Return the worktree root for `start`, or None when it is not in one."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=False, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    out = completed.stdout.strip()
    return Path(out) if out else None


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
        if subject["rootExclusion"]:
            print(
                f"  NOTE: this root is itself {subject['rootExclusion']}; "
                f"assessing it because you named it explicitly"
            )
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
        if subject["unknownExtensions"]:
            total_unknown = sum(subject["unknownExtensions"].values())
            print(
                f"  unknown extensions ({total_unknown} file(s), not code, "
                f"not docs, not a recognized asset type):"
            )
            for ext, count in sorted(subject["unknownExtensions"].items()):
                print(f"    - {ext or '(no extension)'}: {count}")
        if subject["noisePruned"]:
            print(f"  noise directories pruned: {subject['noisePruned']}")
        print()

    for note in notes:
        print(f"note: {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
