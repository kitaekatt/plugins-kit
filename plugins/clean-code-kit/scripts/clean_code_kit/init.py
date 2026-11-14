"""``init``: propose a config.yaml from the extension counts of ``git ls-files``. Writes nothing."""

from __future__ import annotations

from collections import Counter
from pathlib import Path, PurePosixPath

import yaml

from . import config, coverage, snapshot
from .errors import Refusal
from .languages import EXTENSIONS, supported_extensions

TEST_DIRS = ("test", "tests", "spec", "specs", "__tests__")
SKIP_DIRS = {"node_modules", "vendor", "build", "dist", "out", "target", "venv", ".venv"}


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
    source_dirs: Counter[str] = Counter()
    at_root = 0
    for path in tracked:
        if coverage.extension(path) not in supported:
            continue
        parts = PurePosixPath(path).parts
        if len(parts) == 1:
            at_root += 1
        elif parts[0] not in TEST_DIRS and parts[0] not in SKIP_DIRS and not parts[0].startswith("."):
            source_dirs[parts[0]] += 1
    if not source_dirs:
        raise Refusal(f"{repo}: no source file sits below a top-level directory; "
                      "write source_roots by hand (references/configuring.md)")
    test_roots = sorted({PurePosixPath(p).parts[0] for p in tracked
                         if len(PurePosixPath(p).parts) > 1 and PurePosixPath(p).parts[0] in TEST_DIRS})
    unsupported = [(ext, n) for ext, n in counts.most_common() if ext not in supported]
    lines = [f"# Proposed by `code_audit.py init` from `git ls-files` in {repo}.",
             f"# Review before saving it as {config.CONFIG_REL.as_posix()}; init writes nothing."]
    for lang in languages:
        n = sum(counts[e] for e in EXTENSIONS[lang])
        lines.append(f"# {lang}: {n} tracked file{'s' if n != 1 else ''}")
    for root, n in sorted(source_dirs.items()):
        lines.append(f"# source root {root}: {n} source file{'s' if n != 1 else ''}")
    if at_root:
        lines.append(f"# {at_root} source file{'s' if at_root != 1 else ''} at the repository root "
                     "lie outside every proposed source root and would not be measured")
    for ext, n in unsupported[:10]:
        lines.append(f"# {ext}: {n} file{'s' if n != 1 else ''}, no lane reads this extension")
    lines += [
        "schema: 1", "snapshot: head",
        f"languages: [{', '.join(languages)}]",
        f"source_roots: [{', '.join(sorted(source_dirs))}]",
        f"test_roots: [{', '.join(test_roots)}]",
        "exclude: []",
        "lanes:",
        "  duplication: {enabled: true, threshold: 0.82, min_lines: 4, group_identical: true}",
        "  complexity:  {enabled: true, coverage: false, top: 50}",
    ]
    text = "\n".join(lines) + "\n"
    config.parse(config.config_path(repo), yaml.safe_load(text))  # the proposal must itself load
    return text
