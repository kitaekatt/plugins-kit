#!/usr/bin/env python3
"""discover_claude_md.py -- enumerate CLAUDE.md and CLAUDE.local.md files visible from the
current working directory.

Usage:
    python discover_claude_md.py
    python discover_claude_md.py --json

Walks upward from cwd to the project root (the nearest ancestor containing
.git) collecting ancestor CLAUDE.md and CLAUDE.local.md files -- never looking
outside the project boundary -- then walks downward up to a depth limit
collecting descendants. Outputs a numbered list (or JSON) with role
classification:

    role values:
      root       -- CLAUDE.md at cwd, when no CLAUDE.md exists above it (claude
                    was launched at the project top)
      ancestor   -- CLAUDE.md above cwd
      child      -- CLAUDE.md below cwd, OR at cwd when an ancestor CLAUDE.md
                    exists above it (a subordinate file, not the project root)
      local      -- CLAUDE.local.md at any of the above locations

Stdlib-only.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Iterable

# The shared walk lives in skills_kit_lib; make the plugin root importable
# regardless of which interpreter/venv launched this script (stdlib-only:
# skills_kit_lib degrades gracefully without pyyaml).
_PLUGIN_ROOT = Path(__file__).resolve().parents[3]
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from skills_kit_lib.dirwalk import iter_dirs  # noqa: E402


DESCEND_MAX_DEPTH = 6

# --- Code-directory dimension trigger (Level 1) -------------------------------
# A CLAUDE.md gets the code-directory insight-validation dimension (fidelity +
# value scrutiny) when it sits inside / describes a directory of code or data, OR
# when its body carries review-claim / shape markers. Otherwise it gets the
# classic placement+hygiene treatment only. The flag is mechanical (one dir
# listing + one regex pass) so it is idempotent. See code-dir-insight-filter.md
# and the proposal's section 5.0.

CODE_DATA_EXT = {
    ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".hh", ".cs", ".py", ".go",
    ".rs", ".java", ".kt", ".kts", ".swift", ".m", ".mm", ".ts", ".tsx", ".js",
    ".jsx", ".mjs", ".cjs", ".dart", ".lua", ".rb", ".php", ".scala", ".nim",
    ".nimble", ".lobster", ".sql", ".yaml", ".yml", ".csv", ".json", ".toml",
    ".proto", ".fbs", ".gradle", ".cmake", ".tf", ".bazel", ".bff",
    ".sh", ".bat", ".ps1", ".gd", ".tscn", ".awk",
    # Unreal descriptors: JSON text listing modules, plugins and dependencies --
    # readable, and the most coverage-relevant file in the directory holding it.
    # The engine's BINARY containers (.uasset/.umap/...) are the opposite case
    # and live in discover_coverage.ASSET_BINARY_EXT.
    ".uplugin", ".uproject",
}
# .kts (Kotlin script), .dart, .nim/.nimble, .lobster, .bazel, .bff (FASTBuild
# config -- load-bearing build config in at least one consuming project, not
# incidental), and .bat added 2026-08-26 from an evidenced corpus survey of
# DISCOVERY-FAILED directories (built from `p4 files`, not from this set's own
# classification): 62 directories held zero recognized extensions and were
# skipped entirely. `.sh` was already present.
# .md files that are docs, not review-notes; CLAUDE.md/local are the audited file.
_MD_LIKE = {".md", ".mdx", ".rst", ".txt"}
_CLAUDE_NAMES = {"CLAUDE.md", "CLAUDE.local.md"}

# Signal-B content markers (any hit flips the file to code-directory).
_SIGNAL_B = re.compile(
    r"(?im)"
    r"(^\s*#{1,4}\s*Review\s+Checks\b"          # Shape B payload heading
    r"|\bFORBIDDEN\b"                             # Shape C safety rail
    r"|gitignored|is a leak|clean checkout"       # negative-existence / Shape C
    r"|\(see\s+/"                                 # Shape D repo-root pointer
    r"|must match|don't copy|silent at build|search for usages"
    r"|lines?\s*~?\d"                             # line anchors
    r"|`[^`]+\.(?:cpp|h|hpp|cs|py|go|rs|ts|js|lua|yaml|yml|fbs)`"  # file anchors
    r")"
)
# Gotcha phrasing ("do not" / "don't" / "never") is too common in ordinary
# policy prose to flip a file on its own; it counts as Signal B only when the
# same line anchors the claim to code -- an inline-code span, a line anchor,
# or a source-file name.
_SIGNAL_B_GOTCHA = re.compile(
    r"(?im)^(?=.*\b(?:do not|don'?t|never)\b)"
    r".*(?:`[^`]+`|lines?\s*~?\d|\b\w+\.(?:cpp|h|hpp|cs|py|go|rs|ts|js|lua|yaml|yml|fbs)\b)"
)
# Negative guard: a declared claude_md: contract block forces classic.
_HAS_SCHEMA_BLOCK = re.compile(r"(?m)^\s*claude_md:\s*$")


def classify_dimension(claude_md_path: Path) -> str:
    """Return 'code-directory' or 'classic' for one CLAUDE.md.

    Level-1 trigger from the proposal: code-directory if (Signal A: the file's
    own directory is mostly code/data siblings) OR (Signal B: the body carries
    review-claim/shape markers). Negative guard forces 'classic' when the file
    declares a claude_md: schema block or sits in a skill directory (SKILL.md
    sibling). Best-effort and side-effect-free; any read error -> 'classic'.
    """
    try:
        directory = claude_md_path.parent
        # Negative guard: a skill directory's CLAUDE.md is decision-provenance,
        # not code-directory review notes -> classic.
        if (directory / "SKILL.md").exists():
            return "classic"

        # Signal A -- sibling extension tally (non-recursive, files only).
        code_data = 0
        md_like = 0
        try:
            for entry in directory.iterdir():
                if not entry.is_file() or entry.name in _CLAUDE_NAMES:
                    continue
                ext = entry.suffix.lower()
                if ext in CODE_DATA_EXT:
                    code_data += 1
                elif ext in _MD_LIKE:
                    md_like += 1
        except OSError:
            pass
        signal_a = code_data >= 1 and code_data >= md_like

        # Read the body once for the schema guard and Signal B.
        body = claude_md_path.read_text(encoding="utf-8", errors="ignore")
        if _HAS_SCHEMA_BLOCK.search(body):
            return "classic"
        signal_b = bool(_SIGNAL_B.search(body) or _SIGNAL_B_GOTCHA.search(body))

        return "code-directory" if (signal_a or signal_b) else "classic"
    except OSError:
        return "classic"


def find_project_root(cwd: Path) -> Path | None:
    """Return the project root: the nearest directory at or above cwd that holds
    a .git entry (directory or file). None when cwd is not inside a git repo --
    the audit then treats cwd as having no in-project ancestors.

    Deliberately git-ONLY, and the only such resolver in this scripts directory:
    the claude-md audit lane's ancestor scope is defined by the git repository
    boundary. Anything asking the broader question ("where does this PROJECT
    start", including a Perforce workspace) must use project_root.find_project_root
    instead -- do not widen this one.
    """
    current = cwd
    while True:
        if (current / ".git").exists():
            return current
        if current == current.parent:
            return None
        current = current.parent


def collect_ancestors(cwd: Path) -> list[tuple[Path, str]]:
    """Walk upward from cwd to the project root, collecting CLAUDE.md and
    CLAUDE.local.md. The walk stops at the project root (the .git boundary) and
    never scans directories outside the project. Returns (path, role) tuples
    ordered root-most-first. Empty when cwd is not in a git repo, or when cwd is
    itself the project root (nothing above it counts).
    """
    out: list[tuple[Path, str]] = []
    project_root = find_project_root(cwd)
    if project_root is None or cwd == project_root:
        return out
    current = cwd.parent
    while True:
        for name, role in (("CLAUDE.md", "ancestor"), ("CLAUDE.local.md", "local")):
            candidate = current / name
            if candidate.exists():
                out.append((candidate, role))
        if current == project_root:
            break
        current = current.parent
    out.reverse()
    return out


def collect_at_cwd(cwd: Path, has_ancestor_root: bool = False) -> list[tuple[Path, str]]:
    out: list[tuple[Path, str]] = []
    # The cwd CLAUDE.md is `root` only when it is the project top. If a CLAUDE.md
    # was found above cwd, claude was launched inside a larger project, so this
    # file is a subordinate (`child`) -- the project-root-only hygiene checks
    # (H1/H2/H3) belong to the real root above, not to the launch-dir file.
    cwd_role = "child" if has_ancestor_root else "root"
    for name, role in (("CLAUDE.md", cwd_role), ("CLAUDE.local.md", "local")):
        candidate = cwd / name
        if candidate.exists():
            out.append((candidate, role))
    return out


def collect_descendants(
    cwd: Path,
    include_dirs: Iterable[str] = (),
    skipped_out: list[Path] | None = None,
) -> list[tuple[Path, str]]:
    """`include_dirs` and `skipped_out` pass straight through to
    `iter_dirs` -- see its docstring. Without them the walk silently prunes
    every noise-named directory (and every dot-directory but `.claude`), so a
    CLAUDE.md sitting under a directory that happens to share a noise name
    (e.g. `tmp/`, `Build/`) is never discovered and nothing says so; the two
    parameters are how a caller opts a name back in and how it can report what
    was pruned instead of losing that information.
    """
    out: list[tuple[Path, str]] = []
    for current_path, files in iter_dirs(
        cwd, DESCEND_MAX_DEPTH, include_dirs=include_dirs, skipped_out=skipped_out
    ):
        if current_path == cwd:
            continue
        for name, role in (("CLAUDE.md", "child"), ("CLAUDE.local.md", "local")):
            if name in files:
                out.append((current_path / name, role))
    out.sort(key=lambda x: str(x[0]))
    return out


def discover(
    cwd: Path,
    include_dirs: Iterable[str] = (),
    skipped_out: list[Path] | None = None,
) -> list[tuple[Path, str]]:
    ancestors = collect_ancestors(cwd)
    # A CLAUDE.md ancestor (not a personal CLAUDE.local.md) above cwd means cwd
    # is not the project top, so the cwd CLAUDE.md is classified `child`.
    has_ancestor_root = any(role == "ancestor" for _, role in ancestors)
    out: list[tuple[Path, str]] = []
    out.extend(ancestors)
    out.extend(collect_at_cwd(cwd, has_ancestor_root))
    out.extend(collect_descendants(cwd, include_dirs=include_dirs, skipped_out=skipped_out))
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
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a numbered list")
    parser.add_argument("--cwd", default=None, help="override cwd (for testing)")
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
    results = discover(cwd, include_dirs=include_dirs, skipped_out=skipped_dirs)
    skipped_rel = sorted(
        str(_relative_or_self(p, cwd)) for p in skipped_dirs
    )

    # role=local files are personal-scoped; they never take the code-directory
    # dimension. Everything else gets the Level-1 trigger classified.
    def dim_for(path: Path, role: str) -> str:
        return "classic" if role == "local" else classify_dimension(path)

    if args.json:
        # Kept as a single flat LIST -- unchanged shape for the common case of
        # no skips -- rather than wrapped in a {"files": ...} envelope, so an
        # existing consumer that reads this as a bare array of file records
        # sees no difference when nothing was pruned. A skipped directory is
        # a distinct record shape (no "path"/"role"/"dimension"; a
        # "skipped_dir" key instead) appended after the file records.
        payload = [
            {"index": i + 1, "path": str(p), "role": role, "dimension": dim_for(p, role)}
            for i, (p, role) in enumerate(results)
        ]
        payload.extend({"skipped_dir": rel, "reason": "noise-name"} for rel in skipped_rel)
        print(json.dumps(payload, indent=2))
        return 0

    if not results:
        print(f"No CLAUDE.md or CLAUDE.local.md files found at or near {cwd}.")
    else:
        print(f"CLAUDE.md files visible from {cwd}:\n")
        for i, (path, role) in enumerate(results, start=1):
            try:
                display = path.relative_to(cwd)
            except ValueError:
                display = path
            dim = dim_for(path, role)
            tag = "code-dir" if dim == "code-directory" else "classic"
            print(f"  {i:>3}. [{role:<8}] [{tag:<8}] {display}")

    if skipped_rel:
        print(f"\nskipped {len(skipped_rel)} noise-named directory/ies (use --include-dir to opt one back in):")
        for rel in skipped_rel:
            print(f"  - {rel}")
    return 0


def _relative_or_self(path: Path, root: Path) -> Path:
    try:
        return path.relative_to(root)
    except ValueError:
        return path


if __name__ == "__main__":
    sys.exit(main())
