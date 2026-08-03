#!/usr/bin/env python3
"""discover_project_doc.py -- enumerate candidate PROJECT DOCUMENTS visible from a scan root.

A *project document* is a standalone reference doc that is NOT a SKILL.md, NOT a
CLAUDE.md / CLAUDE.local.md, and NOT inside a skill's references/ folder. These
are the docs the md-domain project-doc audit lane evaluates against the
cohesion-principles `project_reference_md` role + the skill-maturation pipeline:

    Docs/*.md, Docs/**/*.md.html (Markdeep), .claude/docs/*.md,
    <subsystem>/docs/*.md, README / NOTES / design docs / hand-off plans.

Skill-attached references (`*/skills/*/references/*.md`) and CLAUDE.md / SKILL.md
files are deliberately EXCLUDED -- they are audited by md-domain's skill audit
and claude-md audit lanes respectively. Vendored / generated trees are skipped.

Usage:
    python discover_project_doc.py           # numbered list, scan root = cwd
    python discover_project_doc.py --json    # JSON for the audit workflow
    python discover_project_doc.py --root PATH           # scan a specific subtree
    python discover_project_doc.py --path FILE [...]      # classify specific files only

For each candidate it emits mechanical signals the audit lanes consume:

    kind              project_doc | skill_reference | other_claude_artifact
    role_hint         readme | null -- docs judged under a special per-artifact
                      role (README = human-facing derived brief)
    generated         true when a generated_artifact provenance signal exists
    generation_record sidecar:<name> | marker:<line> | null -- the signal
                      (generated docs are audited for provenance ONLY)
    lines             effective line count
    approx_tokens     ~chars/4
    inbound_citations number of OTHER text files that mention this doc by name
                      (0 == orphan: nothing in the load graph points here). The
                      scan covers the project tree AND installed plugin-cache
                      skills (SKILL.md + references) from the harness plugin
                      cache, so a doc referenced only by an installed plugin
                      skill is NOT reported as an orphan.
    cited_by          up to a few example citing paths (orphan triage)

Stdlib-only; degrades gracefully without skills_kit_lib.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# The shared walk lives in skills_kit_lib; make the plugin root importable
# regardless of which interpreter/venv launched this script.
_PLUGIN_ROOT = Path(__file__).resolve().parents[3]
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

try:
    from skills_kit_lib.dirwalk import iter_dirs  # noqa: E402
except Exception:  # pragma: no cover - fallback when the lib is unavailable
    iter_dirs = None


SCAN_MAX_DEPTH = 8

# Extensions that count as a project document.
DOC_EXT = {".md", ".mdx", ".rst", ".txt"}
# .md.html is a compound suffix (Markdeep); handled by name, not Path.suffix.
MARKDEEP_SUFFIX = ".md.html"

# Files that are other CLAUDE-artifacts, not project docs (audited elsewhere).
_NOT_PROJECT_DOC_NAMES = {"SKILL.md", "CLAUDE.md", "CLAUDE.local.md"}

# Directory names that are vendored, generated, or build output -- never project
# docs. Matched against any path segment.
_SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".pytest_cache", ".venv", "venv",
    "site-packages", "dist", "build", "Intermediate", "Binaries", "Saved",
    "Engine", "ThirdParty", "External", "uvcache", "stubs", "Generated",
    "generated", ".mypy_cache", ".ruff_cache", "DerivedDataCache",
}

# Text file extensions that can plausibly *cite* a project doc (for the inbound
# citation / orphan scan). We read these to see who points at each candidate.
_CITER_EXT = {".md", ".mdx", ".rst", ".txt", ".yaml", ".yml", ".json"}

_SKILL_REF_RE = re.compile(r"[/\\]skills[/\\][^/\\]+[/\\]references[/\\]")

# --- generated_artifact role signals (cohesion-principles generated_artifact) ---
# A committed generated output is identified by a machine-readable generation-
# record sidecar next to it (e.g. top50.params.json for top50.md) or an in-file
# marker in the first ~20 lines. The audit then checks ONLY provenance.
_GENERATION_SIDECAR_SUFFIXES = (".params.json", ".recipe.json", ".gen.json")
_GENERATED_MARKER_RE = re.compile(
    r"generated (?:analysis|by|from|with)|auto-?generated"
    r"|do not edit(?: by hand)?|this (?:file|document) (?:is|was) generated",
    re.IGNORECASE,
)


def _doc_stem(name: str) -> str:
    """Strip the doc extension, handling the compound Markdeep suffix."""
    lower = name.lower()
    if lower.endswith(MARKDEEP_SUFFIX):
        return name[: -len(MARKDEEP_SUFFIX)]
    return name[: -len(Path(name).suffix)] if Path(name).suffix else name


def generation_record(path: Path) -> str | None:
    """Provenance signal for the generated_artifact role, or None.

    Returns "sidecar:<name>" when a generation-record sidecar sits next to the
    artifact, "marker:<line>" when an in-file generation marker appears in the
    first 20 lines.
    """
    stem = _doc_stem(path.name)
    for suffix in _GENERATION_SIDECAR_SUFFIXES:
        sidecar = path.with_name(stem + suffix)
        if sidecar.exists():
            return f"sidecar:{sidecar.name}"
    try:
        head = path.read_text(encoding="utf-8", errors="ignore")[:4000]
    except OSError:
        return None
    for line in head.splitlines()[:20]:
        if _GENERATED_MARKER_RE.search(line):
            return f"marker:{line.strip()[:120]}"
    return None


def role_hint(path: Path) -> str | None:
    """Named-role hint for docs judged under a special per-artifact role:
    'readme' (human-facing derived brief, cohesion-principles readme_md)."""
    if path.name.lower().split(".")[0] == "readme":
        return "readme"
    return None


# Project-root markers, VCS-agnostic: git, mercurial, svn, AND perforce
# (.p4config.txt) -- the audited project may not be a git repo.
_PROJECT_MARKERS = (".git", ".hg", ".svn", ".p4config.txt")


def find_project_root(start: Path) -> Path | None:
    """Nearest ancestor of `start` (inclusive) holding a project marker, else None.

    Used to scope the inbound-citation (orphan) scan to the whole project even
    when only a subdirectory is being audited -- a doc under .claude/docs is
    cited from CLAUDE.md / skills elsewhere in the repo, so an orphan check that
    only scanned .claude/docs would false-positive on nearly everything. Markers
    cover git/hg/svn and Perforce (.p4config.txt) so non-git projects resolve too.
    """
    current = start if start.is_dir() else start.parent
    while True:
        if any((current / marker).exists() for marker in _PROJECT_MARKERS):
            return current
        if current == current.parent:
            return None
        current = current.parent


def _config_dir() -> Path:
    """The harness config dir: $CLAUDE_CONFIG_DIR if set, else ~/.claude."""
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".claude"


def _read_enabled_plugin_names(config_dir: Path, project_root: Path | None) -> set[str]:
    """Best-effort set of ENABLED plugin names from settings.json files.

    Reads `enabledPlugins` from the user (`<config>/settings.json`) and project
    (`<project>/.claude/settings.json` + `settings.local.json`) settings. The
    field's shape has varied across harness versions, so accept all of:
        {"name@marketplace": true, ...}
        ["name@marketplace", ...]
        {"marketplace": {"name": true, ...}, ...}
    Returns bare plugin names (the part before any '@'). An EMPTY result means
    "could not determine" -- the caller then falls back to every cached plugin
    (over-inclusion only ever removes a false orphan, never adds one).
    """
    names: set[str] = set()
    setting_files = [config_dir / "settings.json"]
    if project_root is not None:
        setting_files += [
            project_root / ".claude" / "settings.json",
            project_root / ".claude" / "settings.local.json",
        ]
    for sf in setting_files:
        try:
            data = json.loads(sf.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        ep = data.get("enabledPlugins") if isinstance(data, dict) else None
        if isinstance(ep, dict):
            for key, val in ep.items():
                if isinstance(val, dict):
                    for name, flag in val.items():
                        if flag:
                            names.add(str(name).split("@", 1)[0])
                elif val:
                    names.add(str(key).split("@", 1)[0])
        elif isinstance(ep, list):
            for key in ep:
                names.add(str(key).split("@", 1)[0])
    return names


def _highest_version_dir(plugin_dir: Path) -> Path | None:
    """The highest-version subdir of a cached plugin (lexical sort proxy)."""
    version_dirs = [d for d in plugin_dir.iterdir() if d.is_dir()]
    if not version_dirs:
        return None
    version_dirs.sort(key=lambda d: d.name)
    return version_dirs[-1]


def plugin_cache_citer_files(config_dir: Path, project_root: Path | None):
    """Yield citer files from installed plugin-cache skills.

    Harness cache layout: `<config>/plugins/cache/<marketplace>/<plugin>/<version>/`.
    For each plugin the highest version dir is scanned; its skills/ subtree (or
    the whole install if there is no skills/) contributes SKILL.md + reference
    docs as inbound-citation sources. When an enabled-plugin set is resolvable
    it filters to those; otherwise every cached plugin is included. Cached
    plugins come from the harness's configured marketplaces by construction.
    """
    cache_root = config_dir / "plugins" / "cache"
    if not cache_root.is_dir():
        return
    enabled = _read_enabled_plugin_names(config_dir, project_root)
    try:
        mkt_dirs = sorted(d for d in cache_root.iterdir() if d.is_dir())
    except OSError:
        return
    for mkt_dir in mkt_dirs:
        try:
            plugin_dirs = sorted(d for d in mkt_dir.iterdir() if d.is_dir())
        except OSError:
            continue
        for plugin_dir in plugin_dirs:
            if enabled and plugin_dir.name not in enabled:
                continue
            chosen = _highest_version_dir(plugin_dir)
            if chosen is None:
                continue
            skills_root = chosen / "skills"
            base = skills_root if skills_root.is_dir() else chosen
            for f in _walk_files(base):
                if f.suffix.lower() in _CITER_EXT or f.name.lower().endswith(
                    MARKDEEP_SUFFIX
                ):
                    yield f


def is_doc_file(name: str) -> bool:
    """True when a filename looks like a project document by extension."""
    lower = name.lower()
    if lower.endswith(MARKDEEP_SUFFIX):
        return True
    return Path(name).suffix.lower() in DOC_EXT


def classify_kind(path: Path) -> str:
    """Classify a doc-shaped path into the kind the audit cares about."""
    name = path.name
    if name in _NOT_PROJECT_DOC_NAMES:
        return "other_claude_artifact"
    posix = path.as_posix()
    if _SKILL_REF_RE.search(posix) or _SKILL_REF_RE.search(str(path)):
        return "skill_reference"
    return "project_doc"


def _has_skipped_segment(path: Path, root: Path) -> bool:
    try:
        rel_parts = path.relative_to(root).parts
    except ValueError:
        rel_parts = path.parts
    return any(part in _SKIP_DIRS for part in rel_parts)


def _walk_files(root: Path):
    """Yield every file under root, honoring depth + skip-dir rules.

    Uses skills_kit_lib.dirwalk when present (shared excludes); otherwise a
    bounded stdlib walk with the same skip-dir set.
    """
    if iter_dirs is not None:
        for dir_path, files in iter_dirs(root, SCAN_MAX_DEPTH):
            if _has_skipped_segment(dir_path, root):
                continue
            for fname in files:
                yield dir_path / fname
        return
    # Fallback: manual bounded walk.
    root_depth = len(root.parts)
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir():
                if entry.name in _SKIP_DIRS:
                    continue
                if len(entry.parts) - root_depth >= SCAN_MAX_DEPTH:
                    continue
                stack.append(entry)
            elif entry.is_file():
                yield entry


def _measure(path: Path) -> tuple[int, int]:
    """Return (effective_lines, approx_tokens) for a doc, best-effort."""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return (0, 0)
    lines = [ln for ln in text.splitlines()]
    # Effective lines: drop trailing blank lines.
    while lines and not lines[-1].strip():
        lines.pop()
    return (len(lines), len(text) // 4)


def _git_ignored(paths: list[Path], root: Path) -> set[str]:
    """The subset of `paths` git reports as ignored, as strings.

    Best-effort and VCS-tolerant: returns an empty set when git is absent, the
    root is not inside a git work tree, or the invocation fails for any reason
    -- discovery then behaves exactly as before. Uses `git check-ignore
    --stdin -z` so one subprocess covers the whole candidate list.
    """
    if not paths:
        return set()
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "check-ignore", "--stdin", "-z"],
            input="\0".join(str(p) for p in paths),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    # Exit 0: some ignored (listed on stdout). Exit 1: none ignored.
    # Anything else (128: not a repo / bad usage): treat as no information.
    if proc.returncode not in (0, 1):
        return set()
    return {p for p in proc.stdout.split("\0") if p}


def collect_candidates(root: Path) -> list[Path]:
    out: list[Path] = []
    for path in _walk_files(root):
        if not is_doc_file(path.name):
            continue
        if _has_skipped_segment(path, root):
            continue
        out.append(path)
    # Drop gitignored candidates (build artifacts like *.egg-info that the
    # skip-dir set does not enumerate). Explicit --path nominations bypass
    # this by construction -- they never go through collect_candidates.
    ignored = _git_ignored(out, root)
    if ignored:
        out = [p for p in out if str(p) not in ignored]
    out.sort(key=lambda p: str(p))
    return out


def _index_citer(
    path: Path,
    basenames: set[str],
    candidate_paths: set[str],
    inbound: dict[str, set[str]],
) -> None:
    """Record which candidate basenames a single citer file mentions."""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return
    spath = str(path)
    for name in basenames:
        if name in text:
            # Exclude the doc citing itself.
            if spath in candidate_paths and Path(spath).name == name:
                continue
            inbound[name].add(spath)


def build_inbound_index(
    root: Path,
    candidates: list[Path],
    extra_citer_files=None,
) -> dict[str, list[Path]]:
    """One pass over citer files: map each candidate basename -> citing files.

    A candidate is "cited" when its basename appears verbatim in another text
    file (a CLAUDE.md pointer, a SKILL.md reference, a doc-to-doc link, a config
    entry). Self-mentions are excluded. This is the orphan signal: an empty
    list means nothing points at the doc.

    Citer sources are the project tree under `root` PLUS `extra_citer_files` --
    files outside the project tree (installed plugin-cache skills) that are still
    part of the load graph. A doc referenced only by an installed plugin skill is
    therefore NOT an orphan.
    """
    basenames = {p.name for p in candidates}
    # Map basename -> set of citing paths.
    inbound: dict[str, set[str]] = {name: set() for name in basenames}
    candidate_paths = {str(p) for p in candidates}

    for path in _walk_files(root):
        if path.suffix.lower() not in _CITER_EXT and not path.name.lower().endswith(
            MARKDEEP_SUFFIX
        ):
            continue
        if _has_skipped_segment(path, root):
            continue
        _index_citer(path, basenames, candidate_paths, inbound)

    # Extra citers live OUTSIDE the project tree (plugin cache), so the
    # in-tree skip-segment check does not apply -- index them directly.
    for path in extra_citer_files or ():
        _index_citer(path, basenames, candidate_paths, inbound)

    return {name: sorted(Path(s) for s in paths) for name, paths in inbound.items()}


def describe(path: Path, inbound: dict[str, list[Path]], root: Path) -> dict:
    kind = classify_kind(path)
    lines, approx_tokens = _measure(path)
    citing = inbound.get(path.name, [])
    # Exclude self if the basename collides with the candidate itself.
    citing = [c for c in citing if c != path]
    cited_by = []
    for c in citing[:5]:
        try:
            cited_by.append(str(c.relative_to(root)))
        except ValueError:
            cited_by.append(str(c))
    gen_record = generation_record(path)
    return {
        "path": str(path),
        "kind": kind,
        "role_hint": role_hint(path),
        "generated": gen_record is not None,
        "generation_record": gen_record,
        "lines": lines,
        "approx_tokens": approx_tokens,
        "inbound_citations": len(citing),
        "cited_by": cited_by,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a numbered list")
    parser.add_argument("--root", default=None,
                        help="candidate scan root -- where project docs are enumerated (default: cwd)")
    parser.add_argument("--citer-root", default=None,
                        help="root for the inbound-citation (orphan) scan. Default: the project "
                             "root (.git ancestor) of the candidates, else --root. Decoupled from "
                             "--root so auditing a subdirectory (e.g. .claude/docs) still detects "
                             "citations from CLAUDE.md / skills elsewhere in the project.")
    parser.add_argument("--path", action="append", default=None,
                        help="classify only this file (repeatable); skips the candidate tree walk")
    parser.add_argument("--skip-plugin-cache", action="store_true",
                        help="do NOT index installed plugin-cache skills as citation sources "
                             "(default: index them, so a doc referenced only by an installed "
                             "plugin skill is not falsely reported as an orphan)")
    parser.add_argument("--config-dir", default=None,
                        help="harness config dir holding plugins/cache (default: $CLAUDE_CONFIG_DIR "
                             "or ~/.claude). Rarely needed; mainly for testing.")
    args = parser.parse_args()

    root = Path(args.root).resolve() if args.root else Path.cwd().resolve()

    if args.path:
        candidates = [Path(p).resolve() for p in args.path]
    else:
        candidates = collect_candidates(root)

    # The citer scan root is decoupled from the candidate root: an orphan check
    # must see the WHOLE project (a .claude/docs doc is cited from CLAUDE.md /
    # skills outside that folder). Default to the candidates' .git project root;
    # fall back to --root when there is no git boundary.
    if args.citer_root:
        citer_root = Path(args.citer_root).resolve()
    else:
        anchor = candidates[0].parent if candidates else root
        # VCS marker (git/hg/svn/p4) -> the directory the audit was launched from
        # (usually the project top) -> never silently the candidate subdir, which
        # would under-count citations and false-flag orphans.
        citer_root = find_project_root(anchor) or Path.cwd().resolve()

    # Installed plugin-cache skills are part of the load graph but live outside
    # the project tree, so scan them as additional citation sources (unless
    # disabled). A repo doc referenced only by an installed plugin skill is not
    # an orphan.
    extra_citers = None
    if not args.skip_plugin_cache:
        config_dir = Path(args.config_dir).expanduser() if args.config_dir else _config_dir()
        extra_citers = list(plugin_cache_citer_files(config_dir, project_root=citer_root))

    inbound = build_inbound_index(citer_root, candidates, extra_citer_files=extra_citers)
    records = [describe(p, inbound, citer_root) for p in candidates]

    if args.json:
        print(json.dumps(
            [{"index": i + 1, **rec} for i, rec in enumerate(records)], indent=2))
        return 0

    if not records:
        print(f"No project documents found under {root}.")
        return 0

    print(f"Project documents under {root}:\n")
    for i, rec in enumerate(records, start=1):
        try:
            display = Path(rec["path"]).relative_to(root)
        except ValueError:
            display = rec["path"]
        orphan = "ORPHAN" if rec["inbound_citations"] == 0 else f"{rec['inbound_citations']} ref"
        kind = rec["kind"]
        tag = "proj-doc" if kind == "project_doc" else (
            "skill-ref" if kind == "skill_reference" else "claude-art")
        extras = ""
        if rec.get("generated"):
            extras += " [generated]"
        if rec.get("role_hint"):
            extras += f" [{rec['role_hint']}]"
        print(f"  {i:>3}. [{tag:<10}] [{rec['lines']:>4}L] [{orphan:<7}] {display}{extras}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
