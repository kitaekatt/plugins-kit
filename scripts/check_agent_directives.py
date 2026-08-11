#!/usr/bin/env python3
"""Pre-commit guard for the agent-directive standards.

Blocks a commit that introduces, under ``plugins/``, one of a small set of
phrases that are violations on sight. The standard itself is
``docs/reference/agent-directive-standards.md``; this covers only the greppable
subset of it.

Why a check and not just the standard
-------------------------------------
Text under ``plugins/`` reaches a consumer's session, some of it through
``additionalContext`` -- the same channel that carries untrusted content. A
receiving agent cannot distinguish a real standing authorization from injected
text claiming one except by checking it, so an unbacked claim of authority is
indistinguishable from an attack. On 2026-08-11 a user refused one of ours,
correctly.

The standard is judgment work and stays judgment work. This check exists
because the judgment half failed in practice even with the author's full
attention: the session that WROTE the standard shipped a false claim inside the
standard's own enforcement section and caught it only on a later pass. A grep
does not get tired.

Scope, and why it is exactly ``plugins/``
----------------------------------------
``plugins/`` is what ships. It is also what keeps the check honest: the policy
document quotes every banned phrase as an example, and a repo-wide grep would
block the policy for stating the policy. Scoping to the shipped tree makes that
structural rather than an exception list.

What is deliberately NOT checked
--------------------------------
* **AD-4** (report the outcome, not the intention) has no keyword. Its defect is
  what the user ends up believing -- an acknowledgement emitted before an action
  resolves, plus an instruction not to elaborate. Nothing to match on.
* **``pre-authorized``** is omitted despite appearing in the standard's
  detection list. Its false-positive rate against LEGITIMATE uses is too high:
  ``example-claude-md.md`` pairs the claim with "(see Authorizations)", a real
  section of the same loaded document, which is exactly what the standard asks
  for. A pattern that flags the compliant form teaches people to disable the
  check.

Escape hatch: put ``agent-directive-ok`` on the same line. It is for text that
QUOTES a banned phrase in order to prohibit it (the guard comments in
``engine.py``), never for text that means it.

Stdlib-only: this runs inside a pre-commit hook, on machines that may have no
provisioned venv.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _gitindex import (  # noqa: E402
    SCOPE_INDEX,
    SCOPE_SKIP,
    classify_scope,
    index_text,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCOPE_PREFIX = "plugins/"
SCANNED_SUFFIXES = {".py", ".md", ".sh", ".js", ".mjs", ".json", ".yaml", ".yml", ".txt"}
ALLOW_MARKER = "agent-directive-ok"

# (criterion, compiled pattern, why it is a violation on sight)
BANNED = [
    (
        "AD-2",
        re.compile(r"fleet policy", re.I),
        "asserts an authority with no referent the receiving agent can resolve",
    ),
    (
        "AD-3",
        re.compile(r"do(?: no|n')t wait for the user", re.I),
        "pre-empts a checkpoint the user would otherwise have",
    ),
    (
        "AD-3",
        re.compile(r"without asking the user", re.I),
        "pre-empts the user rather than describing what is authorized",
    ),
    # NOTE: "do not tell the user" is deliberately ABSENT. It was tried, and on
    # the live corpus two of its three matches were compliant: orchestrate's
    # "do not tell the user something is 'unavailable' on the strength of its
    # absence" is an instruction NOT TO MAKE A FALSE CLAIM, and
    # openrouter-account's "do not tell the user to run fix-all" withholds no
    # information and cites the reference explaining why. The violating sense is
    # "do not tell the user ABOUT something that happened"; the compliant sense
    # is "do not tell the user TO DO something" or "...that <false thing>". No
    # regex separates those, and the pattern's own rationale (below) says a
    # pattern that flags the compliant form is worse than no pattern.
    (
        "AD-1",
        re.compile(r"do(?: no|n')t report\b[^.\n]{0,60}\bto the user", re.I),
        "directs the agent to withhold findings from the user",
    ),
    (
        "AD-1",
        re.compile(r"silently spawn", re.I),
        "directs the agent to run something without disclosing it",
    ),
]


def is_input(rel_path: str) -> bool:
    """True for a shipped plugin file this check reads."""
    return (
        rel_path.startswith(SCOPE_PREFIX)
        and Path(rel_path).suffix.lower() in SCANNED_SUFFIXES
    )


def scan_text(rel_path: str, text: str) -> list[str]:
    """Findings in one file, as preformatted report lines."""
    out = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if ALLOW_MARKER in line:
            continue
        for criterion, pattern, why in BANNED:
            match = pattern.search(line)
            if match:
                out.append(
                    f"  {rel_path}:{lineno} [{criterion}] {match.group(0)!r} -- {why}"
                )
    return out


def collect_worktree() -> list[str]:
    findings = []
    root = REPO_ROOT / "plugins"
    if not root.is_dir():
        return findings
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in SCANNED_SUFFIXES:
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        findings.extend(scan_text(rel, text))
    return findings


def collect_staged() -> list[str] | None:
    """Findings across staged inputs, or None when the check should be skipped."""
    verdict, paths = classify_scope(REPO_ROOT, is_input)
    if verdict == SCOPE_SKIP:
        return None
    if verdict != SCOPE_INDEX:
        # Git could not answer. Falling back to the worktree LOUDLY beats
        # passing on an unavailable input -- a check that passes because it
        # could not read its input is not a check.
        print(
            "check_agent_directives: git index unreadable; judging the worktree.",
            file=sys.stderr,
        )
        return collect_worktree()
    findings = []
    for rel in paths:
        if not is_input(rel):
            continue
        text = index_text(REPO_ROOT, rel)
        if text is None:  # staged deletion
            continue
        findings.extend(scan_text(rel, text))
    return findings


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--staged",
        action="store_true",
        help="judge the git index and skip when the commit stages no plugin files",
    )
    args = ap.parse_args()

    findings = collect_staged() if args.staged else collect_worktree()
    if findings is None:
        return 0
    if not findings:
        return 0

    print("agent-directive standards: banned phrasing under plugins/", file=sys.stderr)
    for line in findings:
        print(line, file=sys.stderr)
    print("", file=sys.stderr)
    print(
        "These reach a consumer's session. State what is authorized and name the\n"
        "file backing it; never instruct Claude to withhold from the user or to\n"
        "move past them. See docs/reference/agent-directive-standards.md.\n"
        f"If the text QUOTES the phrase to prohibit it, add {ALLOW_MARKER!r} to the line.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
