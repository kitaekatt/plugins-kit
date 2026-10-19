"""Activation check 1 of the llm-kit migration: the maintained-reference scan.

The signed design (docs/reference/llm-kit-architecture.md, "Skills, agents,
workflows, and CLIs") requires, before llm-kit's registrations are activated
for a consumer, that "source and manifest scans find no unsupported old
qualified reference". This module is that scan. The migration plan
(docs/planning/llm-kit/migration-plan.md, section 1 and increment M3) assigns
it the same job again at legacy deletion (M10, "the maintained-reference scan").

What counts as an old qualified reference (three shapes, one token each):

* a skill, agent or workflow registration: ``<old plugin>:<registered name>``,
  for the seven registered names of llm-kit and the three legacy plugins;
* a manifest plugin reference: ``plugins-kit:<old plugin>``;
* a marketplace-qualified plugin name, as in an install or update command:
  ``<old plugin>@plugins-kit``.

What is scanned: every file git knows (tracked or untracked-not-ignored),
except the exemptions in :data:`EXEMPT_PREFIXES`: the three frozen legacy
plugin trees and their test trees, llm-kit's parity and repoint material,
dated planning records, and generated index pages.

The allowlist of consumers that have not migrated yet
-----------------------------------------------------

:data:`ALLOWLIST` is the explicit, shrinking list of files that may still carry
an old qualified reference. Each entry names the migration increment that owns
the file and the exact set of distinct tokens it may carry. The scan fails when

* a file outside the allowlist carries a token (an unsupported reference);
* an allowlisted file carries a token its entry does not list, or an entry
  lists a token the file no longer carries (a stale entry).

So a consumer increment (M4-M9) finishes by deleting its entries here: leaving
one behind fails the "stale" check, and migrating without deleting is
impossible. Adding an entry is the only way to ship a new old reference, and it
is a reviewed change to this file. The allowlist is empty when the last
consumer has migrated (M10 also deletes the three legacy trees whose own
references the scan exempts).

The imports of the old Python packages and the old shared-library names in
``shared_lib_imports`` are not qualified references and are not scanned here;
the deletion scan in the plan's section 7 covers those and extends this module.
"""

from __future__ import annotations

import re
import subprocess
from collections import Counter
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, NamedTuple

_REPO_ROOT = Path(__file__).resolve().parents[2]
_THIS_FILE = "tests/repo-scripts/test_maintained_references.py"

# The three legacy plugins and the registered names, spelled as fragments so
# that this file does not match its own patterns.
OLD_PLUGINS = ("llm-scripting-" + "kit", "content-pipeline-" + "kit", "job-" + "kit")
MARKETPLACE = "plugins-" + "kit"
#: The registered skills, agent and workflow of the final llm-kit.
REGISTERED_NAMES = (
    "openrouter-account",
    "content-pipeline-domain",
    "background-pipeline",
    "execute-work-unit",
    "workflow-pipeline",
    "pipeline-worker",
    "run-ready-wave",
)

_BOUND_L = r"(?<![\w-])"
_BOUND_R = r"(?![\w-])"
_OLD = "|".join(re.escape(p) for p in OLD_PLUGINS)
_NAMES = "|".join(re.escape(n) for n in REGISTERED_NAMES)
PATTERNS = (
    re.compile(f"{_BOUND_L}(?:{_OLD}):(?:{_NAMES}){_BOUND_R}"),
    re.compile(f"{_BOUND_L}{re.escape(MARKETPLACE)}:(?:{_OLD}){_BOUND_R}"),
    re.compile(f"{_BOUND_L}(?:{_OLD})@{re.escape(MARKETPLACE)}{_BOUND_R}"),
)

#: Paths under these prefixes are not scanned. Each is a reason, not a hole.
EXEMPT_PREFIXES: Dict[str, str] = {
    **{f"plugins/{p}/": "frozen legacy plugin tree" for p in OLD_PLUGINS},
    **{f"tests/{p}/": "frozen legacy plugin test tree" for p in OLD_PLUGINS},
    "tests/llm-kit/": "llm-kit parity, repoint and differential material",
    "docs/planning/": "dated planning records",
    ".databench/": "generated index pages",
}

# Migration increments (docs/planning/llm-kit/migration-plan.md, section 8).
OWNERS = frozenset({"M4", "M5", "M6", "M7", "M8", "M9", "M10"})


class Entry(NamedTuple):
    owner: str
    tokens: frozenset
    reason: str


def _e(owner: str, *tokens: str, reason: str) -> Entry:
    return Entry(owner, frozenset(tokens), reason)


_LSK_INSTALL = "llm-scripting-" + "kit@plugins-kit"
_LSK_REF = "plugins-kit:llm-scripting-" + "kit"

_M10 = (
    "bootstrap standing documentation and a compatibility fixture; not a consumer "
    "migration, so legacy deletion (M10) rewrites them unless an earlier "
    "increment already has"
)

#: Files that may still carry an old qualified reference. Shrinks to empty.
ALLOWLIST: Dict[str, Entry] = {
    # M10: bootstrap standing documents and a compatibility fixture
    "plugins/bootstrap/skills/bootstrap/references/action-triggered-install.md": _e(
        "M10", _LSK_INSTALL, reason=_M10
    ),
    "plugins/bootstrap/skills/plugin-dev/references/interrupt-contract.md": _e(
        "M10", _LSK_INSTALL, reason=_M10
    ),
    "tests/bootstrap/fixtures/spiritcrossing_tiered_bootstrap.json": _e(
        "M10", _LSK_REF, reason=_M10
    ),
}


# --- the scanner -----------------------------------------------------------

def find_tokens(text: str) -> Counter:
    """Every old qualified reference in ``text``, counted by exact token."""
    found: Counter = Counter()
    for pattern in PATTERNS:
        for match in pattern.finditer(text):
            found[match.group(0)] += 1
    return found


def exemption_for(path: str) -> str | None:
    for prefix, reason in EXEMPT_PREFIXES.items():
        if path.startswith(prefix):
            return reason
    if path == _THIS_FILE:
        return "this scanner names the patterns it scans for"
    return None


def evaluate(files: Mapping[str, str], allowlist: Mapping[str, Entry]) -> List[str]:
    """The problems found in ``files`` (path -> text) against ``allowlist``."""
    problems: List[str] = []
    scanned = {p: find_tokens(t) for p, t in files.items() if exemption_for(p) is None}
    for path in sorted(scanned):
        tokens = scanned[path]
        entry = allowlist.get(path)
        if entry is None:
            for token in sorted(tokens):
                problems.append(f"{path}: unsupported old qualified reference {token!r}")
        elif tokens and set(tokens) != set(entry.tokens):
            extra = sorted(set(tokens) - set(entry.tokens))
            gone = sorted(set(entry.tokens) - set(tokens))
            if extra:
                problems.append(f"{path}: reference not in its allowlist entry: {extra}")
            if gone:
                problems.append(
                    f"{path}: stale allowlist entry ({entry.owner}) lists {gone}; "
                    "delete the token"
                )
    for path, entry in sorted(allowlist.items()):
        if path not in files:
            problems.append(f"{path}: stale allowlist entry ({entry.owner}): file is absent")
        elif exemption_for(path) is not None:
            problems.append(f"{path}: allowlist entry for an exempt path ({exemption_for(path)})")
        elif not scanned[path]:
            problems.append(
                f"{path}: stale allowlist entry ({entry.owner}): no old reference remains; "
                "delete the entry"
            )
    return problems


def repository_files(root: Path = _REPO_ROOT) -> Dict[str, str]:
    """Path -> text of every non-ignored file git knows, decodable as UTF-8."""
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        check=True,
        capture_output=True,
    ).stdout
    files: Dict[str, str] = {}
    for raw in out.split(b"\0"):
        if not raw:
            continue
        path = raw.decode("utf-8")
        full = root / path
        if not full.is_file():
            continue  # a tracked path deleted in the working tree
        try:
            files[path] = full.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue  # binary
    return files


# --- the guard over the real tree -------------------------------------------

def test_no_unsupported_old_qualified_reference() -> None:
    problems = evaluate(repository_files(), ALLOWLIST)
    assert not problems, "old qualified references in maintained code:\n" + "\n".join(problems)


def test_allowlist_owners_are_migration_increments() -> None:
    bad = {p: e.owner for p, e in ALLOWLIST.items() if e.owner not in OWNERS}
    assert not bad, f"allowlist entries must name a migration increment M4-M10: {bad}"


def test_registered_names_match_the_final_llm_kit_registrations() -> None:
    """The seven scanned names are exactly llm-kit's registrations.

    A registration added to or removed from llm-kit without updating
    REGISTERED_NAMES would let its old qualified spelling go unscanned.
    """
    plugin = _REPO_ROOT / "plugins" / "llm-kit"
    registered = {p.name for p in (plugin / "skills").iterdir() if (p / "SKILL.md").is_file()}
    registered |= {p.stem for p in (plugin / "agents").glob("*.md")}
    registered |= {p.stem for p in (plugin / "workflows").glob("*.js")}
    assert registered == set(REGISTERED_NAMES)


# --- each detector is shown to fail (planted input) and to pass again -------

PLANTS = {
    "skill": "Invoke " + "content-pipeline-" + "kit:execute-work-unit now",
    "agent flag": "claude --bg --agent " + "content-pipeline-" + "kit:pipeline-worker -- go",
    "workflow": "name: \"" + "content-pipeline-" + "kit:run-ready-wave\"",
    "manifest ref": '"ref": "' + "plugins-" + 'kit:job-kit"',
    "install command": "claude plugin update " + "llm-scripting-" + "kit@plugins-kit",
}


def test_each_pattern_can_fail() -> None:
    for label, planted in PLANTS.items():
        assert find_tokens(planted), f"the scan does not see a planted {label} reference"


def test_final_names_and_lookalikes_are_not_flagged() -> None:
    clean = (
        "llm-kit:execute-work-unit plugins-kit:llm-kit llm-kit@plugins-kit\n"
        "content-pipeline-kit:run-created job-kit:run-created\n"  # persisted event ids
        "plugins-kit:job-kit-extra my-job-kit:pipeline-worker\n"
    )
    assert not find_tokens(clean)


def test_planted_reference_fails_then_passes_when_restored() -> None:
    files = {"plugins/example-kit/skill.md": "no old reference here"}
    assert not evaluate(files, {})
    files["plugins/example-kit/skill.md"] = PLANTS["workflow"]
    problems = evaluate(files, {})
    assert len(problems) == 1 and "unsupported old qualified reference" in problems[0]
    files["plugins/example-kit/skill.md"] = "no old reference here"
    assert not evaluate(files, {})


def test_allowlist_must_shrink_and_cannot_grow_silently() -> None:
    path = "plugins/example-kit/diag.py"
    token = next(iter(find_tokens(PLANTS["install command"])))
    allow = {path: Entry("M4", frozenset({token}), "example")}
    assert not evaluate({path: PLANTS["install command"]}, allow)
    # the consumer migrated but left its entry behind
    stale = evaluate({path: "migrated"}, allow)
    assert len(stale) == 1 and "stale allowlist entry" in stale[0]
    # the entry's file is gone
    assert "file is absent" in evaluate({}, allow)[0]
    # a new kind of old reference appears in an allowlisted file
    grown = evaluate({path: PLANTS["install command"] + "\n" + PLANTS["manifest ref"]}, allow)
    assert len(grown) == 1 and "not in its allowlist entry" in grown[0]


def test_exempt_trees_are_not_scanned() -> None:
    for prefix in EXEMPT_PREFIXES:
        assert not evaluate({prefix + "x.md": PLANTS["skill"]}, {}), prefix
    assert not evaluate({_THIS_FILE: PLANTS["skill"]}, {})


def test_scan_covers_tests_and_manifests_of_consumers() -> None:
    for path in ("tests/workflow-kit/test_x.py", "plugins/workflow-kit/bootstrap.json"):
        assert evaluate({path: PLANTS["manifest ref"]}, {}), path
