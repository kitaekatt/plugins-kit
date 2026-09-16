"""Durable guard: pure detectors for bare-Python interpreter invocations in
tracked manifests (bootstrap.json / env.json) and tracked shell-family files
(*.sh, *.cmd, *.bat, *.ps1, plugins/*/bin/*, .githooks/*).

A manifest command must reach Python only through the bootstrap-provisioned
interpreter variable (see plugins/bootstrap/bootstrap_lib/interpreter_env.py),
never a bare `python`/`python3`/`py`/`pythonw`/`python3.NN`/`python.exe`
command word and never a bare `uv run ... python`. `scan_manifest_text` finds
every such hit in manifest JSON text.

Shell-family files have several legitimate reasons to select an interpreter
directly (a launcher shim choosing among a plugin venv / the standalone
Python / PATH, a bootstrap entry point that must run before any venv exists,
a maintainer script under `uv run python`, ...). `scan_shell_text` finds every
hit without judging whether it is legitimate -- classification is deliberately
NOT baked in here.

Four real-file lanes (shell family, Python outside tests/, plugins/**
Markdown, docs/root Markdown) apply a verdict layer on top of the scanners:
a hit conforms through the maintainer `uv run` bypass (variant (a)) or a
whole-file allowlist entry whose anchor must still be in the file; any
other hit fails its lane. T7a (manifests) has no allowlist at all: a
tracked manifest never calls a bare python.
"""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path
from typing import NamedTuple

_REPO_ROOT = Path(__file__).resolve().parents[2]

# --- shared token pattern -------------------------------------------------
#
# A bare interpreter token, not part of a longer path or identifier:
#   - not preceded by an identifier or path character -- matches the existing
#     manifest-audit convention: "/usr/bin/python3", "$BOOTSTRAP_PYTHON", and
#     "mypython3"/"pythonic" never match (only case-sensitive lowercase
#     "python..." tokens match at all, so "$BOOTSTRAP_PYTHON" is additionally
#     excluded by case).
#   - not immediately preceded by "uv " -- "uv python list" / "uv python pin"
#     are a different uv subcommand, not a python invocation.
#   - followed only by a boundary that looks like an actual invocation
#     (whitespace, a closing quote/paren, a shell operator, or end of line)
#     rather than an identifier/path continuation (".", "-", alnum) or a
#     label colon (`"python3: ok"` log messages) or prose punctuation
#     (`"no python, jq"` -- trailing comma).
_NOT_BEFORE = r"(?<![A-Za-z0-9_./\\-])(?<!uv )(?<!uv\t)"
_SAFE_AFTER = r"(?=[\s\"');|&`]|$)"

_BARE_TOKEN_RE = re.compile(
    _NOT_BEFORE
    + r"(python3(?:\.\d+)?|pythonw|python\.exe|python|py)"
    + _SAFE_AFTER
)

# `uv run [flags] python`. Only `--project <path>` / `--project=<path>`
# consumes a following value; any other flag (e.g. `--no-project`) is treated
# as a bare switch so it does not eat the trailing `python` word.
_UV_RUN_PYTHON_RE = re.compile(
    r"\buv\s+run\b"
    r"(?:\s+--project(?:=|\s+)\S+"
    r"|\s+--[A-Za-z0-9][A-Za-z0-9_-]*(?!\S))*"
    r"\s+python\b"
)

_PROJECT_FLAG_RE = re.compile(r"--project(?:=|\s+)\S+")


class ManifestHit(NamedTuple):
    line: int
    token: str


class ShellHit(NamedTuple):
    line: int
    token: str
    kind: str  # "bare" or "uv_run"
    has_project_flag: bool
    text: str


def scan_manifest_text(text: str) -> list[ManifestHit]:
    """Every bare-python hit in manifest JSON text (bootstrap.json / env.json).

    A manifest command reaches Python only through
    "${BOOTSTRAP_PYTHON:?requires bootstrap >= ...}" (or the
    BOOTSTRAP_PROJECT_PYTHON sibling for project env_checks); any bare
    python/python3/py/pythonw/python.exe/python3.N, or a bare
    `uv run ... python`, is a violation. Pure text scan (no JSON parsing) --
    a bare token can never legally appear anywhere in a manifest, so no
    field-path awareness is needed to catch it.
    """
    hits: list[ManifestHit] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        uv_match = _UV_RUN_PYTHON_RE.search(line)
        if uv_match:
            hits.append(ManifestHit(lineno, uv_match.group(0)))
            continue
        for m in _BARE_TOKEN_RE.finditer(line):
            hits.append(ManifestHit(lineno, m.group(1)))
    return hits


def scan_shell_text(path: str, text: str) -> list[ShellHit]:
    """Every bare-python hit in a tracked shell-family file's text.

    `path` is accepted (repo-relative, forward slashes) for a future
    classifier's use -- e.g. deciding whether a location is a repo-maintainer
    script -- but this function itself does not judge conformance; it only
    detects and describes each hit (kind, whether an explicit --project flag
    is present on the same line). See the module docstring for why
    classification is deliberately not built here yet.
    """
    hits: list[ShellHit] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        uv_match = _UV_RUN_PYTHON_RE.search(line)
        if uv_match:
            hits.append(ShellHit(
                lineno, uv_match.group(0), "uv_run",
                bool(_PROJECT_FLAG_RE.search(line)), line,
            ))
            continue
        for m in _BARE_TOKEN_RE.finditer(line):
            hits.append(ShellHit(lineno, m.group(1), "bare", False, line))
    return hits


# --- git helpers (tests only; scanners above stay pure / disk-free) -------

def _git_ls_files(*patterns: str) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(_REPO_ROOT), "ls-files", *patterns],
        capture_output=True, text=True, check=True,
    )
    return [line for line in result.stdout.splitlines() if line]


_MANIFEST_PATTERNS = ("*bootstrap.json", "*env.json")


# --- T7a: real tracked manifests, zero hits -------------------------------

def test_tracked_manifests_use_no_bare_python():
    """No tracked bootstrap.json/env.json calls a bare Python interpreter.

    Revert proof: inject `"install": {"windows": "python3 -c 1"}` into any
    tracked manifest and this goes red immediately (no fixture needed to
    prove it -- the assertion reads every real tracked manifest).
    """
    offenders = []
    for rel in _git_ls_files(*_MANIFEST_PATTERNS):
        text = (_REPO_ROOT / rel).read_text(encoding="utf-8")
        for hit in scan_manifest_text(text):
            offenders.append(f"{rel}:{hit.line} ({hit.token!r})")
    assert not offenders, "bare Python in a tracked manifest:\n" + "\n".join(offenders)


# --- T7a': counterfactual for T7a -----------------------------------------

def test_scan_manifest_text_catches_injected_token():
    """Prove scan_manifest_text actually detects a violation (without
    touching a tracked file) -- the counterfactual T7a needs but cannot show
    by itself, since T7a's assertion is "zero hits" and a scanner that never
    matches anything would also show zero hits."""
    violating = (
        '{"tools": [{"name": "x", '
        '"install": {"windows": "python3 -c 1"}}]}'
    )
    hits = scan_manifest_text(violating)
    assert [h.token for h in hits] == ["python3"]

    violating_uv_run = (
        '{"tools": [{"name": "x", "check": "uv run python -c 1"}]}'
    )
    hits2 = scan_manifest_text(violating_uv_run)
    assert hits2 and hits2[0].token == "uv run python"

    clean = (
        '{"tools": [{"name": "x", "check": '
        '"\\"${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}\\" -c 1"}]}'
    )
    assert scan_manifest_text(clean) == []


# --- T7b': counterfactual for the (deferred) real-shell-file test --------

def test_scan_shell_text_catches_injected_violation():
    """Prove scan_shell_text detects both hit shapes -- a bare interpreter
    token, and a bare `uv run ... python` -- and correctly leaves an
    absolute-path invocation and a project-scoped `uv run` alone."""
    bare_violation = 'PY="python3"\nexec "$PY" "$@"\n'
    hits = scan_shell_text("plugins/example/bin/example", bare_violation)
    assert [h.token for h in hits] == ["python3"]
    assert hits[0].kind == "bare"

    uv_run_violation = 'uv run python -c "print(1)"\n'
    hits2 = scan_shell_text("plugins/example/scripts/example.sh", uv_run_violation)
    assert hits2 and hits2[0].kind == "uv_run"
    assert hits2[0].has_project_flag is False

    uv_run_with_project = 'uv run --project "$PLUGIN_ROOT" python -c "print(1)"\n'
    hits3 = scan_shell_text("plugins/example/scripts/example.sh", uv_run_with_project)
    assert hits3 and hits3[0].has_project_flag is True

    absolute_path_only = '"$REPO_ROOT/.venv/bin/python" "$@"\n'
    assert scan_shell_text("scripts/example.sh", absolute_path_only) == []

    uv_python_list = 'uv python list --only-installed\n'
    assert scan_shell_text("scripts/example.sh", uv_python_list) == []

    log_label = 'log_entry "python3: ok - found at $WANT_PYTHON"\n'
    assert scan_shell_text("plugins/example/hooks/x.sh", log_label) == []


# ===========================================================================
# The remaining three lanes (shell/cmd/bat/bin/githooks real-file check,
# python outside tests/, markdown) plus the shared verdict layer and
# allowlist. See the module docstring's STATUS note for why T7b/T7b'' were
# deferred; that deferral is now resolved for shell (and extended to python
# and markdown) using the R3 mapping in bootstrap-python-plan-v3.md section 6
# and interface-v3.md section 6 ("Lint (U7), CLI (U2r), guard (U4r)").
#
# Two mechanisms decide whether a real hit conforms, applied in this order:
#
#  1. The MAINTAINER_UV_RUN_ALLOWED module constant (variant (a), the
#     orchestrator's recommendation): a `uv run ... python` hit auto-conforms
#     when the path is a maintainer-typed command location (repo-root
#     scripts/, .githooks/, a root-level *.md file, or anything under
#     docs/) -- see _is_maintainer_uv_run_context. This is a PER-HIT rule
#     (only uv_run-kind hits qualify) and never applies under plugins/**
#     (those paths never match the prefixes), which keeps the plugins/**
#     markdown lane strict regardless of the constant.
#
#  2. The `{path: AllowlistEntry(anchor, reason)}` allowlist: a whole-file
#     exemption for producers, diagnostics, and deterministic-first
#     bootstrap consumers (R3 mapping row 1), plus a handful of documented
#     false positives and warning/anti-pattern prose. `anchor` is a literal
#     substring that must still be present in the file (see the staleness
#     test) so a later unrelated edit cannot silently invalidate the
#     exemption's reasoning.
#
# A hit covered by neither is an offender: either a genuine finding pending
# a fix from the audit unit that owns the file, or (rarer) a gap in this
# unit's classification -- both are reported, never silently dropped.
# ===========================================================================

class AllowlistEntry(NamedTuple):
    anchor: str
    reason: str


# Variant (a) -- recommended, per bootstrap-python-plan-v3.md section 6:
# "uv run [--extra dev] python" survives as a named exception for
# plugins-kit MAINTAINER commands. Flip to False to model variant (b)
# (strict): every uv_run hit becomes a finding regardless of path, and
# every allowlist/report note below that cites this constant stops applying
# (see this unit's handback report for the itemized (b) delta -- the guard
# code does not need to change further, only this one flag and the report).
MAINTAINER_UV_RUN_ALLOWED = True


def _is_maintainer_uv_run_context(path: str) -> bool:
    """A maintainer-typed command location under variant (a): repo-root
    scripts/, .githooks/, a root-level *.md file (CLAUDE.md, CONTRIBUTING.md,
    ...), or anything under docs/. Never true for a plugins/** path, which is
    what keeps the plugins/** markdown lane strict under either variant."""
    if path.startswith("scripts/") or path.startswith(".githooks/"):
        return True
    if path.startswith("docs/"):
        return True
    if path.endswith(".md") and "/" not in path:
        return True
    return False


# gen_code_review_skills.py lives under scripts/ (a maintainer location) but
# its uv_run literals are emitted INTO shipped plugins/** skill text; R3
# mapping row 2 forces those to "${BOOTSTRAP_PYTHON:?...}" regardless of
# variant, so the scripts/ bypass must not apply to this one path.
_FORCED_REGARDLESS_OF_VARIANT = {"scripts/gen_code_review_skills.py"}


def verdict_for_path(
    path: str,
    hits: list[ShellHit],
    allowlist: dict[str, AllowlistEntry],
    preload_lines: set[int] = frozenset(),
) -> list[str]:
    """Verdict layer: classify each hit in one tracked path as conforming
    (maintainer uv_run bypass, a `uv run --no-project python` skill preload
    on one of `preload_lines`, or the path is in `allowlist`) or an
    offender. Returns offender description strings (empty when the path
    fully conforms)."""
    offenders = []
    for hit in hits:
        if hit.kind == "uv_run" and hit.line in preload_lines:
            continue
        if (
            hit.kind == "uv_run"
            and MAINTAINER_UV_RUN_ALLOWED
            and _is_maintainer_uv_run_context(path)
            and path not in _FORCED_REGARDLESS_OF_VARIANT
        ):
            continue
        if path in allowlist:
            continue
        offenders.append(f"{path}:{hit.line} [{hit.kind}] {hit.token!r}")
    return offenders


# --- python source scanner (data) ------------------------------------------

def _python_docstring_lines(text: str) -> set[int]:
    """1-based line numbers covered by a module/class/function docstring --
    an AST-exact companion to the comment/shebang stripping below (a regex
    cannot reliably tell a docstring from a code string, but ast.parse can).
    Returns an empty set for unparseable text rather than raising -- a
    scanner must never crash on a tracked file it cannot fully understand."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()
    lines: set[int] = set()
    nodes = [tree] + [
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    for node in nodes:
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            lines.update(range(first.lineno, first.end_lineno + 1))
    return lines


def scan_python_text(path: str, text: str) -> list[ShellHit]:
    """Every bare-python hit in a tracked Python source file's text (outside
    tests/). Shares scan_shell_text's core detection (a string literal,
    comment, or docstring naming a bare interpreter command or a bare `uv
    run ... python`) but additionally excludes three shapes that are never
    an invocation site in Python source: a line-1 shebang, a full-line `#`
    comment, and any line inside a module/class/function docstring. Without
    that exclusion, ordinary explanatory prose (a docstring warning "a bare
    `python` or `uv run` lands in a different environment", vendored across
    nine bootstrap_guard.py copies) would swamp every real hit; WITH it, a
    real hit -- a string literal naming a bare interpreter in actual code,
    or a genuine `uv run ... python` command line -- still surfaces."""
    hits: list[ShellHit] = []
    doc_lines = _python_docstring_lines(text)
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if lineno == 1 and stripped.startswith("#!"):
            continue
        if stripped.startswith("#"):
            continue
        if lineno in doc_lines:
            continue
        uv_match = _UV_RUN_PYTHON_RE.search(line)
        if uv_match:
            hits.append(ShellHit(
                lineno, uv_match.group(0), "uv_run",
                bool(_PROJECT_FLAG_RE.search(line)), line,
            ))
            continue
        for m in _BARE_TOKEN_RE.finditer(line):
            hits.append(ShellHit(lineno, m.group(1), "bare", False, line))
    return hits


# --- markdown scanner (data) ------------------------------------------------

_FENCE_LINE_RE = re.compile(r"^\s*`{3,}[A-Za-z0-9_+-]*\s*$")


def scan_markdown_text(path: str, text: str) -> list[ShellHit]:
    """Every bare-python hit in a tracked Markdown file's text. Shares
    scan_shell_text's core detection, with one exclusion: a fenced-code-block
    delimiter line that is only a language tag (```` ```python ````, opening
    or closing) names a language, not a command, and is excluded. A
    documented command inside the fence body, or an inline code span, is
    still scanned -- the exclusion is narrowly the tag line itself."""
    hits: list[ShellHit] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if _FENCE_LINE_RE.match(line):
            continue
        uv_match = _UV_RUN_PYTHON_RE.search(line)
        if uv_match:
            hits.append(ShellHit(
                lineno, uv_match.group(0), "uv_run",
                bool(_PROJECT_FLAG_RE.search(line)), line,
            ))
            continue
        for m in _BARE_TOKEN_RE.finditer(line):
            hits.append(ShellHit(lineno, m.group(1), "bare", False, line))
    return hits


# --- skill preload scanner (data) ------------------------------------------
#
# A skill's `!` preload (inline `` !`cmd` `` at line start or after
# whitespace, or a ```` ```! ```` fenced block) runs BEFORE Claude sees the
# skill, and Claude Code refuses a preload command that contains a shell
# expansion ("Shell command permission check failed ...: Contains expansion";
# probe 2026-09-16). Only the names Claude Code itself substitutes in skill
# content survive, so a preload can never read BOOTSTRAP_PYTHON: its
# conforming interpreter form is `uv run --no-project python`
# (python-interpreter.md, "Skill preload commands").

#: `${NAME}` forms Claude Code substitutes in skill content before a preload
#: runs (code.claude.com/docs/en/skills, "Available string substitutions").
#: `$ARGUMENTS`, `$N` and declared `$name` arguments are brace-less.
CLAUDE_SKILL_SUBSTITUTIONS = frozenset({
    "CLAUDE_SESSION_ID", "CLAUDE_EFFORT", "CLAUDE_SKILL_DIR",
    "CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT", "CLAUDE_PLUGIN_DATA",
})

_INLINE_PRELOAD_RE = re.compile(r"(?:^|(?<=\s))!`([^`\n]+)`")
_PRELOAD_FENCE_OPEN_RE = re.compile(r"^\s*```!\s*$")
_FENCE_CLOSE_RE = re.compile(r"^\s*```\s*$")
_BRACED_EXPANSION_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)")
_PRELOAD_UV_PYTHON = "uv run --no-project python"


class PreloadCommand(NamedTuple):
    line: int
    command: str


def scan_skill_preloads(text: str) -> list[PreloadCommand]:
    """Every skill preload command in Markdown text: inline `` !`cmd` ``
    placeholders (``!`` at line start or after whitespace, as Claude Code
    recognizes them) and each line of a ```` ```! ```` fenced block."""
    found: list[PreloadCommand] = []
    in_fence = False
    for lineno, line in enumerate(text.splitlines(), start=1):
        if in_fence:
            if _FENCE_CLOSE_RE.match(line):
                in_fence = False
            elif line.strip():
                found.append(PreloadCommand(lineno, line.strip()))
            continue
        if _PRELOAD_FENCE_OPEN_RE.match(line):
            in_fence = True
            continue
        for m in _INLINE_PRELOAD_RE.finditer(line):
            found.append(PreloadCommand(lineno, m.group(1)))
    return found


def preload_expansion_findings(path: str, text: str) -> list[str]:
    """A preload command that expands anything Claude Code does not
    substitute is refused at render time, so the skill never runs."""
    findings = []
    for pre in scan_skill_preloads(text):
        for m in _BRACED_EXPANSION_RE.finditer(pre.command):
            if m.group(1) not in CLAUDE_SKILL_SUBSTITUTIONS:
                findings.append(f"{path}:{pre.line} preload expands ${{{m.group(1)}}}")
    return findings


def conforming_preload_lines(text: str) -> set[int]:
    """Lines whose preload command launches Python the only way a preload
    can: `uv run --no-project python`."""
    return {pre.line for pre in scan_skill_preloads(text)
            if _PRELOAD_UV_PYTHON in pre.command}


# --- per-lane injected-string counterfactuals ------------------------------

def test_scan_python_text_ignores_comment_shebang_and_docstring_but_catches_real_hit():
    """Prove scan_python_text's three Python-specific exclusions actually
    exclude (a shebang, a full-line comment, and a docstring mention -- all
    real shapes found across the repo's vendored bootstrap_guard.py copies
    and engine.py) while still detecting a genuine hit in real code.

    Revert proof: delete the shebang/comment/docstring `continue` guards
    (fall back to scan_shell_text's plain per-line scan) and this goes red
    immediately -- the excluded lines re-appear as extra hits alongside the
    real one."""
    text = (
        '#!/usr/bin/env python3\n'
        '"""Module docstring: a bare `python` or `uv run python` invocation\n'
        'lands in the wrong environment.\n'
        '"""\n'
        '# comment: python3 is required here\n'
        'def f():\n'
        '    """python is mentioned in this docstring too."""\n'
        '    subprocess.run(["python3", "real.py"])\n'
    )
    hits = scan_python_text("plugins/example/scripts/example.py", text)
    assert [(h.line, h.token, h.kind) for h in hits] == [(8, "python3", "bare")]


def test_scan_markdown_text_ignores_fence_tag_but_catches_documented_command():
    """Prove scan_markdown_text's fence-tag exclusion excludes only the
    ```` ```python ```` delimiter line itself, while still detecting a bare
    command inside the fence body and a bare command outside it (an inline
    code span).

    Revert proof: delete the `_FENCE_LINE_RE` guard and this goes red --
    every ```` ```python ````/````` ``` ````` pair in the repo's ~30 UE
    Python-API-example fences would register as a spurious hit."""
    text = (
        '```python\n'
        'subprocess.run(["python3", "real.py"])\n'
        '```\n'
        'Run `python script.py` to reproduce.\n'
    )
    hits = scan_markdown_text("docs/example.md", text)
    assert [(h.line, h.token, h.kind) for h in hits] == [
        (2, "python3", "bare"),
        (4, "python", "bare"),
    ]


def test_variable_and_nested_project_form_produce_no_hits():
    """The two sanctioned conforming forms -- the plain engine-var guard and
    the nested project-then-engine guard from bootstrap-python-plan-v3.md
    section 11 -- must never themselves register as a hit, in any scanner.

    THREE independent safeguards protect these forms against the shared
    regex: case sensitivity, the `_` in `_NOT_BEFORE`'s identifier-boundary
    exclusion, and the `:` that follows the variable name in `${VAR:?...}`/
    `${VAR:-...}` not being in `_SAFE_AFTER`'s character set (U4r confirmed
    by probing IGNORECASE + a widened `_NOT_BEFORE` together -- still zero
    hits, because the trailing `:` boundary alone still blocks the match).
    Revert proof for the test itself, not the shared regex: replace `plain`
    below with a genuine bare violation (e.g. `python -c 1`) and this goes
    red immediately -- it is not a vacuously-true "== []" check."""
    plain = '"${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" -c "import sys"\n'
    nested = (
        '"${BOOTSTRAP_PROJECT_PYTHON:-${BOOTSTRAP_PYTHON:?requires bootstrap '
        '>= 0.120.0}}" -m pytest\n'
    )
    for text in (plain, nested):
        assert scan_manifest_text(text) == []
        assert scan_shell_text("scripts/example.sh", text) == []
        assert scan_python_text("scripts/example.py", text) == []
        assert scan_markdown_text("docs/example.md", text) == []


# ===========================================================================
# Real-file lanes. See the module-level comment above verdict_for_path for
# the two conformance mechanisms. Universe = tracked files matched by each
# lane's git ls-files pattern, minus tests/** (out of scope everywhere --
# u5b/u5c's inventories confirmed every tests/** hit already conforms
# regardless, so excluding the directory changes no verdict).
#
# Every real-file lane is GREEN against the integrated tree: the audit
# units' reroutes landed, and each remaining hit is either the
# maintainer bypass or an allowlisted file whose entry states why. A new
# hit is a finding to FIX in the file; add an allowlist entry only for a
# producer, a diagnostic, a deterministic-first consumer, or prose that
# names an interpreter without instructing anyone to run it.
# ===========================================================================


# --- shell/cmd/bat/bin/githooks real-file lane ------------------------------

_SHELL_PATTERNS = ("*.sh", "*.cmd", "*.bat", "plugins/*/bin/*", ".githooks/*")

_SHELL_ALLOWLIST: dict[str, AllowlistEntry] = {
    ".githooks/pre-commit": AllowlistEntry(
        "Python 3 is required to run the public-repo guard",
        "class D git hook; deterministic .venv path checked first, "
        "PATH-name fallback chain (python3/python/py) is the forced "
        "last resort"),
    "plugins/bootstrap-stuck-fix/hooks/sessionstart/repair-registry.sh": AllowlistEntry(
        "Find a usable Python",
        "stuck-fix producer; must not depend on bootstrap or any "
        "provisioned venv (own file header)"),
    "plugins/bootstrap/hooks/sessionstart/session-bootstrap.sh": AllowlistEntry(
        "python3: ok - found at",
        "class A producer (the SessionStart hook itself); deterministic "
        "standalone path checked before the PATH-name fallback"),
    "plugins/bootstrap/hooks/userpromptsubmit/bootstrap-display.sh": AllowlistEntry(
        "Invoke bootstrap's standalone Python by absolute path (NOT bare python/python3,",
        "class B Claude Code hook script; deterministic path first, "
        "PATH-name fallback documented at the anchor"),
    "plugins/bootstrap/scripts/diagnose-python-venv.sh": AllowlistEntry(
        "Deterministic standalone path first; BOOTSTRAP_PYTHON accepted only as a",
        "class E diagnostic: probes python.exe under uv's install directory by "
        "design; its own interpreter is deterministic-first, then the "
        "validated BOOTSTRAP_PYTHON, then PATH"),
    "plugins/bootstrap/scripts/bootstrap.sh": AllowlistEntry(
        "Same locations session-bootstrap.sh uses, in the same order",
        "class A producer (installer lever); same deterministic-first "
        "order as session-bootstrap.sh"),
    "plugins/hue-kit/bin/hue-kit": AllowlistEntry(
        "Prefer the standalone Python bootstrap installs; the CLI re-execs into the",
        "class C launcher shim; deterministic standalone path first, "
        "PATH-name fallback"),
    "plugins/hue-kit/bin/hue-kit.cmd": AllowlistEntry(
        "Prefer the standalone Python bootstrap installs (avoids the WindowsApps",
        "class C launcher shim (Windows); same deterministic-first pattern"),
    "plugins/job-kit/bin/job-kit": AllowlistEntry(
        "the plugin venv bootstrap provisions -- it has the shared-lib links",
        "class C launcher shim; deterministic standalone path first, "
        "PATH-name fallback"),
    "plugins/job-kit/bin/job-kit.cmd": AllowlistEntry(
        "the plugin venv bootstrap provisions -- it has the shared-lib links",
        "class C launcher shim (Windows); same deterministic-first pattern"),
    "plugins/llm-scripting-kit/bin/llm-scripting-kit": AllowlistEntry(
        "the plugin venv bootstrap provisions -- it has PyYAML, which the CLI",
        "class C launcher shim; deterministic standalone path first, "
        "PATH-name fallback, documented graceful stdlib-only degrade"),
    "plugins/llm-scripting-kit/bin/llm-scripting-kit.cmd": AllowlistEntry(
        "the plugin venv bootstrap provisions -- it has PyYAML, which the CLI",
        "class C launcher shim (Windows); same deterministic-first pattern"),
    "plugins/secrets-kit/bin/secrets-kit": AllowlistEntry(
        "Fall back to anything on PATH; bootstrap normally guarantees the",
        "class C launcher shim; documented stdlib-only graceful degrade to PATH"),
    "plugins/secrets-kit/bin/secrets-kit.cmd": AllowlistEntry(
        "Windows shim invoking the bundled CLI via standalone Python.",
        "class C launcher shim (Windows); same deterministic-first pattern"),
    "plugins/unreal-kit/hooks/pretooluse/check-editor-build-fresh.sh": AllowlistEntry(
        "Deterministic standalone path first; BOOTSTRAP_PYTHON accepted only",
        "class B Claude Code hook script; deterministic standalone path, then "
        "the validated BOOTSTRAP_PYTHON, then the PATH-name fallback"),
    "scripts/check-staged-version-bump.sh": AllowlistEntry(
        "Interpreter resolution mirrors pre-commit-version-check.sh",
        "class D-ish repo-root maintainer script; deliberately avoids uv "
        "so it works on an unprovisioned clone"),
    "scripts/pre-commit-version-check.sh": AllowlistEntry(
        "fast, with no venv sync, and usable on an unprovisioned clone",
        "class D-ish repo-root maintainer script; stdlib-only, "
        "deliberately avoids uv per its own documented reason"),
}


def test_tracked_shell_family_bare_python_is_allowlisted_or_pending():
    """Every bare-python hit in a tracked shell/cmd/bat/bin/githooks file is
    either the maintainer uv_run bypass, allowlisted (a producer/diagnostic/
    deterministic-first consumer), or a reported pending finding.

    Revert proof: remove any one allowlist entry above (e.g. hue-kit/bin/
    hue-kit) and this goes red immediately for that file's PATH-fallback
    tokens, which are real hits already present in the tracked file."""
    offenders = []
    for rel in _git_ls_files(*_SHELL_PATTERNS):
        text = (_REPO_ROOT / rel).read_text(encoding="utf-8")
        hits = scan_shell_text(rel, text)
        offenders.extend(verdict_for_path(rel, hits, _SHELL_ALLOWLIST))
    assert not offenders, (
        "unallowlisted bare-python hit in a tracked shell-family file "
        "(fix the file, or allowlist it with its reason):\n" + "\n".join(offenders)
    )


# --- python outside tests/ real-file lane -----------------------------------

_BOOTSTRAP_GUARD_REASON = (
    "vendored canonical bootstrap_guard.py copy; locates the plugin venv "
    "interpreter via os.path candidates to build a resolved path, not a "
    "bare spawn (drift-tested byte-identical to the canonical copy)")

_PYTHON_ALLOWLIST: dict[str, AllowlistEntry] = {
    "plugins/awesome-kit/skills/orchestrate/scripts/bootstrap_guard.py": AllowlistEntry(
        'for rel in (("Scripts", "python.exe"), ("bin", "python"), ("bin", "python3")):',
        _BOOTSTRAP_GUARD_REASON),
    "plugins/awesome-kit/skills/task/scripts/bootstrap_guard.py": AllowlistEntry(
        'for rel in (("Scripts", "python.exe"), ("bin", "python"), ("bin", "python3")):',
        _BOOTSTRAP_GUARD_REASON),
    "plugins/bootstrap/bootstrap_lib/bootstrap_guard.py": AllowlistEntry(
        'for rel in (("Scripts", "python.exe"), ("bin", "python"), ("bin", "python3")):',
        _BOOTSTRAP_GUARD_REASON + " (canonical source)"),
    "plugins/git-kit/scripts/bootstrap_guard.py": AllowlistEntry(
        'for rel in (("Scripts", "python.exe"), ("bin", "python"), ("bin", "python3")):',
        _BOOTSTRAP_GUARD_REASON),
    "plugins/hue-kit/scripts/bootstrap_guard.py": AllowlistEntry(
        'for rel in (("Scripts", "python.exe"), ("bin", "python"), ("bin", "python3")):',
        _BOOTSTRAP_GUARD_REASON),
    "plugins/job-kit/lib/bootstrap_guard.py": AllowlistEntry(
        'for rel in (("Scripts", "python.exe"), ("bin", "python"), ("bin", "python3")):',
        _BOOTSTRAP_GUARD_REASON),
    "plugins/p4-kit/scripts/bootstrap_guard.py": AllowlistEntry(
        'for rel in (("Scripts", "python.exe"), ("bin", "python"), ("bin", "python3")):',
        _BOOTSTRAP_GUARD_REASON),
    "plugins/skills-kit/scripts/bootstrap_guard.py": AllowlistEntry(
        'for rel in (("Scripts", "python.exe"), ("bin", "python"), ("bin", "python3")):',
        _BOOTSTRAP_GUARD_REASON),
    "plugins/unreal-kit/lib/bootstrap_guard.py": AllowlistEntry(
        'for rel in (("Scripts", "python.exe"), ("bin", "python"), ("bin", "python3")):',
        _BOOTSTRAP_GUARD_REASON),
    "plugins/bootstrap/bootstrap_lib/engine.py": AllowlistEntry(
        "python stub: ok - {stub_result.message}",
        "class E diagnostic display text reporting python_stub_check's "
        "result, not an invocation"),
    "plugins/bootstrap/bootstrap_lib/interpreter_env.py": AllowlistEntry(
        '"python", "python.exe")',
        "deterministic standalone-interpreter path construction (P15), "
        "not a bare spawn"),
    "plugins/bootstrap/bootstrap_lib/manifest_lint.py": AllowlistEntry(
        '_BAD_EXACT = {"python": "python"',
        "the linter's own literal vocabulary of bad tokens -- a "
        "self-referential detector needs the strings it detects"),
    "plugins/bootstrap/scripts/bootstrap_cli.py": AllowlistEntry(
        'if args.command == "python":',
        "`python` is the name of the lever's own subcommand "
        "(`bootstrap python`), not an interpreter"),
    "plugins/bootstrap/bootstrap_lib/python_stub_check.py": AllowlistEntry(
        'for name in ("python.exe", "python3.exe"):',
        "class E diagnostic (detects an MS Store Python stub shadowing "
        "the standalone interpreter); must probe PATH by name, per its "
        "own module docstring"),
    "plugins/bootstrap/bootstrap_lib/shared_lib.py": AllowlistEntry(
        '".local", "share", "python-standalone", "python"',
        "deterministic standalone-interpreter path construction, not a "
        "bare spawn"),
    "plugins/bootstrap/bootstrap_lib/venv_check.py": AllowlistEntry(
        'os.path.join(venv_path, "Scripts", "python.exe"),',
        "venv python path construction + diagnostic messages, not a "
        "bare spawn"),
    "scripts/gen_code_review_skills.py": AllowlistEntry(
        "never as a bare path and never as bare `python`/`python3`",
        "launch-note WARNING prose the generator emits into the p4-kit skill; "
        "its launchers are the guarded BOOTSTRAP_PYTHON form (and uv_run "
        "literals here are never bypassed: _FORCED_REGARDLESS_OF_VARIANT)"),
    "plugins/hue-kit/scripts/scene-layers.py": AllowlistEntry(
        'p.read_text(), "python")',
        "syntax-highlighting language-tag string for the script's own "
        "generated output, not a command"),
    "plugins/hue-kit/scripts/scene-meta-groups.py": AllowlistEntry(
        '_py_highlight(text) if lang == "python"',
        "same language-tag false positive as scene-layers.py"),
    "plugins/llm-scripting-kit/lib/llm_scripting_kit/completion/backends.py": AllowlistEntry(
        "Path(sys.argv[0]).name or 'python'",
        "display-label fallback string for an API user field, not a spawn"),
    "plugins/llm-scripting-kit/lib/llm_scripting_kit/frontdoor/server.py": AllowlistEntry(
        'prog="python -m llm_scripting_kit.frontdoor"',
        "argparse prog= display string, not an invocation"),
    "plugins/pdf-kit/custom_bootstrap.py": AllowlistEntry(
        'data_dir / ".venv" / "Scripts" / "python.exe",',
        "resolves the plugin-data venv python path, then subprocess.run "
        "under that resolved absolute path -- not a bare spawn"),
    "plugins/skills-kit/skills/md-domain/scripts/discover_claude_md.py": AllowlistEntry(
        "cpp|h|hpp|cs|py|go|rs|ts|js|lua|yaml|yml|fbs",
        "'py' inside a regex character class of file extensions, not a "
        "command"),
    "plugins/unreal-kit/custom_bootstrap.py": AllowlistEntry(
        "refresh_unreal_stub.py --project-root <project-root>",
        "agent-facing remediation message; bare `python` is the "
        "documented Windows-PATH convention (P8) and the target script "
        "self-reexecs via reexec_under_plugin_venv"),
    "plugins/unreal-kit/lib/ue_runner_config.py": AllowlistEntry(
        "uproject path not configured. Run: python ue_runner.py --setup",
        "same agent-facing P8 pattern; ue_runner.py self-reexecs"),
    "plugins/unreal-kit/scripts/search_unreal_stub.py": AllowlistEntry(
        "refresh_unreal_stub.py --project-root <project-root>",
        "same agent-facing P8 pattern; refresh_unreal_stub.py self-reexecs"),
    "plugins/unreal-kit/skills/ue-python-api/scripts/ue_runner.py": AllowlistEntry(
        "python ue_runner.py --setup                # check/fix project settings",
        "same agent-facing P8 pattern (help/usage text); ue_runner.py "
        "self-reexecs"),
    "plugins/workflow-kit/scripts/openrouter_run.py": AllowlistEntry(
        "with workflow-kit's venv python (bootstrap links llm_scripting_kit onto it via",
        "informational error message naming the plugin-venv interpreter, "
        "not an invocation"),
}


def test_tracked_python_outside_tests_bare_python_is_allowlisted_or_pending():
    """Every bare-python hit in a tracked Python source file outside tests/
    is either the maintainer uv_run bypass, allowlisted, or a reported
    pending finding.

    Revert proof: remove the interpreter_env.py allowlist entry and this
    goes red for its real `"python", "python.exe")` path-construction hit."""
    offenders = []
    for rel in _git_ls_files("*.py"):
        if rel.startswith("tests/"):
            continue
        text = (_REPO_ROOT / rel).read_text(encoding="utf-8")
        hits = scan_python_text(rel, text)
        offenders.extend(verdict_for_path(rel, hits, _PYTHON_ALLOWLIST))
    assert not offenders, (
        "unallowlisted bare-python hit in a tracked Python file outside "
        "tests/ (fix the file, or allowlist it with its reason):\n"
        + "\n".join(offenders)
    )


# --- markdown, plugins/** strict lane ---------------------------------------

_MD_PLUGINS_ALLOWLIST: dict[str, AllowlistEntry] = {
    "plugins/awesome-kit/skills/orchestrate/SKILL.md": AllowlistEntry(
        "not `uv run python`, which resolves the",
        "anti-pattern warning prose, not an instruction"),
    "plugins/awesome-kit/skills/task/SKILL.md": AllowlistEntry(
        "Python explicitly -- not `uv run python`, which resolves the wrong environment",
        "anti-pattern warning prose + gold-standard-form description"),
    "plugins/bootstrap-stuck-fix/README.md": AllowlistEntry(
        "python plugins/bootstrap-stuck-fix/scripts/repair_registry.py --dry-run",
        "stuck-fix producer; provisions no venv by design, no plugin-venv "
        "path exists to reroute to (own README); scripts are stdlib-only"),
    "plugins/bootstrap/README.md": AllowlistEntry(
        "python directory for the Windows junction/mount-point problem",
        "prose (the standalone-python install directory), not an instruction"),
    "plugins/bootstrap/skills/bootstrap/SKILL.md": AllowlistEntry(
        "id: python_interpreter",
        "the python_interpreter fact: trigger keywords and warning prose that "
        "name the bare forms it forbids; its call sites are the guarded forms"),
    "plugins/bootstrap/skills/bootstrap/references/bootstrap-cli.md": AllowlistEntry(
        "## The `python` subcommand",
        "`bootstrap python` is the lever's subcommand name, not an interpreter"),
    "plugins/bootstrap/skills/bootstrap/references/engine-internals.md": AllowlistEntry(
        "never `uv run python`; see references/python-interpreter.md",
        "prose: the stub check's `python.exe` target, the forced-form note, "
        "and uv's python install directory"),
    "plugins/bootstrap/skills/bootstrap/references/python-interpreter.md": AllowlistEntry(
        "# Python interpreter variables",
        "the contract document: explains why bare `python`/`python3` fail and "
        "names the `bootstrap python` subcommand; every command it gives is a "
        "guarded form"),
    "plugins/bootstrap/skills/bootstrap/references/manifest-reference.md": AllowlistEntry(
        '"subdir": "python",',
        "manifest example value (a directory name) + prose describing "
        "python_stub_check, not an instruction"),
    "plugins/content-pipeline-kit/skills/execute-work-unit/SKILL.md": AllowlistEntry(
        "BEGIN ENUMERATED-INVOCATIONS",
        "illustrative example of a CONSUMER-supplied WorkerCommand between "
        "generator-owned do-not-edit-by-hand markers"),
    "plugins/p4-kit/skills/p4-code-review/SKILL.md": AllowlistEntry(
        "never as a bare path and never as bare `python`/`python3`",
        "generated launch-note WARNING prose (scripts/gen_code_review_skills.py); "
        "the launchers themselves are the guarded BOOTSTRAP_PYTHON form"),
    "plugins/skills-kit/skills/md-domain/references/audit-framework.md": AllowlistEntry(
        "Runs the **scaffolding** (`python -m skills_kit_lib.audit`",
        "prose describing the same documented dual-mode CLI"),
    "plugins/skills-kit/skills/md-domain/references/skill-domain/example-verification.md": AllowlistEntry(
        "A skill documented `uv run python` as the canonical way",
        "origin narrative of the host_python_via_plugin_venv insight, "
        "describing the WRONG pattern as a worked example"),
    "plugins/skills-kit/skills/md-domain/references/skill-domain/scripts.md": AllowlistEntry(
        "python -m skills_kit_lib.audit <path-to-SKILL.md>",
        "the authoritative dual-mode doc itself (bootstrap venv "
        "preferred; bare python degrades gracefully)"),
    "plugins/skills-kit/skills/md-domain/references/lanes/generation-lane.md": AllowlistEntry(
        "python -m skills_kit_lib.audit <path>",
        "same documented dual-mode CLI, table form"),
    "plugins/skills-kit/skills/md-domain/references/standards/skill-standards.md": AllowlistEntry(
        "python -m skills_kit_lib.audit",
        "same documented dual-mode CLI"),
    "plugins/skills-kit/skills_kit_lib/CLAUDE.md": AllowlistEntry(
        "invoked via `python -m skills_kit_lib.<module>`",
        "prose describing the same documented dual-mode CLI"),
    "plugins/unreal-kit/README.md": AllowlistEntry(
        "python ${CLAUDE_PLUGIN_ROOT}/scripts/refresh_unreal_stub.py --project-root .",
        "refresh_unreal_stub.py confirmed to call "
        "reexec_under_plugin_venv before any third-party import"),
    "plugins/unreal-kit/skills/fix-up-redirectors/SKILL.md": AllowlistEntry(
        "unless the cwd has a matching `pyproject.toml`",
        "gold-standard plugin-venv absolute path form + explicit warning"),
    "plugins/unreal-kit/skills/ue-python-api/SKILL.md": AllowlistEntry(
        "ue_runner.py re-execs itself under the plugin venv",
        "reexec documented explicitly; bare python is safe by construction"),
    "plugins/unreal-kit/skills/ue-python-api/references/architecture.md": AllowlistEntry(
        'py "C:/path/to/script.py"',
        "Unreal Editor's own embedded Python console command syntax, not "
        "a host-machine interpreter invocation"),
    "plugins/unreal-kit/skills/ue-python-api/references/bootstrapped-setup.md": AllowlistEntry(
        "Run `python ue_runner.py --setup`",
        "all three referenced scripts confirmed reexec-guarded"),
    "plugins/unreal-kit/skills/ue-python-api/references/project-setup.md": AllowlistEntry(
        "bRemoteExecution",
        "reexec-guarded scripts (ue_runner.py / search_unreal_stub.py)"),
    "plugins/unreal-kit/skills/ue-python-api/references/script-bootstrap.md": AllowlistEntry(
        "Execs `ue_runner.py` under the bootstrap-provisioned plugin venv",
        "prose explaining the reexec mechanism"),
    "plugins/unreal-kit/skills/ue-python-api/references/script-execution.md": AllowlistEntry(
        'py "C:/path/to/script.py"',
        "same UE console-command false positive as architecture.md"),
    "plugins/workflow-kit/skills/workflow-kit/SKILL.md": AllowlistEntry(
        "Plugin-venv interpreter (NOT `uv run python`, which resolves the venv from cwd",
        "gold-standard plugin-venv form + explicit anti-pattern warning"),
    "plugins/workflow-kit/skills/workflow-kit/references/workflow-yaml.md": AllowlistEntry(
        "plugin-venv interpreter (NOT `uv run python`",
        "gold-standard plugin-venv form + explicit anti-pattern warning"),
}


def test_tracked_plugin_markdown_bare_python_is_allowlisted_or_pending():
    """Every bare-python hit in a tracked plugins/** Markdown file is either
    allowlisted or a reported pending finding. This lane is STRICT: the
    maintainer uv_run bypass never applies (no plugins/** path matches its
    prefixes), so a real `uv run ... python` instruction under plugins/**
    is always a finding, per R3 mapping section 6 ("FINDING under
    plugins/** (shipped)").

    Revert proof: remove the unreal-kit/README.md allowlist entry and this
    goes red for its real reexec-guarded `python ...refresh_unreal_stub.py`
    hit."""
    offenders = []
    for rel in _git_ls_files("plugins/**/*.md"):
        text = (_REPO_ROOT / rel).read_text(encoding="utf-8")
        hits = scan_markdown_text(rel, text)
        offenders.extend(verdict_for_path(
            rel, hits, _MD_PLUGINS_ALLOWLIST,
            preload_lines=conforming_preload_lines(text)))
    assert not offenders, (
        "unallowlisted bare-python hit in a tracked plugins/** Markdown "
        "file (fix the file, or allowlist it with its reason):\n"
        + "\n".join(offenders)
    )


def test_tracked_skill_preloads_expand_only_claude_code_names():
    """No skill preload in plugins/** expands a shell variable. Claude Code
    rejects such a command before the skill renders, so a preload that says
    `"${BOOTSTRAP_PYTHON:?...}"` breaks the skill for every consumer. Not
    allowlistable: there is no preload for which the refusal is acceptable.

    Revert that turns this RED: put `"${BOOTSTRAP_PYTHON:?requires bootstrap
    >= 0.120.0}"` in place of `uv run --no-project python` in
    plugins/cache-kit/skills/cache-report/SKILL.md's preload line."""
    offenders = []
    for rel in _git_ls_files("plugins/**/*.md"):
        text = (_REPO_ROOT / rel).read_text(encoding="utf-8")
        offenders.extend(preload_expansion_findings(rel, text))
    assert not offenders, (
        "skill preload with a shell expansion Claude Code refuses (use "
        "`uv run --no-project python` and only Claude Code's substituted "
        "names):\n" + "\n".join(offenders)
    )


def test_preload_scanner_counterfactuals():
    """The preload rule in both directions, on injected text: a
    `uv run --no-project python` preload with substituted names conforms; a
    preload naming BOOTSTRAP_PYTHON (or any other shell variable) is a
    finding; inline code that merely shows `!` is not a preload; a fenced
    ```! block is scanned line by line."""
    good = ('!`uv run --no-project python "${CLAUDE_PLUGIN_ROOT}/s.py" '
            '--session "${CLAUDE_SESSION_ID}" $ARGUMENTS`\n')
    assert preload_expansion_findings("p/SKILL.md", good) == []
    assert conforming_preload_lines(good) == {1}
    hits = scan_markdown_text("p/SKILL.md", good)
    assert [h.kind for h in hits] == ["uv_run"]
    assert verdict_for_path("p/SKILL.md", hits, {}, preload_lines={1}) == []
    assert verdict_for_path("p/SKILL.md", hits, {}) != []

    bad = ('Report:\n'
           '!`"${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" '
           '"${CLAUDE_PLUGIN_ROOT}/s.py"`\n')
    assert preload_expansion_findings("p/SKILL.md", bad) == [
        "p/SKILL.md:2 preload expands ${BOOTSTRAP_PYTHON}"]
    assert conforming_preload_lines(bad) == set()

    fenced = '```!\nnode --version\necho "${HOME}"\n```\n'
    assert preload_expansion_findings("p/SKILL.md", fenced) == [
        "p/SKILL.md:3 preload expands ${HOME}"]

    not_preloads = ('the user must run it (prefix with `!`)\n'
                    'KEY=!`echo ${HOME}`\n'
                    '`echo "${HOME}"`\n')
    assert scan_skill_preloads(not_preloads) == []


# --- markdown, docs/root lenient lane ---------------------------------------

_MD_DOCS_ALLOWLIST: dict[str, AllowlistEntry] = {
    "CLAUDE.md": AllowlistEntry(
        "**Scoped exception: Python CLI launcher shims.**",
        "prose: the launcher-shim fallback chain it describes, warnings "
        "against `pip` / `python -m venv`, and the interpreter insight; its "
        "maintainer commands are `uv run` (variant (a)) and its engine "
        "command is the forced BOOTSTRAP_PYTHON form"),
    "CONTRIBUTING.md": AllowlistEntry(
        "Never run `pip`, `python -m venv`, or any package manager manually for a",
        "warning prose against manual install, not an instruction"),
    "docs/bootstrap/reference/case-studies/unreal-kit.md": AllowlistEntry(
        'satisfied_by="python ${CLAUDE_PLUGIN_ROOT}/scripts/refresh_unreal_stub.py',
        "case study reproduces real (reexec-guarded) source inside a code "
        "fence, not an instruction the reader executes"),
    "docs/historical/add-claude-driven-bootstrap-setup-pattern.md": AllowlistEntry(
        "python3 <PLUGIN_ROOT>/scripts/setup.py --check --data-dir <path>",
        "archived design doc; illustrative CLI for a setup.py that was "
        "never built, not a live instruction"),
    "docs/planning/awesome-kit-task-system/prior-systems-exploration.md": AllowlistEntry(
        "front-door",
        "prose describing a prior/inherited system's directory layout, "
        "not an instruction"),
    "docs/reference/plugin-opinion-razor.md": AllowlistEntry(
        "a bare `python`/`python3`/`py` command word in a shipped plugin manifest",
        "register row naming the bare forms the bootstrap manifest lint flags"),
    "docs/planning/bootstrap/MILESTONES.md": AllowlistEntry(
        "command -v python3",
        "prose describing detection-script behavior for a milestone record"),
}


def test_tracked_docs_root_markdown_bare_python_is_allowlisted_or_pending():
    """Every bare-python hit in a tracked docs/root Markdown file (excluding
    plugins/** and tests/**) is either the maintainer uv_run bypass,
    allowlisted, or a reported pending finding. This lane is LENIENT: a
    real `uv run ... python` maintainer instruction under docs/**, .md
    at repo root, scripts/**, or .githooks/** auto-conforms via
    MAINTAINER_UV_RUN_ALLOWED (variant (a)).

    Revert proof: remove the CONTRIBUTING.md allowlist entry and this goes
    red for its real warning-prose `python -m venv` hit."""
    offenders = []
    for rel in _git_ls_files("*.md"):
        if rel.startswith("plugins/") or rel.startswith("tests/"):
            continue
        text = (_REPO_ROOT / rel).read_text(encoding="utf-8")
        hits = scan_markdown_text(rel, text)
        offenders.extend(verdict_for_path(rel, hits, _MD_DOCS_ALLOWLIST))
    assert not offenders, (
        "unallowlisted bare-python hit in a tracked docs/root Markdown "
        "file (fix the file, or allowlist it with its reason):\n"
        + "\n".join(offenders)
    )


# --- allowlist staleness -----------------------------------------------------

def test_allowlists_are_not_stale():
    """Every allowlist path is still tracked, and every anchor is still a
    literal substring of that file's current text -- an allowlist entry
    whose file was renamed, deleted, or edited away from the reasoning it
    records is silently wrong, not silently safe.

    Revert proof: change any one anchor string above to text that is not in
    the file (e.g. typo a word in the hue-kit/bin/hue-kit anchor) and this
    goes red immediately."""
    all_tracked = set(_git_ls_files())
    offenders = []
    for allowlist in (
        _SHELL_ALLOWLIST, _PYTHON_ALLOWLIST,
        _MD_PLUGINS_ALLOWLIST, _MD_DOCS_ALLOWLIST,
    ):
        for path, entry in allowlist.items():
            if path not in all_tracked:
                offenders.append(f"{path}: not tracked by git ls-files")
                continue
            text = (_REPO_ROOT / path).read_text(encoding="utf-8")
            if entry.anchor not in text:
                offenders.append(f"{path}: anchor {entry.anchor!r} not found in file")
    assert not offenders, "stale allowlist entry:\n" + "\n".join(offenders)
