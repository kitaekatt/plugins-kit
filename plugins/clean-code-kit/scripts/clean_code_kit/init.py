"""``init``: propose a config.yaml from the extension counts of ``git ls-files``. Writes nothing."""

from __future__ import annotations

import posixpath
from collections import Counter
from pathlib import Path, PurePosixPath

import yaml

from . import config, coverage, snapshot
from .errors import Refusal
from .languages import EXTENSIONS, KIND_OTHER, KIND_SOURCE_UNMEASURED, kind_of, supported_extensions

TEST_DIRS = ("test", "tests", "spec", "specs", "__tests__")
SKIP_DIRS = {"node_modules", "vendor", "build", "dist", "out", "target", "venv", ".venv"}
# A top-level directory holding fewer supported files than this is not proposed as a root itself.
MIN_ROOT_FILES = 3


def _source_roots(tracked: list[str], supported: set[str]) -> tuple[dict[str, int], list[str]]:
    """``({root: supported file count}, files left out)``. Hidden directories count when git tracks files in them."""
    by_top: dict[str, list[str]] = {}
    for path in tracked:
        parts = PurePosixPath(path).parts
        if coverage.extension(path) in supported and len(parts) > 1 \
                and parts[0] not in TEST_DIRS and parts[0] not in SKIP_DIRS:
            by_top.setdefault(parts[0], []).append(path)
    roots: dict[str, int] = {}
    stray: list[str] = []
    for top, paths in by_top.items():
        if len(paths) >= MIN_ROOT_FILES:
            roots[top] = len(paths)
        elif len(paths) == 1:
            stray.append(paths[0])
        else:
            roots[posixpath.commonpath([PurePosixPath(p).parent.as_posix() for p in paths])] = len(paths)
    return roots, sorted(stray)


def propose(target: Path) -> str:
    repo = snapshot.repo_root(target)
    tracked = snapshot.git(repo, "ls-files", "-z").split("\0")
    tracked = [p for p in tracked if p]
    counts = Counter(coverage.extension(p) for p in tracked)
    supported = supported_extensions()
    languages = [lang for lang, exts in EXTENSIONS.items() if any(counts[e] for e in exts)]
    if not languages:
        raise Refusal(f"{repo}: no tracked file has an extension a lane reads "
                      f"({', '.join(sorted(supported))}); nothing to propose")
    source_roots, stray = _source_roots(tracked, supported)
    at_root = sum(1 for p in tracked if len(PurePosixPath(p).parts) == 1 and coverage.extension(p) in supported)
    if not source_roots:
        raise Refusal(f"{repo}: no directory holds {MIN_ROOT_FILES} or more source files, and no "
                      "other directory can be proposed; write source_roots by hand "
                      "(references/configuring.md)")
    test_roots = sorted({PurePosixPath(p).parts[0] for p in tracked
                         if len(PurePosixPath(p).parts) > 1 and PurePosixPath(p).parts[0] in TEST_DIRS})
    unread_source = [(ext, n) for ext, n in counts.most_common() if kind_of(ext) == KIND_SOURCE_UNMEASURED]
    other = sum(n for ext, n in counts.items() if kind_of(ext) == KIND_OTHER)
    lines = [f"# Proposed by `code_audit.py init` from `git ls-files` in {repo}.",
             f"# Review before saving it as {config.CONFIG_REL.as_posix()}; init writes nothing."]
    for lang in languages:
        n = sum(counts[e] for e in EXTENSIONS[lang])
        lines.append(f"# {lang}: {n} tracked file{'s' if n != 1 else ''}")
    for root, n in sorted(source_roots.items()):
        lines.append(f"# source root {root}: {n} source file{'s' if n != 1 else ''}")
    if at_root:
        lines.append(f"# {at_root} source file{'s' if at_root != 1 else ''} at the repository root "
                     "lie outside every proposed source root and would not be measured")
    for path in stray:
        lines.append(f"# {path}: the only source file under its top-level directory; not proposed as a root")
    for ext, n in unread_source:
        lines.append(f"# {ext}: {n} file{'s' if n != 1 else ''}, no lane reads this extension")
    lines.append(f"# other files: {other}")
    lines += [
        "schema: 1", "snapshot: head",
        f"languages: [{', '.join(languages)}]",
        f"source_roots: [{', '.join(sorted(source_roots))}]",
        f"test_roots: [{', '.join(test_roots)}]",
        "exclude: []",
        "lanes:",
        "  duplication: {enabled: true, threshold: 0.82, min_lines: 4, group_identical: true}",
        "  complexity:  {enabled: true, coverage: false, top: 50}",
    ]
    text = "\n".join(lines) + "\n"
    config.parse(config.config_path(repo), yaml.safe_load(text))  # the proposal must itself load
    return text
