#!/usr/bin/env python3
"""Find every use of the three retiring LLM plugins under DEVROOT.

Deterministic and stdlib-only: no inference, no network. Every direct,
non-hidden child directory of DEVROOT is one project. For each project the
script lists the files (git: tracked plus untracked-not-ignored; otherwise a
tree walk), reads each text file, and records every line that matches one of
the patterns in :func:`build_patterns`. Each hit carries a category derived
mechanically from the pattern and the file type (see :func:`classify`).

The three plugins are spelled as fragments in this file so that the audit
does not match itself when it scans plugins-kit.

Projects named ``plugins-kit*`` are the marketplace checkouts. Their own legacy
trees, the legacy test trees and the llm-kit plugin and tests are
self-references (:data:`SELF_PREFIXES`); they are counted per category under
``self`` and left out of the hit listing.

Findings are data: a completed scan exits 0. An unreadable file, a failing git
command or a missing root raises or exits non-zero.

Usage:
    uv run python scripts/retired_plugin_audit.py                  # text report
    uv run python scripts/retired_plugin_audit.py --format json
    uv run python scripts/retired_plugin_audit.py --root D:/other  # repeatable
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

# ==============================================================================
# Configuration
# ==============================================================================

MARKETPLACE = "plugins-" + "kit"

#: (CLI/plugin slug, importable module name) for each retiring plugin. The
#: module names are the package directories under each plugin's ``lib/``.
PLUGINS: tuple[tuple[str, str], ...] = (
    ("llm-scripting-" + "kit", "llm_scripting_" + "kit"),
    ("content-pipeline-" + "kit", "content_" + "pipeline"),
    ("job-" + "kit", "job_" + "kit"),
)

CATEGORIES: tuple[str, ...] = (
    "import",
    "enabled-plugin",
    "install-name",
    "cli-or-venv-path",
    "shared-lib-declaration",
    "mention",
)

SKIP_DIRS = frozenset({".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build"})
MAX_FILE_BYTES = 5 * 1024 * 1024
BINARY_PROBE_BYTES = 8 * 1024
TRIM_CHARS = 160
WORKERS = 16

#: Settings files read in every project even when git ignores them.
SETTINGS_FILES = (".claude/settings.json", ".claude/settings.local.json")

#: Marketplace checkouts are projects whose name starts with this.
SELF_PROJECT_PREFIX = MARKETPLACE

#: Paths inside a marketplace checkout that belong to the retiring plugins or
#: to their replacement; hits there are self-references.
SELF_PREFIXES: tuple[str, ...] = (
    *(f"plugins/{slug}/" for slug, _ in PLUGINS),
    *(f"tests/{slug}/" for slug, _ in PLUGINS),
    "tests/llm-kit/",
    "plugins/llm-kit/",
)

PYTHON_SUFFIXES = frozenset({".py", ".pyi", ".pyw", ".ipynb"})
CONFIG_SUFFIXES = frozenset(
    {".json", ".jsonc", ".yaml", ".yml", ".toml", ".cfg", ".ini"} | PYTHON_SUFFIXES
)
SHELL_SUFFIXES = frozenset({".sh", ".bash", ".zsh", ".cmd", ".bat", ".ps1"})

#: Cheap byte-level filter; every pattern below contains one of these tokens.
_PREFILTER = re.compile(
    b"|".join(
        re.escape(token.encode("ascii"))
        for slug, module in PLUGINS
        for token in (slug, module)
    )
)

# ==============================================================================
# Patterns
# ==============================================================================


@dataclass(frozen=True)
class Pattern:
    """One regex, tagged with its kind and the plugin it looks for."""

    pid: str
    kind: str
    plugin: str
    regex: re.Pattern[str]


def build_patterns() -> tuple[Pattern, ...]:
    """Return the match patterns, one regex each, in a fixed order."""
    patterns: list[Pattern] = []
    sep = r"[/\\]+"
    for slug, module in PLUGINS:
        e_slug = re.escape(slug)
        e_mod = re.escape(module)
        specs = (
            ("import", rf"\b(?:import|from)\s+{e_mod}\b"),
            ("install-at", re.escape(slug + "@")),
            ("install-ref", re.escape(f"{MARKETPLACE}:{slug}")),
            ("path", rf"{re.escape(MARKETPLACE)}{sep}{e_slug}[/\\]"),
            ("shared-lib", rf"""["']{e_mod}(?:\.[\w.]+)?["']"""),
            ("bare", rf"\b{e_slug}\b"),
        )
        for kind, source in specs:
            patterns.append(Pattern(f"{kind}:{slug}", kind, slug, re.compile(source)))
    return tuple(patterns)


PATTERNS = build_patterns()


def classify(kind: str, relpath: str) -> str:
    """Map a pattern kind and a file path to one category, mechanically."""
    path = Path(relpath)
    suffix = path.suffix.lower()
    if kind == "import":
        return "import" if suffix in PYTHON_SUFFIXES else "mention"
    if kind == "install-at":
        if path.name.lower().startswith("settings") and suffix == ".json":
            return "enabled-plugin"
        return "install-name"
    if kind == "install-ref":
        return "install-name"
    if kind == "path":
        return "cli-or-venv-path"
    if kind == "shared-lib":
        return "shared-lib-declaration" if suffix in CONFIG_SUFFIXES else "mention"
    if kind == "bare":
        if suffix in SHELL_SUFFIXES:
            return "cli-or-venv-path"
        if suffix == "" and "bin" in path.parts[:-1]:
            return "cli-or-venv-path"
        return "mention"
    raise ValueError(f"unknown pattern kind: {kind}")


# ==============================================================================
# Data model
# ==============================================================================


@dataclass
class Hit:
    category: str
    path: str
    line: int
    text: str
    patterns: list[str]


@dataclass
class ProjectReport:
    name: str
    path: str
    git: bool
    files_scanned: int = 0
    skipped_large: int = 0
    skipped_binary: int = 0
    skipped_not_file: int = 0
    hits: list[Hit] = field(default_factory=list)
    self_by_category: Counter[str] = field(default_factory=Counter)

    def by_category(self) -> dict[str, int]:
        counts = Counter(hit.category for hit in self.hits)
        return {cat: counts.get(cat, 0) for cat in CATEGORIES}

    def to_json(self) -> dict[str, object]:
        return {
            "name": self.name,
            "path": self.path,
            "git": self.git,
            "files_scanned": self.files_scanned,
            "skipped_large": self.skipped_large,
            "skipped_binary": self.skipped_binary,
            "skipped_not_file": self.skipped_not_file,
            "hit_count": len(self.hits),
            "by_category": self.by_category(),
            "self": {
                "total": sum(self.self_by_category.values()),
                "by_category": {c: self.self_by_category.get(c, 0) for c in CATEGORIES},
            },
            "hits": [
                {
                    "category": h.category,
                    "path": h.path,
                    "line": h.line,
                    "text": h.text,
                    "patterns": h.patterns,
                }
                for h in self.hits
            ],
        }


# ==============================================================================
# File discovery
# ==============================================================================


def _skipped_component(relpath: str) -> bool:
    return any(part in SKIP_DIRS for part in relpath.replace("\\", "/").split("/"))


def walk_files(base: Path, prefix: str = "") -> list[str]:
    """Walk ``base`` without following symlinks or junctions; return relpaths."""
    found: list[str] = []
    stack = [(base, prefix)]
    while stack:
        directory, rel = stack.pop()
        with os.scandir(directory) as entries:
            for entry in sorted(entries, key=lambda e: e.name):
                if entry.name in SKIP_DIRS:
                    continue
                child_rel = f"{rel}{entry.name}"
                st = entry.stat(follow_symlinks=False)
                if getattr(st, "st_file_attributes", 0) & _REPARSE_POINT:
                    continue  # links, junctions and other reparse points
                if stat.S_ISDIR(st.st_mode):
                    stack.append((Path(entry.path), child_rel + "/"))
                elif stat.S_ISREG(st.st_mode):
                    found.append(child_rel)
    return found


def git_files(project: Path) -> list[str]:
    """Tracked plus untracked-not-ignored paths; nested repos are walked."""
    result = subprocess.run(
        ["git", "-C", str(project), "ls-files", "-co", "--exclude-standard", "-z"],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git ls-files failed in {project} (exit {result.returncode}): "
            f"{result.stderr.decode('utf-8', 'replace').strip()}"
        )
    files: list[str] = []
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        rel = raw.decode("utf-8", "surrogateescape")
        if rel.endswith("/"):
            # A nested repository or submodule: ls-files lists only the directory.
            nested = project / rel.rstrip("/")
            if nested.is_dir() and not nested.is_symlink() and not nested.is_junction():
                files.extend(walk_files(nested, rel))
            continue
        files.append(rel)
    return files


def project_files(project: Path) -> tuple[bool, list[str]]:
    is_git = (project / ".git").exists()
    names = git_files(project) if is_git else walk_files(project)
    seen = {n for n in names if not _skipped_component(n)}
    for settings in SETTINGS_FILES:
        if settings not in seen and (project / settings).is_file():
            seen.add(settings)
    return is_git, sorted(seen)


# ==============================================================================
# Scanning
# ==============================================================================


def _excerpt(line: str, start: int) -> str:
    """The stripped line, trimmed to TRIM_CHARS, windowed on the first match."""
    stripped = line.strip()
    start = max(0, start - (len(line) - len(line.lstrip())))
    if len(stripped) <= TRIM_CHARS:
        return stripped
    begin = max(0, start - 60)
    return stripped[begin : begin + TRIM_CHARS]


def scan_text(relpath: str, text: str) -> list[Hit]:
    """Return one hit per (line, category) for a decoded file."""
    hits: list[Hit] = []
    for number, raw in enumerate(text.split("\n"), start=1):
        line = raw.rstrip("\r")
        if not line.strip():
            continue
        per_category: dict[str, list[str]] = {}
        first_start: dict[str, int] = {}
        bare: list[tuple[str, int]] = []
        for pattern in PATTERNS:
            match = pattern.regex.search(line)
            if match is None:
                continue
            if pattern.kind == "bare":
                bare.append((pattern.pid, match.start()))
                continue
            category = classify(pattern.kind, relpath)
            per_category.setdefault(category, []).append(pattern.pid)
            first_start.setdefault(category, match.start())
        if bare and not per_category:
            # A bare name is the fallback only: a more specific match owns the line.
            for pid, start in bare:
                kind_category = classify("bare", relpath)
                per_category.setdefault(kind_category, []).append(pid)
                first_start.setdefault(kind_category, start)
        for category, pids in per_category.items():
            hits.append(Hit(category, relpath, number, _excerpt(line, first_start[category]), pids))
    return hits


def is_self(project_name: str, relpath: str) -> bool:
    return project_name.startswith(SELF_PROJECT_PREFIX) and relpath.startswith(SELF_PREFIXES)


_REPARSE_POINT = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT


def is_regular_file(path: Path) -> bool:
    """True for a plain file; links and other reparse points are never followed."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    if getattr(st, "st_file_attributes", 0) & _REPARSE_POINT:
        return False
    return stat.S_ISREG(st.st_mode)


def read_candidate(full: Path, follow: bool) -> tuple[str, bytes]:
    """Return ("ok", data), or (reason, b"") for a skipped file.

    Reasons: "not_file", "large", "binary". Only the first 8 KB is read before
    the binary check, so large binaries cost one small read.
    """
    if follow:
        if not full.is_file():
            return "not_file", b""
    elif not is_regular_file(full):
        return "not_file", b""
    if full.stat().st_size > MAX_FILE_BYTES:
        return "large", b""
    with full.open("rb") as handle:
        head = handle.read(BINARY_PROBE_BYTES)
        if b"\0" in head:
            return "binary", b""
        return "ok", head + handle.read()


def scan_project(project: Path) -> ProjectReport:
    is_git, relpaths = project_files(project)
    report = ProjectReport(name=project.name, path=str(project), git=is_git)

    def work(rel: str) -> tuple[str, list[Hit]]:
        # Settings files are followed even when they are links (a user-level
        # settings file is often one); every other link is skipped.
        status, data = read_candidate(project / rel, follow=rel in SETTINGS_FILES)
        if status != "ok":
            return status, []
        if _PREFILTER.search(data) is None:
            return "ok", []
        return "ok", scan_text(rel, data.decode("utf-8", errors="replace"))

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(work, relpaths))  # keeps relpaths order
    for rel, (status, hits) in zip(relpaths, results):
        if status == "not_file":
            report.skipped_not_file += 1
        elif status == "large":
            report.skipped_large += 1
        elif status == "binary":
            report.skipped_binary += 1
        else:
            report.files_scanned += 1
        for hit in hits:
            if is_self(project.name, rel):
                report.self_by_category[hit.category] += 1
            else:
                report.hits.append(hit)
    return report


def list_projects(root: Path) -> list[Path]:
    if not root.is_dir():
        raise SystemExit(f"error: root is not a directory: {root}")
    return sorted(
        (p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")),
        key=lambda p: p.name.lower(),
    )


def audit(roots: list[Path]) -> dict[str, object]:
    projects = [scan_project(p) for root in roots for p in list_projects(root)]
    totals: Counter[str] = Counter()
    for project in projects:
        totals.update(h.category for h in project.hits)
    return {
        "roots": [str(r) for r in roots],
        "categories": list(CATEGORIES),
        "patterns": [p.pid for p in PATTERNS],
        "projects": [p.to_json() for p in projects],
        "totals": {
            "projects": len(projects),
            "projects_with_hits": sum(1 for p in projects if p.hits),
            "hits": sum(totals.values()),
            "by_category": {c: totals.get(c, 0) for c in CATEGORIES},
            "files_scanned": sum(p.files_scanned for p in projects),
            "skipped_large": sum(p.skipped_large for p in projects),
            "skipped_binary": sum(p.skipped_binary for p in projects),
            "skipped_not_file": sum(p.skipped_not_file for p in projects),
        },
    }


# ==============================================================================
# Output
# ==============================================================================


def format_text(result: dict[str, object]) -> str:
    out: list[str] = []
    projects = result["projects"]
    assert isinstance(projects, list)
    for project in projects:
        self_info = project["self"]
        if not project["hits"] and not self_info["total"]:
            continue
        out.append(
            f"== {project['name']} ({'git' if project['git'] else 'no git'}; "
            f"{project['files_scanned']} files scanned, {project['skipped_large']} oversize, "
            f"{project['skipped_binary']} binary skipped) =="
        )
        if self_info["total"]:
            detail = ", ".join(f"{c} {n}" for c, n in self_info["by_category"].items() if n)
            out.append(f"  self: {self_info['total']} ({detail}) [not listed]")
        for category in CATEGORIES:
            rows = [h for h in project["hits"] if h["category"] == category]
            if not rows:
                continue
            out.append(f"  [{category}] {len(rows)}")
            for hit in rows:
                out.append(f"    {hit['path']}:{hit['line']}: {hit['text']}")
        out.append("")
    out.append("== Summary ==")
    header = ["project", *CATEGORIES, "hits", "self", "scanned", "oversize", "binary"]
    table = [header]
    for project in projects:
        counts = project["by_category"]
        table.append(
            [
                project["name"],
                *(str(counts[c]) for c in CATEGORIES),
                str(project["hit_count"]),
                str(project["self"]["total"]),
                str(project["files_scanned"]),
                str(project["skipped_large"]),
                str(project["skipped_binary"]),
            ]
        )
    totals = result["totals"]
    table.append(
        [
            "TOTAL",
            *(str(totals["by_category"][c]) for c in CATEGORIES),
            str(totals["hits"]),
            str(sum(p["self"]["total"] for p in projects)),
            str(totals["files_scanned"]),
            str(totals["skipped_large"]),
            str(totals["skipped_binary"]),
        ]
    )
    widths = [max(len(row[i]) for row in table) for i in range(len(header))]
    for row in table:
        out.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
    return "\n".join(out) + "\n"


# ==============================================================================
# CLI
# ==============================================================================


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Find uses of the retiring LLM plugins in every project under DEVROOT."
    )
    parser.add_argument(
        "--root",
        action="append",
        default=[],
        metavar="DIR",
        help="Directory whose children are projects; repeatable; replaces $DEVROOT.",
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)

    if args.root:
        roots = [Path(r).expanduser() for r in args.root]
    else:
        devroot = os.environ.get("DEVROOT")
        if not devroot:
            parser.error("DEVROOT is not set; set it or pass --root DIR")
        roots = [Path(devroot)]

    # Scanned lines may hold non-ASCII text; never let a console codec fail the report.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    result = audit(roots)
    if args.format == "json":
        sys.stdout.write(json.dumps(result, indent=2) + "\n")
    else:
        sys.stdout.write(format_text(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
