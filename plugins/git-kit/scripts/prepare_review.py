"""Gather review context for a git diff range.

Usage:
    prepare_review.py                   # auto-detect range from workspace state
    prepare_review.py <ref>             # review <ref>..HEAD
    prepare_review.py <a>..<b>          # review the given range
    prepare_review.py <a>...<b>         # review the symmetric difference (merge-base..b)
    prepare_review.py --staged          # review index vs HEAD
    prepare_review.py --working         # review working tree vs HEAD (uncommitted)

Any invocation also accepts `--claim <glob>` (repeatable). A changed file whose
repo-relative path matches a claim glob is held back from the generic reviewer
fan-out (its diff is excluded from the chunks and it is dropped from
`changed_files`) and surfaced under `claimed_files` instead, with its pre-image
materialized to `<bundle_dir>/pre-images/<name>`. Claimed files still contribute
to `unique_claude_mds` and the submit-gate scan. With no `--claim` the bundle is
byte-identical to today's (no `claimed_files` key).

A glob prefixed with `!` is an EXCLUSION and beats every positive pattern, so a
caller can claim a broad shape while carving out a subset that no specialist
actually reviews -- e.g. `--claim '**/*.md' --claim '!**/skills/*/references/*.md'`.
Claiming a file removes it from the generic reviewers on the promise that a
specialist reads it instead; where no specialist does, the exclusion is what keeps
the file reviewed at all.

Auto-detect resolution order:
    1. Mid-merge (MERGE_HEAD present)            -> review the in-progress merge
    2. Mid-rebase (rebase-merge/-apply present)  -> review the in-progress rebase
    3. HEAD has upstream                         -> @{upstream}..HEAD
    4. origin/main exists                        -> origin/main..HEAD
    5. origin/master exists                      -> origin/master..HEAD
    6. local main exists                         -> main..HEAD
    7. local master exists                       -> master..HEAD
    Else exit non-zero with a hint to pass an explicit range.

Outputs a JSON bundle on stdout AND persists `bundle.json` next to the
per-file chunk fragments at:
    ~/.claude/plugins/data/plugins-kit/git-kit/reviews/<safe-range-name>/

The diff is partitioned into chunks <=MAX_CHUNK_BYTES at file boundaries
(directory transitions preferred) so reviewer subagents can Read one
chunk per agent and fan out across a large diff. Chunking, CLAUDE.md
ancestor walk, and submit-gate parsing live in bootstrap_lib.code_review
and are shared with p4-kit's p4-code-review skill.

Output schema:
    {
      "vcs": "git",
      "range": "<the diff range we reviewed, e.g. origin/main..HEAD>",
      "head_sha": "<short sha of HEAD>",
      "branch": "<current branch name or 'DETACHED'>",
      "auto_detected_reason": "<human-readable reason chosen, omitted if explicit>",
      "description": "<one or more commit subjects joined by '; '>",
      "bundle_dir": "<absolute path to bundle directory>",
      "diff_chunks": [
        {"index": 0, "path": "chunks/chunk-000.diff",
         "files": ["<repo-relative path>", ...], "bytes": <int>}
      ],
      "changed_files": [
        {"path": "<repo-relative path>", "local": "<absolute path>",
         "status": "A"|"M"|"D"|"R"|"C"|"T",
         "chunk_index": <int or null>,
         "claude_mds": ["<absolute path>", ...]}
      ],
      "unique_claude_mds": ["<absolute path>", ...],
      "untracked_or_unstaged": [
        {"local": "<absolute path>", "path": "<repo-relative>",
         "kind": "untracked"|"unstaged_modified"|"unstaged_deleted"|"staged_uncommitted"}
      ],
      "merge_conflicts": [
        {"path": "<repo-relative>", "local": "<absolute path>"}
      ],
      "claimed_files": [                      # present only when --claim was passed
        {"identifier": "<repo-relative path>", "path": "<repo-relative path>",
         "local": "<absolute path>", "status": "A"|"M"|"D"|...,
         "pre_image": "<absolute path to materialized pre-image, or null for an add>",
         "trivial": <bool>,                    # pure-mechanical: typo-sized + no meaning-bearing change
         "trivial_reasons": ["<code>", ...],   # disqualifiers when trivial=false (e.g. too_large, structure_changed)
         "trivial_checks": {"ascii_clean": <bool>, "no_abs_paths": <bool>},  # only when trivial=true
         "claude_mds": ["<absolute path>", ...]}   # nearest-ancestor-first; includes self for a CLAUDE.md subject
      ],
      "submit_gates": [
        {"source": "<absolute path to CLAUDE.md>",
         "summary": "<one-line imperative>",
         "scope_paths": ["<prefix or glob>", ...],
         "matched_files": ["<local path>", ...],
         "rationale": "<optional prose, may be empty>",
         "line_no": <int>}
      ],
      "change_id": "<range spec>",               # ledger key: the change identity
      "ledger_baseline": "<range base SHA>",      # invalidates stale ledger entries
      "ledger_hits": [                            # findings previously declined for this range, still valid
        {"key": "<hash>", "kind": "md_audit"|"code_review", "file": "<path>",
         "verdict": "declined", "baseline": "<sha>", "timestamp": <float>,
         "label": "<short human label>", "severity": "<md_audit only>"}
      ]
    }

Also supports `prepare_review.py --ledger-record <declined.json>` -- record
findings the author declined into the ledger so they render collapsed (not
re-litigated) on the next review of the same range. Payload:
{change_id, baseline, declined:[...]}. See bootstrap_lib.code_review.ledger.

Stderr-only diagnostics. Non-zero exit on hard failure.
"""

import hashlib
import re
import sys
from pathlib import Path
from typing import Optional

# Plugins define their own bootstrap-provisioned venv and must run under it
# preferentially. A bare `python` or `uv run` invocation lands in a different
# environment with no shared-libs .pth, so re-exec under the provisioned venv
# before importing bootstrap_lib below -- a no-op when already there. The guard
# is the vendored, stdlib-only bootstrap_guard next to this script; importing it
# can never itself trip the missing-bootstrap_lib failure.
from bootstrap_guard import reexec_under_plugin_venv  # noqa: E402

reexec_under_plugin_venv("git-kit")

try:
    from bootstrap_lib.path_repair import repair_path  # noqa: E402

    # Shared VCS-neutral review pipeline -- subprocess wrapper, section
    # splitting, chunking + CLAUDE.md walk + submit-gate scan, bundle
    # emission. See bootstrap_lib/code_review/pipeline.py.
    from bootstrap_lib.code_review.pipeline import (  # noqa: E402
        assemble_bundle,
        emit_bundle,
        matches_claim,
        preimage_relpath,
        run_vcs,
        split_sections,
    )
    from bootstrap_lib.code_review import ledger  # noqa: E402
except ImportError:
    # bootstrap_lib is absent -> the bootstrap plugin never provisioned this
    # plugin's venv. Convert the raw ModuleNotFoundError traceback into an
    # actionable "install/enable plugins-kit:bootstrap" message and exit.
    from bootstrap_guard import require_bootstrap

    require_bootstrap(
        "git-kit", feature="code review", missing="bootstrap_lib", force=True
    )

repair_path()


# This module is the GIT VcsAdapter (bootstrap_lib.code_review.vcs_adapter) by
# shape, not by inheritance: get_repo_root -> workspace_root, detect_default_range
# / parse_range_arg -> resolve_target, fetch_diff, _parse_git_header ->
# parse_header, _git_diff_to_sections -> diff_to_sections, fetch_changed_files ->
# enumerate_changed_files, find_untracked_or_unstaged -> hygiene_unincluded,
# find_merge_conflicts -> hygiene_unresolved, materialize_preimage ->
# materialize_preimage. Git implements neither auto-shelve optional capability
# (snapshot_change/cleanup) -- its range is always diffable -- but DOES implement
# materialize_preimage (used by the claim mechanism). See the protocol docstring
# for the full contract before changing any of these shapes.


# Mirror p4-kit's choice for the same reason -- Read tool refuses files
# beyond some unpublished threshold (a 1.4 MB plain-text diff fails).
# 1 MB leaves ~40% headroom and keeps chunk counts close to 1 for typical
# diffs. Tune downward if a Read failure surfaces.
MAX_CHUNK_BYTES = 1024 * 1024

DEFAULT_BUNDLE_ROOT = (
    Path.home() / ".claude" / "plugins" / "data"
    / "plugins-kit" / "git-kit" / "reviews"
)

# Declined-findings ledger: a single JSON file in the plugin's version-independent
# data dir, a sibling of the per-range bundle dirs. Keyed per change-id (range).
# See bootstrap_lib.code_review.ledger.
LEDGER_PATH = DEFAULT_BUNDLE_ROOT / "ledger.json"

# git diff section header prefix: `diff --git a/<path> b/<path>`.
# Paths may contain spaces (emitted unquoted) or non-ASCII characters
# (C-quoted with octal escapes under core.quotepath's default). Header
# parsing therefore cannot be a simple \S+ regex -- see
# _parse_git_header_path for the quote-aware extraction.
_GIT_HEADER_PREFIX = "diff --git "

# Single-character C escapes git uses when quoting paths (core.quotepath).
_C_ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13}

# Status letters in `git diff --name-status` and `git status --porcelain`.
_STATUS_CHARS = set("AMDRCT")


def run_git(args: list[str], cwd: Optional[Path] = None) -> tuple[int, str, str]:
    """Run a git command, return (returncode, stdout, stderr).

    Thin wrapper over the shared run_vcs, which forces UTF-8 decoding --
    non-Latin-1 file content (CJK, emoji) in diffs would abort the
    subprocess reader on Windows under cp1252.
    """
    return run_vcs("git", args, cwd=cwd)


# ---------------------------------------------------------------------------
# Workspace state detection -- pick a sensible default range
# ---------------------------------------------------------------------------


def get_git_dir() -> Optional[Path]:
    """Resolve `.git` (or the worktree's gitdir) for the current cwd."""
    rc, out, _ = run_git(["rev-parse", "--git-dir"])
    if rc != 0:
        return None
    return Path(out.strip())


def get_repo_root() -> Optional[Path]:
    rc, out, _ = run_git(["rev-parse", "--show-toplevel"])
    if rc != 0:
        return None
    return Path(out.strip())


def get_current_branch() -> Optional[str]:
    """Return the current branch name, or None if HEAD is detached."""
    rc, out, _ = run_git(["symbolic-ref", "-q", "HEAD"])
    if rc != 0:
        return None
    return out.strip().replace("refs/heads/", "", 1) or None


def ref_exists(ref: str) -> bool:
    rc, _, _ = run_git(["rev-parse", "--verify", "--quiet", ref])
    return rc == 0


def detect_default_range() -> tuple[str, str]:
    """Pick a default diff range from workspace state.

    Returns (range_spec, reason). Raises ValueError if no sensible default
    exists (detached HEAD with no upstream and no main/master fallback).

    `range_spec` is a string that `git diff` accepts directly (e.g.
    `origin/main..HEAD`) OR one of the sentinel strings `__merge_in_progress__`,
    `__rebase_in_progress__`, `__working_tree__`, `__staged__` for non-range
    modes -- callers route those through different git commands.
    """
    git_dir = get_git_dir()
    if git_dir is None:
        raise ValueError("not inside a git repository (cwd: cannot resolve .git)")

    if (git_dir / "MERGE_HEAD").exists():
        return ("__merge_in_progress__", "MERGE_HEAD present -- reviewing the in-progress merge")
    if (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists():
        return ("__rebase_in_progress__", "rebase in progress -- reviewing the in-progress rebase")

    branch = get_current_branch()
    if branch is None:
        # Detached HEAD. Try a sensible fallback to main/master; fail if none.
        for fb in ["origin/main", "origin/master", "main", "master"]:
            if ref_exists(fb):
                return (f"{fb}..HEAD", f"detached HEAD; falling back to {fb}..HEAD")
        raise ValueError(
            "HEAD is detached and no main/master ref found. Pass an explicit range "
            "(e.g. `prepare_review.py <base-ref>`)."
        )

    # Try upstream first.
    rc, out, _ = run_git(["rev-parse", "--symbolic-full-name", "@{upstream}"])
    if rc == 0 and out.strip():
        upstream = out.strip()
        return (f"{upstream}..HEAD", f"@{{upstream}} = {upstream}")

    # Fallback chain. Skip a fallback if it IS the current branch (no-op
    # diff). Exact match only: on branch "main", "origin/main" must remain
    # a valid fallback (it is the unpushed-commits base, not a no-op).
    for fb in ["origin/main", "origin/master", "main", "master"]:
        if fb == branch:
            continue
        if ref_exists(fb):
            return (f"{fb}..HEAD", f"no @{{upstream}} set; falling back to {fb}..HEAD")

    raise ValueError(
        f"branch '{branch}' has no @{{upstream}} and no main/master ref exists. "
        f"Pass an explicit range (e.g. `prepare_review.py <base-ref>`)."
    )


# ---------------------------------------------------------------------------
# Diff fetch + parse
# ---------------------------------------------------------------------------


def fetch_diff(range_spec: str) -> str:
    """Return the raw `git diff` output for `range_spec`.

    Sentinels:
    - __working_tree__: `git diff` (worktree vs HEAD, including unstaged)
    - __staged__:       `git diff --cached`
    - __merge_in_progress__: `git diff HEAD` (merge result vs HEAD)
    - __rebase_in_progress__: `git diff HEAD` (current state vs HEAD)

    Otherwise: `git diff <range_spec>`.

    Merge mode deliberately uses a plain (non-combined) diff, NOT
    `git diff --cc HEAD`: combined output emits `diff --cc` headers the
    splitter doesn't parse, and once a conflict is resolved and staged
    `--cc` omits the resolution hunks entirely. `git diff HEAD` shows the
    full merge result (incoming files + conflict resolutions, with any
    unresolved conflict markers inline) in plain format, and matches the
    `--name-status HEAD` used by fetch_changed_files.
    """
    if range_spec == "__working_tree__":
        cmd = ["diff", "HEAD"]
    elif range_spec == "__staged__":
        cmd = ["diff", "--cached"]
    elif range_spec in ("__merge_in_progress__", "__rebase_in_progress__"):
        cmd = ["diff", "HEAD"]
    else:
        cmd = ["diff", range_spec]
    rc, out, err = run_git(cmd)
    if rc != 0:
        raise ValueError(f"git diff failed: {err.strip() or 'no output'}")
    return out


def fetch_changed_files(range_spec: str) -> list[tuple[str, str]]:
    """Return [(status, path), ...] for files changed in `range_spec`.

    `status` is a single letter (A/M/D/R/C/T). For renames the path is
    the post-rename b-side. Empty list if no changes.

    Uses `-z` (NUL-separated) output so paths come through raw -- without
    -z, git C-quotes non-ASCII paths (core.quotepath default), and the
    quoted escape string would never match a real file or diff section.
    """
    if range_spec == "__working_tree__":
        cmd = ["diff", "--name-status", "-z", "HEAD"]
    elif range_spec == "__staged__":
        cmd = ["diff", "--name-status", "-z", "--cached"]
    elif range_spec in ("__merge_in_progress__", "__rebase_in_progress__"):
        cmd = ["diff", "--name-status", "-z", "HEAD"]
    else:
        cmd = ["diff", "--name-status", "-z", range_spec]
    rc, out, _ = run_git(cmd)
    if rc != 0:
        return []
    files: list[tuple[str, str]] = []
    tokens = out.split("\0")
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if not token:
            i += 1
            continue
        status = token[:1]
        if status not in _STATUS_CHARS:
            i += 1
            continue
        # R / C carry a similarity score and two paths (old, new); other
        # statuses carry one path. Take the last path (the b-side).
        n_paths = 2 if status in "RC" else 1
        if i + n_paths >= len(tokens):
            break
        path = tokens[i + n_paths]
        if path:
            files.append((status, path))
        i += n_paths + 1
    return files


def fetch_description(range_spec: str) -> str:
    """Pick a human-readable description for the range.

    For a real <a>..<b> range: concatenate commit subjects (newest first),
    up to ~5; ellipsis if more. For working-tree / staged / merge / rebase
    modes: a fixed marker string.
    """
    if range_spec == "__working_tree__":
        return "(uncommitted working-tree changes)"
    if range_spec == "__staged__":
        return "(staged-but-uncommitted changes)"
    if range_spec == "__merge_in_progress__":
        return "(in-progress merge)"
    if range_spec == "__rebase_in_progress__":
        return "(in-progress rebase)"
    rc, out, _ = run_git(["log", "--no-merges", "--format=%s", "-n", "6", range_spec])
    if rc != 0:
        return ""
    subjects = [s.strip() for s in out.splitlines() if s.strip()]
    if not subjects:
        return ""
    if len(subjects) > 5:
        return "; ".join(subjects[:5]) + f"; (+{len(subjects) - 5} more)"
    return "; ".join(subjects)


def _unquote_c_path(token: str) -> str:
    """Undo git's C-style path quoting: '"r\\303\\251sum\\303\\251.txt"' -> 'résumé.txt'.

    Octal escapes encode raw bytes; the byte sequence is decoded as UTF-8.
    Tokens that are not quoted pass through unchanged.
    """
    if len(token) < 2 or not (token.startswith('"') and token.endswith('"')):
        return token
    body = token[1:-1]
    out = bytearray()
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            if nxt in ('"', "\\"):
                out.extend(nxt.encode("utf-8"))
                i += 2
            elif nxt in _C_ESCAPES:
                out.append(_C_ESCAPES[nxt])
                i += 2
            elif nxt.isdigit():
                out.append(int(body[i + 1 : i + 4], 8))
                i += 4
            else:
                out.extend(nxt.encode("utf-8"))
                i += 2
        else:
            out.extend(ch.encode("utf-8"))
            i += 1
    return out.decode("utf-8", errors="replace")


def _scan_quoted(s: str) -> int:
    """Index just past the closing quote of a leading C-quoted token."""
    i = 1
    while i < len(s):
        if s[i] == "\\":
            i += 2
        elif s[i] == '"':
            return i + 1
        else:
            i += 1
    return len(s)


def _parse_git_header_path(line: str) -> Optional[str]:
    """Extract the b-side (post-image) path from a `diff --git` header line.

    Handles the header shapes git actually emits:
    - plain:   diff --git a/foo.py b/foo.py
    - spaced:  diff --git a/has space.txt b/has space.txt    (unquoted!)
    - quoted:  diff --git "a/r\\303\\251sum\\303\\251.txt" "b/r\\303\\251sum\\303\\251.txt"
    (either side may be quoted independently)

    Returns None when the line is not a diff --git header.
    """
    if not line.startswith(_GIT_HEADER_PREFIX):
        return None
    rest = line[len(_GIT_HEADER_PREFIX):]
    if rest.startswith('"'):
        # Quoted a-side: skip it; the remainder is the b-side token
        # (quoted or not -- even unquoted-with-spaces it runs to EOL).
        b_token = rest[_scan_quoted(rest):].lstrip(" ")
        b_path = _unquote_c_path(b_token)
        return b_path[2:] if b_path.startswith("b/") else None
    if rest.endswith('"'):
        # Unquoted a-side, quoted b-side: the first quote starts the b
        # token (an a-path containing a literal quote would itself be
        # quoted by git, landing in the branch above).
        b_path = _unquote_c_path(rest[rest.index('"'):])
        return b_path[2:] if b_path.startswith("b/") else None
    # Both sides unquoted. For non-renames a == b, so try each ` b/`
    # split point and prefer one where the two sides match; renames (or
    # pathological paths containing ` b/`) fall back to the last split
    # point as best effort.
    candidates = [m.start() for m in re.finditer(" b/", rest)]
    if not candidates:
        return None
    for i in candidates:
        a_part, b_part = rest[:i], rest[i + 1:]
        if a_part.startswith("a/") and a_part[2:] == b_part[2:]:
            return b_part[2:]
    return rest[candidates[-1] + 1:][2:]


def _parse_git_header(line: str) -> Optional[dict]:
    """Header matcher for the shared splitter.

    Uses the b-side (post-image) path as the identifier; for renames the
    a-side and b-side differ and the b-side is the canonical one.
    """
    path = _parse_git_header_path(line)
    return None if path is None else {"path": path}


def split_git_diff_sections(diff_text: str) -> tuple[str, list[dict]]:
    """Split git-diff output into (preamble, [{path, header, body}, ...])."""
    return split_sections(diff_text, _parse_git_header)


def _git_diff_to_sections(diff_text: str) -> tuple[str, list[dict]]:
    """Adapter: git-format diff -> (preamble, [DiffSection])."""
    preamble, sections = split_git_diff_sections(diff_text)
    return preamble, [
        {"identifier": s["path"], "text": s["header"] + s["body"]}
        for s in sections
    ]


# ---------------------------------------------------------------------------
# Workspace hygiene checks (analogs of p4-kit's unreconciled/unresolved)
# ---------------------------------------------------------------------------


def find_untracked_or_unstaged(
    repo_root: Path, touched_dirs: list[Path]
) -> list[dict]:
    """`git status --porcelain` filtered to files inside touched_dirs.

    Each entry: {"local": <abs path>, "path": <repo-rel>, "kind": ...}.
    `kind` distinguishes:
      - "untracked"            -- ?? in status
      - "unstaged_modified"    -- worktree differs from index
      - "unstaged_deleted"     -- file removed from worktree but still tracked
      - "staged_uncommitted"   -- index differs from HEAD (different from the diff range we're reviewing)
    """
    rc, out, _ = run_git(["status", "--porcelain", "-uall"], cwd=repo_root)
    if rc != 0:
        return []
    touched_resolved = []
    for d in touched_dirs:
        try:
            touched_resolved.append(d.resolve())
        except OSError:
            continue

    items: list[dict] = []
    for raw in out.splitlines():
        if len(raw) < 3:
            continue
        index_st = raw[0]
        worktree_st = raw[1]
        path_part = raw[3:].strip()
        # Renames: "R  old -> new" -- use the new (post-rename) path
        if " -> " in path_part:
            path_part = path_part.split(" -> ", 1)[1]
        path_part = path_part.strip().strip('"')

        local = (repo_root / path_part).resolve()
        try:
            local_dir = local.parent
        except OSError:
            continue
        # Only surface files inside the directories the review touches.
        if not any(
            local_dir == td or td in local_dir.parents for td in touched_resolved
        ):
            continue

        if index_st == "?" and worktree_st == "?":
            kind = "untracked"
        elif worktree_st == "M":
            kind = "unstaged_modified"
        elif worktree_st == "D":
            kind = "unstaged_deleted"
        elif index_st in "AMDRCT":
            kind = "staged_uncommitted"
        else:
            continue
        items.append({"local": str(local), "path": path_part, "kind": kind})
    return items


def find_merge_conflicts(repo_root: Path) -> list[dict]:
    """`git ls-files -u` returns unmerged paths (one row per stage).

    We collapse to one entry per path. Empty list when no merge in progress.
    """
    rc, out, _ = run_git(["ls-files", "-u"], cwd=repo_root)
    if rc != 0 or not out.strip():
        return []
    seen: set[str] = set()
    items: list[dict] = []
    for line in out.splitlines():
        # Format: "100644 abcdef 1\tpath/to/file"
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        path = parts[1].strip()
        if path in seen:
            continue
        seen.add(path)
        items.append({"path": path, "local": str((repo_root / path).resolve())})
    return items


# ---------------------------------------------------------------------------
# Bundle assembly
# ---------------------------------------------------------------------------


def _safe_dir_name(range_spec: str) -> str:
    """Filesystem-safe directory name for a range spec.

    Sanitization alone is lossy (`feature/x..HEAD` and `feature-x..HEAD`
    both sanitize to `feature-x..HEAD`), so any spec the sanitizer altered
    gets a short deterministic hash suffix to keep distinct specs in
    distinct bundle dirs -- e.g. `origin/main..HEAD` ->
    `origin-main..HEAD-<sha1[:8]>`. Already-safe specs like
    `__working_tree__` stay as-is (underscore is safe).
    """
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", range_spec).strip("-")
    if safe == range_spec:
        return safe
    digest = hashlib.sha1(range_spec.encode("utf-8")).hexdigest()[:8]
    return f"{safe}-{digest}" if safe else digest


def parse_range_arg(arg: str) -> str:
    """Normalize a user-provided range argument.

    Bare ref `<ref>` becomes `<ref>..HEAD`. `<a>..<b>` and `<a>...<b>`
    pass through. `--staged` and `--working` map to sentinels.
    """
    if arg == "--staged":
        return "__staged__"
    if arg == "--working":
        return "__working_tree__"
    if ".." in arg:
        return arg
    return f"{arg}..HEAD"


def _range_base(range_spec: str) -> Optional[str]:
    """The revision the pre-image of a changed file should be read from.

    For a normal `<a>..HEAD` / `<a>..<b>` range the base is the left side. For
    a symmetric `<a>...<b>` range git diffs from the merge-base, so the base is
    `git merge-base a b`. Working-tree / staged / merge / rebase modes are all
    diffed against HEAD, so HEAD is their natural base. Returns None when no
    base can be resolved (the caller then treats the file as an add).
    """
    if range_spec in (
        "__working_tree__", "__staged__",
        "__merge_in_progress__", "__rebase_in_progress__",
    ):
        return "HEAD"
    if "..." in range_spec:
        a, b = range_spec.split("...", 1)
        rc, out, _ = run_git(["merge-base", a or "HEAD", b or "HEAD"])
        return out.strip() if rc == 0 and out.strip() else None
    if ".." in range_spec:
        a = range_spec.split("..", 1)[0]
        return a or "HEAD"
    return None


def materialize_preimage(range_spec: str, path: str, bundle_dir: Path) -> Optional[str]:
    """Write `path`'s content at the range base into the bundle; return its path.

    `git show <base>:<path>` fails when the file did not exist at the base
    (an add, or the post-rename side of a rename); that case returns None so
    the subject-lens reviewer treats every finding as attributable.
    """
    base = _range_base(range_spec)
    if base is None:
        return None
    rc, out, _ = run_git(["show", f"{base}:{path}"])
    if rc != 0:
        return None
    dest = bundle_dir / preimage_relpath(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(out, encoding="utf-8")
    return str(dest)


def _range_base_sha(range_spec: str) -> Optional[str]:
    """Resolve the range base to a concrete SHA (the ledger baseline).

    The base ref (`_range_base`) is a ref like `origin/main` for a normal range
    and an already-resolved merge-base SHA for a symmetric range; either way
    `git rev-parse` yields the concrete SHA. When the base advances (origin/main
    moves, HEAD changes for working-tree mode), the SHA changes -- so a stored
    ledger entry keyed to the old base goes stale and the finding re-surfaces.
    """
    base = _range_base(range_spec)
    if base is None:
        return None
    rc, out, _ = run_git(["rev-parse", base])
    return out.strip() if rc == 0 and out.strip() else None


def build_bundle(
    range_spec: str,
    bundle_dir: Path,
    auto_reason: Optional[str] = None,
    claim_globs: Optional[list[str]] = None,
    ledger_path: Optional[Path] = None,
    review_generated: bool = False,
) -> dict:
    """Gather context for `range_spec`, write chunks to disk, return the index bundle.

    When `claim_globs` is non-empty, changed files whose repo-relative path
    matches a claim pattern are held back from the generic reviewers (see
    assemble_bundle): their pre-image is materialized into the bundle and they
    are surfaced under a top-level `claimed_files` list instead of
    `changed_files`. When empty the bundle is byte-identical to today's.

    Machine-generated files (detected from content -- see
    bootstrap_lib.code_review.generated) are likewise held back from the
    reviewers and surfaced under a top-level `generated_files` list, because
    the review target is the GENERATOR, not its output. `review_generated=True`
    disables that exclusion for an author who explicitly wants the full review.
    """
    claim_globs = claim_globs or []
    repo_root = get_repo_root()
    if repo_root is None:
        raise ValueError("not inside a git repository")

    diff = fetch_diff(range_spec)
    changed = fetch_changed_files(range_spec)
    description = fetch_description(range_spec)

    rc, head_out, _ = run_git(["rev-parse", "--short", "HEAD"])
    head_sha = head_out.strip() if rc == 0 else ""
    branch = get_current_branch() or "DETACHED"

    preamble, sections = _git_diff_to_sections(diff)
    files = [
        {
            "identifier": path,
            "path": path,
            "local": str((repo_root / path).resolve()),
            "status": status,
        }
        for status, path in changed
    ]
    # Materialize pre-images for claimed files BEFORE assembly so the front-half
    # keeps the VCS-specific mechanics; assemble_bundle only routes/excludes.
    for f in files:
        if matches_claim(f["identifier"], claim_globs):
            f["pre_image"] = materialize_preimage(range_spec, f["path"], bundle_dir)
    core = assemble_bundle(
        preamble=preamble,
        sections=sections,
        files=files,
        bundle_dir=bundle_dir,
        max_chunk_bytes=MAX_CHUNK_BYTES,
        workspace_root=repo_root,
        claim_globs=claim_globs,
        review_generated=review_generated,
    )
    changed_files = core["changed_files"]

    # Touched parent dirs for the untracked/unstaged scan.
    touched_dirs: list[Path] = []
    seen_dirs: set[Path] = set()
    for cf in changed_files:
        d = Path(cf["local"]).parent
        if d not in seen_dirs:
            seen_dirs.add(d)
            touched_dirs.append(d)

    untracked_or_unstaged = find_untracked_or_unstaged(repo_root, touched_dirs)
    merge_conflicts = find_merge_conflicts(repo_root)

    # Declined-findings ledger. The baseline is the range base SHA; when it moves
    # (origin/main advances, HEAD changes for working-tree mode) previously-
    # declined findings re-surface. Falls back to head_sha when no base resolves.
    ledger_baseline = _range_base_sha(range_spec) or head_sha or ""
    change_id = range_spec
    ledger_hits = ledger.ledger_hits(ledger_path or LEDGER_PATH, change_id, ledger_baseline)

    bundle: dict = {
        "vcs": "git",
        "range": range_spec,
        "head_sha": head_sha,
        "branch": branch,
        "description": description,
        "bundle_dir": core["bundle_dir"],
        "diff_chunks": core["diff_chunks"],
        "changed_files": changed_files,
        "unique_claude_mds": core["unique_claude_mds"],
        "untracked_or_unstaged": untracked_or_unstaged,
        "merge_conflicts": merge_conflicts,
        "submit_gates": core["submit_gates"],
        "change_id": change_id,
        "ledger_baseline": ledger_baseline,
        "ledger_hits": ledger_hits,
    }
    if auto_reason:
        bundle["auto_detected_reason"] = auto_reason
    if claim_globs:
        bundle["claimed_files"] = core.get("claimed_files", [])
    if core.get("generated_files"):
        bundle["generated_files"] = core["generated_files"]
    return bundle


def _parse_args(args: list[str]) -> tuple[list[str], list[str], bool]:
    """Split argv[1:] into (positionals, claim_globs, review_generated).

    `--claim <glob>` (repeatable) and `--claim=<glob>` collect claim patterns;
    `--review-generated` turns OFF generated-artifact exclusion so those files
    are chunked and reviewed like any other; everything else (a range/ref,
    `--staged`, `--working`) is a positional. Raises ValueError on a `--claim`
    with no value.
    """
    positionals: list[str] = []
    claim_globs: list[str] = []
    review_generated = False
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--review-generated":
            review_generated = True
            i += 1
        elif a == "--claim":
            if i + 1 >= len(args):
                raise ValueError("--claim requires a glob argument")
            claim_globs.append(args[i + 1])
            i += 2
        elif a.startswith("--claim="):
            claim_globs.append(a[len("--claim="):])
            i += 1
        else:
            positionals.append(a)
            i += 1
    return positionals, claim_globs, review_generated


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[1] == "--ledger-record":
        try:
            n = ledger.record_from_file(LEDGER_PATH, Path(argv[2]))
        except (OSError, ValueError) as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        print(f"prepare_review: recorded {n} declined finding(s) into the ledger.", file=sys.stderr)
        return 0

    try:
        positionals, claim_globs, review_generated = _parse_args(argv[1:])
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2

    auto_reason: Optional[str] = None
    if len(positionals) == 0:
        try:
            range_spec, auto_reason = detect_default_range()
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
    elif len(positionals) == 1:
        range_spec = parse_range_arg(positionals[0])
    else:
        print(
            "Usage: prepare_review.py [<ref>|<a>..<b>|<a>...<b>|--staged|--working] "
            "[--claim <glob> ...] [--review-generated]",
            file=sys.stderr,
        )
        return 2

    bundle_dir = DEFAULT_BUNDLE_ROOT / _safe_dir_name(range_spec)
    bundle_dir.mkdir(parents=True, exist_ok=True)
    try:
        bundle = build_bundle(
            range_spec,
            bundle_dir,
            auto_reason=auto_reason,
            claim_globs=claim_globs,
            review_generated=review_generated,
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    return emit_bundle(bundle, bundle_dir)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
