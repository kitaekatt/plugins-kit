"""references_audit.py -- static analysis for Claude Code skill cross-references.

Scans markdown files for `/skill-name` references and Skill-tool invocations,
builds a dependency graph against the discovered skill pool, and reports broken
references. The scan scope is configurable:

- skills      -- SKILL.md only (default, backward-compatible with the original
                  skill-deps behavior).
- references  -- every *.md file inside a skill directory that is not SKILL.md
                  (e.g. references/*.md alongside a skill, per-skill CLAUDE.md).
- md          -- every *.md file under the scan roots.
- all         -- alias for md.

Scopes can be combined with commas (e.g. --scope skills,references).
--path FILE narrows the scan to one specific file (may be repeated); the skill
pool is still loaded from the full scan roots so refs resolve correctly.

--ignore-dir GLOB and --ignore-file GLOB (both repeatable) skip files whose
path matches the supplied fnmatch glob. Useful for harness transcript dirs and
vendored third-party docs that produce systematic false positives.

--json emits a structured machine-readable report instead of markdown. Each
finding carries file path, 1-indexed line number, ref name, severity, and a
category hint, so downstream tooling can classify and apply mechanical fixes.

Fenced code blocks (``` and ~~~), inline code spans (single/double-backtick
`...` runs), and YAML frontmatter are masked before scanning, so refs that
appear inside them (e.g. a backticked `/route` endpoint or `$/unit` notation)
do not trigger findings.

A file can declare a per-file allowlist for legacy / historical references
that intentionally do not resolve, via a comma-separated YAML frontmatter
field `references-audit-allow-stale` (preferred) or the legacy spelling
`audit-references-allow-stale` (kept for back-compat with files written
before the 0.8.0 rename):

    ---
    references-audit-allow-stale: plan, designer-plan, rollback-to-preflight
    ---

Listed bare names are silenced for both soft refs (`/plan`) and hard deps
(`skill: "plan"`) -- but only inside that file, and only for the names on
the list. Any new broken reference in the same file still fires. This is
preferred over `--ignore-file` for historical artifacts: it co-locates the
exception with the doc that owns it and stays granular.

Exit codes: 0 = clean (no errors), 1 = broken hard dependencies found.
"""

import argparse
import fnmatch
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Shared substrate (frontmatter parsing + plugin-tier discovery) lives in
# skills_kit_lib; make the plugin root importable regardless of which
# interpreter/venv launched this script. No third-party dependencies are
# required (skills_kit_lib degrades gracefully without pyyaml).
_PLUGIN_ROOT = Path(__file__).resolve().parents[3]
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from skills_kit_lib.corpus import PluginEntry, discover_corpus  # noqa: E402
from skills_kit_lib.markdown_heuristics import (  # noqa: E402
    parse_frontmatter as _lib_parse_frontmatter,
)


@dataclass
class SkillInfo:
    """A discovered skill (one SKILL.md). Defines a resolvable name."""
    name: str
    path: Path
    source: str  # "project", "user", or "plugin:<plugin-id>"


@dataclass
class SourceFile:
    """A markdown file the scanner reads for cross-references. May or may not
    be a SKILL.md."""
    path: Path
    kind: str       # "skill", "reference", "md"
    source: str     # "project", "user", "plugin:<id>", or "explicit"
    skill_name: str | None  # set when kind == "skill"
    hard_deps: list[tuple[str, int]] = field(default_factory=list)
    soft_refs: list[tuple[str, int]] = field(default_factory=list)
    allow_stale: set[str] = field(default_factory=set)  # frontmatter allowlist


# Claude CLI builtins -- not skills, just slash commands
BUILTIN_COMMANDS = {
    "help", "clear", "tasks", "init", "login", "logout",
    "status", "memory", "compact", "cost", "doctor", "fast",
    "review", "code-review", "security-review", "bug",
    "terminal-setup", "vim", "model",
    "add-dir", "mcp", "config", "permissions", "listen",
    "reload-plugins", "agents", "hooks", "output-style",
    "statusline", "resume", "release-notes",
}

# Skills that exist at runtime via plugins/extensions but have no SKILL.md
EXTERNAL_SKILLS = {
    "simplify", "keybindings-help", "claude-developer-platform",
}

# Documentation conventions: any reference to `/<prefix>:<anything>` (or the
# bare `/<prefix>`) is treated as a non-skill placeholder and never reported
# as broken.
#
# - /example:*  -- illustrative syntax in documentation
# - /proposed:* -- a planned skill that does not yet exist
PLACEHOLDER_REF_PREFIXES = {"example", "proposed"}


def parse_frontmatter(text: str) -> dict:
    """Extract YAML frontmatter fields via the shared light (regex) parser."""
    fm = _lib_parse_frontmatter(text)
    return dict(fm.fields) if fm is not None else {}


def find_hard_deps(body: str) -> list[tuple[str, int]]:
    """Find `skill: "name"` Skill-tool invocations. Returns (name, line) pairs,
    line numbers 1-indexed against the input string."""
    out: list[tuple[str, int]] = []
    for m in re.finditer(r'skill:\s*["\']([a-z][a-z0-9-]*)["\']', body):
        line = body.count("\n", 0, m.start()) + 1
        out.append((m.group(1), line))
    return out


# Generic path segments and conjunctions that appear in documentation but are
# not skill references. Keep this list GENERIC -- project-specific vocabulary
# belongs in the per-file `references-audit-allow-stale` frontmatter mechanism
# (or --ignore-dir/--ignore-file), never hardcoded here: a hardcoded foreign
# vocabulary masks real findings in every other project and rots.
NON_SKILL_WORDS = {
    "build", "dev", "tmp", "path", "c", "var", "usr", "etc", "home", "opt", "bin",
    "if", "or", "and", "no", "not",
}

EXCLUDED_REFS = BUILTIN_COMMANDS | EXTERNAL_SKILLS | NON_SKILL_WORDS


def find_soft_refs(body: str) -> list[tuple[str, int]]:
    """Find `/skill-name` references, excluding builtins, externals, and paths.
    Returns (ref, line) pairs, line numbers 1-indexed against the input string.
    Duplicates on the same line are collapsed; same ref on different lines
    yields multiple findings so callers can target precise edit positions."""
    name_part = r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*"
    pattern = (
        r"(?<![a-zA-Z0-9_.:/\\>+*])/("
        + name_part
        + r"(?::"
        + name_part
        + r")?)\b(?![-/])"
    )
    out: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    for m in re.finditer(pattern, body):
        ref = m.group(1)
        head = ref.split(":", 1)[0]
        if head in PLACEHOLDER_REF_PREFIXES:
            continue
        bare = ref.split(":", 1)[-1] if ":" in ref else ref
        if bare in EXCLUDED_REFS:
            continue
        line = body.count("\n", 0, m.start()) + 1
        key = (ref, line)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


# Inline code span: a run of N backticks opens the span and the next run of
# exactly N backticks closes it (single- and double-backtick spans are the
# common cases). Content and delimiters are replaced with spaces of equal
# length, so line/column offsets are preserved.
_INLINE_CODE_RE = re.compile(r"(`+)(.+?)\1")


def _mask_inline_code(line: str) -> str:
    """Blank out inline code-span regions on a single line, preserving length
    (no newlines are involved, so line numbers are unaffected)."""
    return _INLINE_CODE_RE.sub(lambda m: " " * len(m.group(0)), line)


def mask_non_scanned(text: str) -> str:
    """Return text with frontmatter, fenced code-block regions, and inline
    code spans replaced by blanks. Line count and offsets are preserved so
    soft/hard-ref matches align with original file line numbers."""
    lines = text.split("\n")
    out: list[str] = []
    in_frontmatter = lines and lines[0].strip() == "---"
    fence_marker: str | None = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if in_frontmatter:
            out.append("")
            if i > 0 and stripped == "---":
                in_frontmatter = False
            continue
        if fence_marker is None:
            # Opening fence: ``` or ~~~ (optionally followed by lang tag)
            if stripped.startswith("```") or stripped.startswith("~~~"):
                fence_marker = stripped[:3]
                out.append("")
                continue
            out.append(_mask_inline_code(line))
        else:
            out.append("")
            if stripped.startswith(fence_marker):
                fence_marker = None
    return "\n".join(out)


def is_skill_md(path: Path) -> bool:
    """Case-insensitive SKILL.md check."""
    return path.name.lower() == "skill.md"


# ---------------------------------------------------------------------------
# Discovery -- skills (the resolvable name pool)
# ---------------------------------------------------------------------------


def find_project_skill_roots(base_dir: Path) -> list[Path]:
    """Resolve `--project-dir` to one or more `.claude/skills` directories.

    Projects commonly nest skill directories under sub-trees (for example
    `.teamcity/.claude/skills/`). When `--project-dir` points at a project
    root or at the top-level `.claude/skills`, walk the implied project root
    and return every `.claude/skills` directory found. Duplicates are
    deduplicated, results are returned in deterministic path order.
    """
    if not base_dir.exists():
        return []
    if base_dir.name == "skills" and base_dir.parent.name == ".claude":
        project_root = base_dir.parent.parent
    else:
        project_root = base_dir
    roots: set[Path] = set()
    if base_dir.name == "skills" and base_dir.parent.name == ".claude":
        roots.add(base_dir.resolve())
    if project_root.exists():
        for skills_dir in project_root.rglob("skills"):
            if (
                skills_dir.is_dir()
                and skills_dir.parent.name == ".claude"
                and skills_dir.name == "skills"
            ):
                roots.add(skills_dir.resolve())
    return sorted(roots)


def discover_skills(base_dir: Path, source: str) -> list[SkillInfo]:
    """Find all SKILL.md files under every `.claude/skills` root reachable
    from `base_dir`. Returns one SkillInfo per discovered SKILL.md."""
    skills = []
    seen: set[Path] = set()
    for root in find_project_skill_roots(base_dir):
        for skill_file in sorted(
            root.rglob("[Ss][Kk][Ii][Ll][Ll].[Mm][Dd]")
        ):
            resolved = skill_file.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            text = skill_file.read_text(encoding="utf-8", errors="replace")
            fm = parse_frontmatter(text)
            name = fm.get("name", "")
            if not name:
                continue
            skills.append(SkillInfo(name=name, path=skill_file, source=source))
    return skills


def discover_plugin_entries(plugins_dir: Path | None) -> list[PluginEntry]:
    """ONE installed_plugins.json parse, via skills_kit_lib.corpus (the shared
    discovery substrate). The returned PluginEntry list feeds the skill pool,
    the skill-dir classification set, and the scan roots -- the three former
    per-call-site manifest walks collapsed to this single discovery."""
    if not plugins_dir or not plugins_dir.exists():
        return []
    manifest = plugins_dir / "installed_plugins.json"
    if not manifest.exists():
        return []
    corpus = discover_corpus(
        # Plugin tier only: point the user tier at a non-existent root (this
        # script handles project/user discovery itself, with nested
        # .claude/skills semantics corpus does not implement).
        user_skills_root=manifest / "__no_user_tier__",
        installed_plugins_json=manifest,
    )
    # Drop registry entries without an installPath (corpus records them with
    # an empty path; scanning a relative "skills" dir would be wrong).
    return [p for p in corpus.plugins if str(p.install_path) not in ("", ".")]


def plugin_id_of(entry: PluginEntry) -> str:
    """Reconstruct the installed_plugins.json manifest key for an entry."""
    if entry.marketplace and entry.marketplace != "(unknown)":
        return f"{entry.name}@{entry.marketplace}"
    return entry.name


def plugin_skill_infos(entries: list[PluginEntry]) -> list[SkillInfo]:
    """Skill identities from installed plugins (pool entries need a name)."""
    skills: list[SkillInfo] = []
    for entry in entries:
        for rec in entry.skills:
            name = rec.frontmatter.get("name")
            if not name or not isinstance(name, str):
                continue
            skills.append(SkillInfo(
                name=name,
                path=rec.path,
                source=f"plugin:{plugin_id_of(entry)}",
            ))
    return skills


def collect_skill_dirs(
    project_dir: Path,
    user_dir: Path | None,
    plugin_entries: list[PluginEntry],
) -> set[Path]:
    """Return every directory that directly contains a SKILL.md across all
    scan roots. Used to classify non-SKILL .md files as 'reference' kind."""
    skill_dirs: set[Path] = set()
    project_roots: list[Path] = []
    if project_dir:
        project_roots.extend(find_project_skill_roots(project_dir))
    if user_dir:
        project_roots.extend(find_project_skill_roots(user_dir))
    for root in project_roots:
        for sm in root.rglob("[Ss][Kk][Ii][Ll][Ll].[Mm][Dd]"):
            skill_dirs.add(sm.parent)
    for entry in plugin_entries:
        for rec in entry.skills:
            skill_dirs.add(rec.path.parent)
    return skill_dirs


# ---------------------------------------------------------------------------
# Discovery -- source files (everything we scan for cross-references)
# ---------------------------------------------------------------------------


def classify_md(md_path: Path, skill_dirs: set[Path]) -> str:
    """Classify an .md path into 'skill' / 'reference' / 'md'."""
    if is_skill_md(md_path):
        return "skill"
    for d in skill_dirs:
        try:
            md_path.relative_to(d)
        except ValueError:
            continue
        return "reference"
    return "md"


def build_source_file(
    md_path: Path,
    kind: str,
    source: str,
) -> SourceFile:
    text = md_path.read_text(encoding="utf-8", errors="replace")
    fm = parse_frontmatter(text)
    skill_name = fm.get("name") or None if kind == "skill" else None
    # Prefer the new field name; fall back to the legacy spelling for files
    # written before the 0.8.0 rename.
    allow_stale_raw = fm.get(
        "references-audit-allow-stale",
        fm.get("audit-references-allow-stale", ""),
    )
    allow_stale = {
        token.strip()
        for token in allow_stale_raw.replace("[", " ").replace("]", " ").split(",")
        if token.strip()
    }
    body = mask_non_scanned(text)
    return SourceFile(
        path=md_path,
        kind=kind,
        source=source,
        skill_name=skill_name,
        allow_stale=allow_stale,
        hard_deps=find_hard_deps(body),
        soft_refs=find_soft_refs(body),
    )


def is_ignored(
    path: Path,
    ignore_dirs: list[str],
    ignore_files: list[str],
) -> bool:
    """Return True if `path` should be skipped per `--ignore-dir` /
    `--ignore-file` globs. Globs are matched against both the full path
    (forward-slashed) and the basename, so callers can pass either."""
    norm = str(path).replace("\\", "/")
    for pat in ignore_files:
        if fnmatch.fnmatch(norm, pat) or fnmatch.fnmatch(path.name, pat):
            return True
    for pat in ignore_dirs:
        for ancestor in path.parents:
            anc_norm = str(ancestor).replace("\\", "/")
            if fnmatch.fnmatch(anc_norm, pat) or fnmatch.fnmatch(
                ancestor.name, pat
            ):
                return True
    return False


def discover_source_files(
    base_dirs: list[tuple[Path, str]],
    scopes: set[str],
    skill_dirs: set[Path],
    ignore_dirs: list[str],
    ignore_files: list[str],
) -> list[SourceFile]:
    """Walk each (base_dir, source-label) pair and collect SourceFile entries
    matching the requested scopes."""
    md_scope = "md" in scopes
    sources: list[SourceFile] = []
    seen: set[Path] = set()

    for base_dir, source_label in base_dirs:
        if not base_dir or not base_dir.exists():
            continue
        for md_path in sorted(p for p in base_dir.rglob("*.md") if p.is_file()):
            if md_path in seen:
                continue
            seen.add(md_path)
            if is_ignored(md_path, ignore_dirs, ignore_files):
                continue
            kind = classify_md(md_path, skill_dirs)
            include = (
                (kind == "skill" and "skills" in scopes)
                or (kind == "reference" and "references" in scopes)
                or md_scope
            )
            if not include:
                continue
            sources.append(build_source_file(md_path, kind, source_label))
    return sources


def scan_explicit_paths(
    paths: list[Path],
    skill_dirs: set[Path],
    ignore_dirs: list[str],
    ignore_files: list[str],
) -> list[SourceFile]:
    """For each --path argument, scan the file and classify it."""
    out: list[SourceFile] = []
    for p in paths:
        p = p.resolve()
        if not p.exists() or not p.is_file():
            print(
                f"WARNING: --path target does not exist or is not a file: {p}",
                file=sys.stderr,
            )
            continue
        if is_ignored(p, ignore_dirs, ignore_files):
            continue
        kind = classify_md(p, skill_dirs)
        out.append(build_source_file(p, kind, "explicit"))
    return out


# ---------------------------------------------------------------------------
# Analysis + reporting
# ---------------------------------------------------------------------------


def expected_name_from_path(skill_path: Path, base_dir: Path) -> str | None:
    """Derive expected skill name from directory structure.

    .claude/skills/cl-audit/SKILL.md -> "cl-audit"
    .claude/skills/git/sync/SKILL.md -> "git-sync"
    """
    try:
        rel = skill_path.parent.relative_to(base_dir)
    except ValueError:
        return None
    parts = rel.parts
    if not parts:
        return None
    return "-".join(parts)


def build_name_map(skills: list[SkillInfo]) -> dict[str, list[SkillInfo]]:
    """Index skills by bare name AND, for plugin skills, by qualified
    `<plugin-name>:<skill>` form."""
    name_map: dict[str, list[SkillInfo]] = {}
    for s in skills:
        name_map.setdefault(s.name, []).append(s)
        if s.source.startswith("plugin:"):
            plugin_id = s.source.split(":", 1)[1]
            plugin_name = plugin_id.split("@", 1)[0]
            name_map.setdefault(f"{plugin_name}:{s.name}", []).append(s)
    return name_map


def parse_scope_arg(raw: str) -> set[str]:
    """Parse a --scope value like 'skills,references' into a set. 'all' is
    expanded to 'md'. Empty string falls back to {'skills'}."""
    valid = {"skills", "references", "md", "all"}
    out: set[str] = set()
    for token in raw.split(","):
        t = token.strip().lower()
        if not t:
            continue
        if t not in valid:
            raise ValueError(f"unknown scope '{t}'. Valid: {sorted(valid)}")
        out.add(t)
    if "all" in out:
        out.discard("all")
        out.add("md")
    if not out:
        out.add("skills")
    return out


def _fwd(path: Path) -> str:
    return str(path).replace("\\", "/")


def analyze(
    project_dir: Path,
    user_dir: Path | None,
    plugins_dir: Path | None,
    scopes: set[str],
    explicit_paths: list[Path],
    ignore_dirs: list[str],
    ignore_files: list[str],
    verbose: bool = False,
    json_output: bool = False,
) -> int:
    # 0. ONE plugin-tier discovery (installed_plugins.json parsed exactly once,
    #    via skills_kit_lib.corpus); the pool, the skill-dir set, and the scan
    #    roots below all derive from it.
    plugin_entries = discover_plugin_entries(plugins_dir)

    # 1. Build the skill pool (always from SKILL.md files across all roots,
    #    regardless of scope, so refs resolve correctly).
    all_skills: list[SkillInfo] = []
    if project_dir.exists():
        all_skills.extend(discover_skills(project_dir, "project"))
    if user_dir and user_dir.exists():
        all_skills.extend(discover_skills(user_dir, "user"))
    all_skills.extend(plugin_skill_infos(plugin_entries))

    name_map = build_name_map(all_skills)
    all_names = set(name_map.keys())
    skill_dirs = collect_skill_dirs(project_dir, user_dir, plugin_entries)

    # 2. Build the source-file set (which files we scan for refs). Explicit
    #    --path entries override the scope walk.
    if explicit_paths:
        sources = scan_explicit_paths(
            explicit_paths, skill_dirs, ignore_dirs, ignore_files
        )
    else:
        base_dirs: list[tuple[Path, str]] = []
        if project_dir.exists():
            for root in find_project_skill_roots(project_dir):
                base_dirs.append((root, "project"))
        if user_dir and user_dir.exists():
            for root in find_project_skill_roots(user_dir):
                base_dirs.append((root, "user"))
        for entry in plugin_entries:
            base_dirs.append(
                (entry.install_path / "skills", f"plugin:{plugin_id_of(entry)}")
            )
        sources = discover_source_files(
            base_dirs, scopes, skill_dirs, ignore_dirs, ignore_files
        )

    # 3. Find issues. Findings are tracked as structured records (for JSON
    #    output and downstream classification) and as rendered strings (for
    #    the human-readable markdown report).
    findings: list[dict] = []

    def add_finding(
        severity: str,
        category_hint: str,
        message: str,
        *,
        file: str | None = None,
        line: int | None = None,
        ref: str | None = None,
        owner: str | None = None,
        source: str | None = None,
        kind: str | None = None,
    ) -> None:
        findings.append({
            "severity": severity,
            "category_hint": category_hint,
            "message": message,
            "file": file,
            "line": line,
            "ref": ref,
            "owner": owner,
            "source": source,
            "kind": kind,
        })

    for src in sources:
        owner = src.skill_name or _fwd(src.path)
        file_str = _fwd(src.path)
        for dep, line in src.hard_deps:
            if dep in all_names or dep in src.allow_stale:
                continue
            add_finding(
                "ERROR", "hard-dep",
                f"ERROR: {owner} ({src.source}, {src.kind}) at "
                f"{file_str}:{line} has hard dep `skill: \"{dep}\"` "
                f"but \"{dep}\" does not exist",
                file=file_str, line=line, ref=dep,
                owner=owner, source=src.source, kind=src.kind,
            )
        for ref, line in src.soft_refs:
            bare = ref.split(":", 1)[-1] if ":" in ref else ref
            if ref in all_names or bare in src.allow_stale or ref in src.allow_stale:
                continue
            add_finding(
                "WARNING", "soft-ref",
                f"WARNING: {owner} ({src.source}, {src.kind}) at "
                f"{file_str}:{line} references `/{ref}` but "
                f"\"{ref}\" does not exist",
                file=file_str, line=line, ref=ref,
                owner=owner, source=src.source, kind=src.kind,
            )

    # Shadowed skills (user overrides project) -- skill-pool issue, scope-independent
    for name, entries in name_map.items():
        sources_set = {e.source for e in entries}
        if "project" in sources_set and "user" in sources_set:
            add_finding(
                "INFO", "shadowed",
                f"INFO: {name} is shadowed (user skill overrides project skill)",
                ref=name,
            )

    # Name/directory mismatch on the skill identities (project/user only;
    # plugin install paths are version-pinned cache dirs).
    project_roots = find_project_skill_roots(project_dir) if project_dir else []
    user_roots = find_project_skill_roots(user_dir) if user_dir else []
    for s in all_skills:
        if s.source.startswith("plugin:"):
            continue
        candidate_roots = project_roots if s.source == "project" else user_roots
        expected = None
        for base in candidate_roots:
            try:
                s.path.resolve().relative_to(base)
            except ValueError:
                continue
            expected = expected_name_from_path(s.path, base)
            if expected is not None:
                break
        if expected is not None and expected != s.name:
            add_finding(
                "WARNING", "name-mismatch",
                f"WARNING: {s.name} ({s.source}) -- directory "
                f'suggests "{expected}" but frontmatter says "{s.name}"',
                file=_fwd(s.path), owner=s.name, source=s.source,
                ref=expected, kind="skill",
            )

    errors = [f for f in findings if f["severity"] == "ERROR"]
    warnings = [f for f in findings if f["severity"] == "WARNING"]
    infos = [f for f in findings if f["severity"] == "INFO"]

    project_skills = [s for s in all_skills if s.source == "project"]
    user_skills = [s for s in all_skills if s.source == "user"]
    plugin_skills = [s for s in all_skills if s.source.startswith("plugin:")]

    by_kind: dict[str, int] = {}
    for src in sources:
        by_kind[src.kind] = by_kind.get(src.kind, 0) + 1

    hard_dep_edges = [
        (src.skill_name or _fwd(src.path), dep, line)
        for src in sources
        for dep, line in src.hard_deps
    ]

    scope_label = "explicit paths" if explicit_paths else ",".join(sorted(scopes))

    # 4. Output -- JSON if requested, otherwise markdown.
    if json_output:
        payload = {
            "scope": scope_label,
            "source_files_scanned": len(sources),
            "source_files_by_kind": by_kind,
            "skill_pool": {
                "project": len(project_skills),
                "user": len(user_skills),
                "plugin": len(plugin_skills),
            },
            "findings": findings,
            "hard_dep_edges": [
                {"owner": s, "dep": d, "line": line}
                for s, d, line in sorted(hard_dep_edges)
            ],
            "summary": {
                "errors": len(errors),
                "warnings": len(warnings),
                "info": len(infos),
                "hard_dep_edges": len(hard_dep_edges),
            },
        }
        print(json.dumps(payload, indent=2))
        return 1 if errors else 0

    print("# Reference Audit Report\n")
    print(f"**Scope:** {scope_label}\n")
    if by_kind:
        kinds_str = ", ".join(f"{n} {k}" for k, n in sorted(by_kind.items()))
        print(f"**Source files scanned:** {len(sources)} ({kinds_str})\n")
    else:
        print(f"**Source files scanned:** {len(sources)}\n")
    print(
        f"**Skill pool:** {len(project_skills)} project, "
        f"{len(user_skills)} user, {len(plugin_skills)} plugin\n"
    )

    print("## Issues\n")
    if findings:
        for f in findings:
            print(f"- {f['message']}")
    else:
        print("No issues found.")
    print()

    if hard_dep_edges:
        print("## Hard Dependencies\n")
        for s, d, line in sorted(hard_dep_edges):
            print(f"- {s} -> {d} (line {line})")
        print()

    if verbose:
        print("## Soft References\n")
        for src in sorted(sources, key=lambda s: _fwd(s.path)):
            if src.soft_refs:
                owner = src.skill_name or _fwd(src.path)
                refs_str = ", ".join(
                    f"/{r}@{line}" for r, line in src.soft_refs
                )
                print(f"- {owner} ({src.kind}): {refs_str}")
        print()

        print("## All Source Files\n")
        print("| Owner / Path | Kind | Source |")
        print("|---|---|---|")
        for src in sorted(sources, key=lambda s: (s.source, _fwd(s.path))):
            owner = src.skill_name or _fwd(src.path)
            print(f"| {owner} | {src.kind} | {src.source} |")
        print()

    print("## Summary\n")
    print(f"- Errors: {len(errors)}")
    print(f"- Warnings: {len(warnings)}")
    print(f"- Info: {len(infos)}")
    print(f"- Hard dependency edges: {len(hard_dep_edges)}")

    return 1 if errors else 0


def main():
    parser = argparse.ArgumentParser(
        description="Audit Claude Code skill cross-references across SKILL.md, "
                    "skill-attached reference files, and/or arbitrary .md files."
    )
    parser.add_argument(
        "--project-dir",
        type=Path,
        default=Path(".claude/skills"),
        help="Project skills directory (default: .claude/skills).",
    )
    parser.add_argument(
        "--user-dir",
        type=Path,
        default=Path.home() / ".claude" / "skills",
        help="User skills directory (default: ~/.claude/skills).",
    )
    parser.add_argument(
        "--plugins-dir",
        type=Path,
        default=Path.home() / ".claude" / "plugins",
        help="Plugins directory containing installed_plugins.json "
             "(default: ~/.claude/plugins).",
    )
    parser.add_argument(
        "--scope",
        type=str,
        default="skills",
        help="Comma-separated scan scopes: skills (default, SKILL.md only), "
             "references (skill-attached .md that aren't SKILL.md), md (every "
             "*.md under the scan roots), all (alias for md). Example: "
             "--scope skills,references",
    )
    parser.add_argument(
        "--path",
        type=Path,
        action="append",
        default=[],
        help="Scan only this specific file. May be repeated. The skill pool "
             "is still loaded from the scan roots so refs resolve.",
    )
    parser.add_argument(
        "--ignore-dir",
        type=str,
        action="append",
        default=[],
        help="Skip any file whose path contains a matching directory. Glob "
             "syntax (fnmatch); matched against the full forward-slashed "
             "path and the basename. Repeatable. Example: --ignore-dir "
             "'*/ClaudeFeedback/*' or --ignore-dir node_modules.",
    )
    parser.add_argument(
        "--ignore-file",
        type=str,
        action="append",
        default=[],
        help="Skip files matching this glob. Matched against the full "
             "forward-slashed path and the basename. Repeatable.",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show full soft-reference graph and per-source-file listing.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a structured JSON report instead of markdown. Each "
             "finding carries file, line, ref, severity, and category_hint "
             "(hard-dep / soft-ref / name-mismatch / shadowed).",
    )
    args = parser.parse_args()
    try:
        scopes = parse_scope_arg(args.scope)
    except ValueError as exc:
        parser.error(str(exc))
        return 2  # unreachable; parser.error sys.exits
    sys.exit(analyze(
        args.project_dir,
        args.user_dir,
        args.plugins_dir,
        scopes,
        args.path,
        args.ignore_dir,
        args.ignore_file,
        args.verbose,
        args.json,
    ))


if __name__ == "__main__":
    main()
