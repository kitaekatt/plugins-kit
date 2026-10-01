"""Durable guard: no BARE ``llm-scripting-kit`` command in a shipped surface.

Claude Code puts each enabled plugin's VERSION-KEYED cache ``bin/`` directory on
the PATH of its own Bash sessions, and nothing else does. A bare
``llm-scripting-kit`` therefore resolves inside a Claude Code session and fails
in PowerShell, Codex, a Git Bash terminal, and every other shell -- which is how
a Codex-side ``git-code-review`` on Windows came to run one and die. Nothing
persists the PATH entry, so the fix is not a PATH change: a shipped surface names
the version-free plugin-venv console script instead, the resolution contract

    ~/.claude/plugins/data/plugins-kit/llm-scripting-kit/.venv/bin/llm-scripting-kit
    (Windows: ~/.claude/plugins/data/plugins-kit/llm-scripting-kit/.venv/Scripts/llm-scripting-kit.exe)

whose single source is ``CLI_COMMAND`` in
``plugins/llm-scripting-kit/lib/llm_scripting_kit/constants.py``.

This module is the structural sibling of ``test_python_invocation_standard.py``:
a pure text scanner (:func:`scan_text`), a verdict layer
(:func:`verdict_for_path`), and anchored exemptions whose anchor text must still
be present in the file it exempts (:func:`test_exemptions_are_not_stale`).

COMMAND POSITION, as implemented. A hit is an ``llm-scripting-kit`` token that
is not part of a longer path or identifier AND sits in one of five positions: the
first word on a line (inside a Markdown fenced code block whose language is not
a pure data language, or anywhere in a ``.yaml`` / ``.json`` file); immediately
after a ``$ `` or ``> `` shell prompt; immediately after a shell operator
(``|``, ``;``, ``&&``, ``||``) inside such a code block; immediately after an
opening quote (backtick, ``"``, ``'``) and NOT immediately followed by that same
closing quote; or as the value of a YAML ``command:`` / ``run:`` / ``cmd:`` /
``entrypoint:`` / ``args:`` key. A token preceded by ``/`` or ``\\`` is already
resolved and is never a hit, which is what makes every contract-prefixed
invocation (``.venv/bin/`` or ``.venv/Scripts/``) conforming by construction.
``llm_scripting_kit`` with an UNDERSCORE is the Python module and never matches.

Two deliberate narrowings, both of which exist to keep the guard's findings
real rather than to make it green:

* A quoted span holding ONLY the name (``the `llm-scripting-kit` plugin``) is
  not an invocation -- there is no subcommand to run -- so the closing-quote
  test above excludes it. A span whose name is followed by anything, or which
  runs to end of line, is a hit.
* Line-start inside a ``yaml``/``yml``/``json``/``toml`` fence is NOT command
  position. Several SKILL.md files wrap their whole body in one ```` ```yaml ````
  fence, so inside it a line start is a YAML position, not a shell one, and
  wrapped prose ("... come from\\nllm-scripting-kit and govern the choice.")
  would otherwise register as a command. Real commands in those files reach the
  scanner through the quoted and ``command:``-value positions instead.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import NamedTuple

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: The CLI's command name, and the two contract prefixes that resolve it.
CLI_NAME = "llm-scripting-kit"
CONTRACT_PREFIXES = (".venv/bin/", ".venv/Scripts/")

# A bare CLI token, not part of a longer path or identifier:
#   - not preceded by an identifier or path character, so
#     "<...>/.venv/bin/llm-scripting-kit", ".venv\\Scripts\\llm-scripting-kit",
#     and "plugins/llm-scripting-kit/README.md" never match;
#   - not followed by an identifier, path, dot, or colon character, so
#     "llm-scripting-kit.json-schema-subset/v1" (a frozen subset literal),
#     "llm_scripting_kit" (the Python module, which also fails the literal), and
#     a YAML key "llm-scripting-kit:" never match.
_NOT_BEFORE = r"(?<![A-Za-z0-9_./\\-])"
_NOT_AFTER = r"(?![A-Za-z0-9_.:-])"
_TOKEN_RE = re.compile(_NOT_BEFORE + re.escape(CLI_NAME) + _NOT_AFTER)

# Markdown fence open/close, with its language tag.
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})\s*([A-Za-z0-9_+-]*)\s*$")
#: Fence languages whose line starts are DATA positions, not command positions.
_DATA_FENCE_LANGS = frozenset({"yaml", "yml", "json", "toml"})

# Everything that may precede the first WORD of a line: indentation, a Markdown
# list bullet, a blockquote marker.
_LINE_LEAD_RE = re.compile(r"^\s*(?:(?:[-*+]|\d+[.)])\s+|>\s+)*")
# A shell prompt (`$ ` / `> `) immediately before the token.
_PROMPT_RE = re.compile(r"(?:^|\s)[$>]\s+$")
# A shell operator immediately before the token.
_OPERATOR_RE = re.compile(r"(?:\|\||&&|[|;])\s+$")
# A YAML command-valued key immediately before the token.
_YAML_COMMAND_RE = re.compile(
    r"^\s*-?\s*(?:command|run|cmd|entrypoint|args)\s*:\s*[\"']?$"
)
_QUOTES = "`\"'"

#: Markdown extensions get fence tracking; the data extensions do not.
_MARKDOWN_SUFFIX = ".md"


class Hit(NamedTuple):
    line: int
    position: str  # line-start | prompt | operator | quoted | yaml-command
    text: str


def scan_text(path: str, text: str) -> list[Hit]:
    """Every bare command-position ``llm-scripting-kit`` hit in one file's text.

    ``path`` selects the Markdown fence-tracking behaviour described in the
    module docstring; it is otherwise unused, and the function touches no disk.
    """
    hits: list[Hit] = []
    is_markdown = path.endswith(_MARKDOWN_SUFFIX)
    in_fence = False
    marker = ""
    lang = ""
    for lineno, line in enumerate(text.splitlines(), start=1):
        if is_markdown:
            fence = _FENCE_RE.match(line)
            if fence:
                if not in_fence:
                    in_fence, marker, lang = True, fence.group(1)[0], fence.group(2).lower()
                    continue
                if fence.group(1)[0] == marker and not fence.group(2):
                    in_fence, marker, lang = False, "", ""
                    continue
        code_context = (not is_markdown) or (in_fence and lang not in _DATA_FENCE_LANGS)
        for match in _TOKEN_RE.finditer(line):
            before, after = line[: match.start()], line[match.end():]
            position = _position_for(before, after, code_context)
            if position:
                hits.append(Hit(lineno, position, line.strip()))
    return hits


def _position_for(before: str, after: str, code_context: bool) -> str | None:
    """The command position this token occupies, or None when it occupies none."""
    if before and before[-1] in _QUOTES and not after.startswith(before[-1]):
        # An opening quote whose span does not close immediately after the name:
        # `llm-scripting-kit describe`, "llm-scripting-kit set-key", or a span
        # continued on the next line. A span holding only the name is prose.
        return "quoted"
    if _PROMPT_RE.search(before):
        return "prompt"
    if _YAML_COMMAND_RE.match(before):
        return "yaml-command"
    if code_context and _OPERATOR_RE.search(before):
        return "operator"
    if code_context and _LINE_LEAD_RE.fullmatch(before):
        return "line-start"
    return None


# --- git helpers (tests only; the scanner above stays pure / disk-free) -----

def _git_ls_files(*patterns: str) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(_REPO_ROOT), "ls-files", *patterns],
        capture_output=True, text=True, check=True,
    )
    return sorted({line for line in result.stdout.splitlines() if line})


#: The lane: tracked shipped-surface text under plugins/.
_LANE_PATTERNS = (
    "plugins/*.md", "plugins/**/*.md",
    "plugins/*.yaml", "plugins/**/*.yaml",
    "plugins/*.json", "plugins/**/*.json",
)


def lane_key(path: str) -> str:
    """The plugin a lane finding belongs to, so a failure names what regressed."""
    parts = path.split("/")
    if len(parts) > 2:
        return parts[1]
    return "plugins (repo-level)"


# ===========================================================================
# Exemptions. Two mechanisms, both ANCHORED: the anchor is literal text that
# must still be present in the file (test_exemptions_are_not_stale), so an
# edit that removes the reasoning cannot leave the exemption silently standing.
#
#  1. _SNIPPET_EXEMPTIONS -- the NARROW form, and the default choice. A hit
#     conforms only when its own line contains the snippet. The rest of the
#     file stays covered, which is what a file holding both adjudicated prose
#     AND real migrated call sites needs: git-kit's and p4-kit's review skills
#     each carry one descriptive checklist line plus several real invocations
#     that this guard exists to protect, so a whole-file entry there would
#     blind the guard to exactly the sites it was built for. A snippet rather
#     than a line NUMBER because prose above it moves.
#
#  2. _WHOLE_FILE_EXEMPTIONS -- for a document whose EVERY bare mention is
#     covered by one in-document statement: README.md and the
#     openrouter-account skill both say in so many words that
#     `llm-scripting-kit` in their examples STANDS FOR the contract path, and
#     llm-scripting-kit's own CLAUDE.md points at that statement. The
#     shorthand is the exemption's reason, and the anchor is the sentence that
#     establishes it.
# ===========================================================================

class AllowlistEntry(NamedTuple):
    anchor: str
    reason: str


_SHORTHAND_REASON = (
    "the document states that a bare `llm-scripting-kit` in its examples "
    "stands for the contract path (anchor), so every example in it is "
    "shorthand for that path rather than an instruction to run a bare name"
)

_WHOLE_FILE_EXEMPTIONS: dict[str, AllowlistEntry] = {
    "plugins/CLAUDE.md": AllowlistEntry(
        "rank each reviewer declaration through the `llm-scripting-kit describe` CLI",
        "repo-level maintainer guidance; both hits are descriptive prose naming "
        "the mechanism a skill uses, not an invocation a reader runs",
    ),
    "plugins/llm-scripting-kit/README.md": AllowlistEntry(
        "In the examples below, `llm-scripting-kit`",
        _SHORTHAND_REASON + "; the file also discusses the PATH situation itself, "
        "which requires naming the bare form",
    ),
    "plugins/llm-scripting-kit/skills/openrouter-account/SKILL.md": AllowlistEntry(
        "The examples below write `llm-scripting-kit` for that path:",
        _SHORTHAND_REASON,
    ),
    "plugins/llm-scripting-kit/CLAUDE.md": AllowlistEntry(
        "(run it by the CLI contract path",
        "the plugin's own maintainer guidance, naming its CLI groups (`swapper`, "
        "`acceptance`, `frontdoor`) in prose and pointing at README.md's "
        "\"Invoking the CLI\" for the path (anchor)",
    ),
}


class SnippetExemption(NamedTuple):
    snippet: str
    reason: str


_SNIPPET_EXEMPTIONS: dict[str, tuple[SnippetExemption, ...]] = {
    "plugins/git-kit/skills/git-code-review/SKILL.md": (
        SnippetExemption(
            "routed through `llm-scripting-kit describe` (stdout printed verbatim",
            "expected-output checklist prose, not a run-this instruction; narrow "
            "on purpose -- this file's real call sites must stay covered",
        ),
    ),
    "plugins/p4-kit/skills/p4-code-review/SKILL.md": (
        SnippetExemption(
            "routed through `llm-scripting-kit describe` (stdout printed verbatim",
            "expected-output checklist prose, not a run-this instruction; narrow "
            "on purpose -- this file's real call sites must stay covered",
        ),
    ),
    "plugins/content-pipeline-kit/skills/content-pipeline-domain/references/"
    "building-a-pipeline.md": (
        SnippetExemption(
            "`llm-scripting-kit resolve --models <entry>` reports the style under",
            "adjudicated descriptive: the sentence's subject is the "
            "`effort_delivery` field, with the command named as what reports it",
        ),
    ),
    "plugins/bootstrap/skills/bootstrap/references/deferred-requirements.md": (
        SnippetExemption(
            'satisfied_by="llm-scripting-kit set-key"',
            "illustrative documentation of the field's SHAPE (the Python call); "
            "the real emitted value lives in llm-scripting-kit's code and uses "
            "CLI_COMMAND",
        ),
        SnippetExemption(
            '"satisfied_by": "llm-scripting-kit set-key"',
            "the same field shape in the emitted-JSON example below it",
        ),
    ),
}


def verdict_for_path(
    path: str,
    hits: list[Hit],
    whole_file: dict[str, AllowlistEntry] | None = None,
    snippets: dict[str, tuple[SnippetExemption, ...]] | None = None,
) -> list[str]:
    """Classify each hit in one tracked path as conforming (whole-file
    exemption, or a snippet exemption whose snippet is on the hit's own line)
    or an offender. Returns offender description strings."""
    whole_file = _WHOLE_FILE_EXEMPTIONS if whole_file is None else whole_file
    snippets = _SNIPPET_EXEMPTIONS if snippets is None else snippets
    if path in whole_file:
        return []
    exempt_snippets = snippets.get(path, ())
    offenders = []
    for hit in hits:
        if any(entry.snippet in hit.text for entry in exempt_snippets):
            continue
        offenders.append(f"{path}:{hit.line} [{hit.position}] {hit.text[:120]}")
    return offenders


def lane_offenders() -> dict[str, list[str]]:
    """Every offender in the shipped-surface lane, grouped by plugin."""
    grouped: dict[str, list[str]] = {}
    for rel in _git_ls_files(*_LANE_PATTERNS):
        text = (_REPO_ROOT / rel).read_text(encoding="utf-8")
        offenders = verdict_for_path(rel, scan_text(rel, text))
        if offenders:
            grouped.setdefault(lane_key(rel), []).extend(offenders)
    return grouped


# --- the real-file lane -----------------------------------------------------

def test_tracked_plugin_surfaces_have_no_bare_llm_scripting_kit_command():
    """Every command-position `llm-scripting-kit` token in a tracked
    plugins/** .md / .yaml / .json file is either contract-prefixed (never a
    hit) or covered by an anchored exemption stating why it is prose.

    Shown to fail: restoring the bare `llm-scripting-kit describe <entry>...
    --caller session` at plugins/git-kit/skills/git-code-review/SKILL.md:165
    turns this red naming the git-kit lane (the snippet exemption in that file
    covers only the checklist line at ~492, so the call site is still read).
    """
    grouped = lane_offenders()
    report = "\n".join(
        f"  {plugin}:\n" + "\n".join(f"    {o}" for o in sorted(offenders))
        for plugin, offenders in sorted(grouped.items())
    )
    assert not grouped, (
        "bare `llm-scripting-kit` in command position in a shipped surface -- "
        "use the contract path "
        "~/.claude/plugins/data/plugins-kit/llm-scripting-kit/.venv/bin/"
        "llm-scripting-kit (Windows: .venv/Scripts/llm-scripting-kit.exe), the "
        "string CLI_COMMAND in llm_scripting_kit/constants.py defines; exempt a "
        "descriptive mention only with an anchored reason:\n" + report
    )


# --- scanner counterfactuals (injected text, no tracked file touched) -------

def test_scan_text_catches_every_command_position():
    """Prove the scanner detects each of the five command positions. The
    real-file test above asserts "zero offenders", which a scanner that never
    matched anything would also satisfy -- this is the counterfactual it needs.

    Shown to fail: drop any one branch from `_position_for` and the
    corresponding entry disappears from this list."""
    text = (
        "```bash\n"
        "llm-scripting-kit describe fable\n"
        "$ llm-scripting-kit usage\n"
        "printf x | llm-scripting-kit complete --models sol\n"
        "```\n"
        "Run `llm-scripting-kit set-key` to store it.\n"
        "command: llm-scripting-kit frontdoor\n"
    )
    assert [(h.line, h.position) for h in scan_text("plugins/x/README.md", text)] == [
        (2, "line-start"),
        (3, "prompt"),
        (4, "operator"),
        (6, "quoted"),
        (7, "yaml-command"),
    ]


def test_scan_text_leaves_the_contract_path_and_the_module_alone():
    """The conforming forms must never register as a hit, on either platform,
    and neither must the Python module or a plugin-relative path.

    Shown to fail: replace the first line below with a bare
    `llm-scripting-kit resolve` and the assertion goes red -- it is not a
    vacuously-true "== []" check."""
    conforming = (
        "```bash\n"
        "~/.claude/plugins/data/plugins-kit/llm-scripting-kit/.venv/bin/"
        "llm-scripting-kit describe fable\n"
        "~/.claude/plugins/data/plugins-kit/llm-scripting-kit/.venv/Scripts/"
        "llm-scripting-kit.exe describe fable\n"
        'C:\\Users\\x\\.claude\\plugins\\data\\plugins-kit\\llm-scripting-kit\\'
        '.venv\\Scripts\\llm-scripting-kit.exe usage\n'
        "```\n"
        "The `llm_scripting_kit` package ships `plugins/llm-scripting-kit/`.\n"
        "Its subset literal is `llm-scripting-kit.json-schema-subset/v1`.\n"
        "The `llm-scripting-kit` plugin is installed; llm-scripting-kit 0.46.0\n"
        "or later is required.\n"
    )
    assert scan_text("plugins/x/README.md", conforming) == []
    for prefix in CONTRACT_PREFIXES:
        assert prefix in conforming


def test_line_start_inside_a_data_fence_is_not_command_position():
    """A SKILL.md body wrapped in one ```` ```yaml ```` fence makes wrapped
    prose start lines; those are not commands, while a `command:` value and a
    quoted invocation inside the same fence still are.

    Shown to fail: delete `_DATA_FENCE_LANGS` from the `code_context` test and
    the first (prose) line re-appears as a spurious line-start hit."""
    text = (
        "```yaml\n"
        "    detail: >-\n"
        "      the `Rule:` line and any `Independence:` line come from\n"
        "      llm-scripting-kit and govern the choice. When a dispatch fails,\n"
        "      run `llm-scripting-kit describe <entry>...` again.\n"
        "```\n"
    )
    assert [(h.line, h.position) for h in scan_text("plugins/x/SKILL.md", text)] == [
        (5, "quoted"),
    ]


def test_a_quoted_span_holding_only_the_name_is_not_a_command():
    """`the `llm-scripting-kit` plugin` names the plugin; there is no
    subcommand to run. A span whose name is followed by anything is a command.

    Shown to fail: drop the `not after.startswith(...)` clause in
    `_position_for` and the first two lines become hits."""
    text = (
        "When `llm-scripting-kit` is not installed, nothing runs.\n"
        'The registry key is "llm-scripting-kit" in that file.\n'
        "The lane re-runs `llm-scripting-kit describe` with one `--exclude`.\n"
    )
    assert [(h.line, h.position) for h in scan_text("plugins/x/ref.md", text)] == [
        (3, "quoted"),
    ]


def test_verdict_layer_applies_whole_file_and_snippet_exemptions():
    """A whole-file exemption clears every hit in its file; a snippet
    exemption clears only a hit on a line containing the snippet, leaving the
    rest of that file covered -- the property that makes the narrow mechanism
    worth having for git-kit's and p4-kit's review skills.

    Shown to fail: make `verdict_for_path` return [] for any path present in
    `snippets` and the second assertion goes green-with-nothing-covered."""
    hits = [
        Hit(10, "quoted", "routed through `llm-scripting-kit describe` (checklist)"),
        Hit(20, "line-start", "llm-scripting-kit describe <entry> --caller session"),
    ]
    whole = {"p/SKILL.md": AllowlistEntry("anchor", "reason")}
    snips = {"p/SKILL.md": (SnippetExemption("(checklist)", "prose"),)}
    assert verdict_for_path("p/SKILL.md", hits, whole, {}) == []
    assert verdict_for_path("p/SKILL.md", hits, {}, snips) == [
        "p/SKILL.md:20 [line-start] llm-scripting-kit describe <entry> --caller session"
    ]
    assert len(verdict_for_path("p/SKILL.md", hits, {}, {})) == 2


def test_lane_key_names_the_plugin_that_regressed():
    assert lane_key("plugins/git-kit/skills/git-code-review/SKILL.md") == "git-kit"
    assert lane_key("plugins/CLAUDE.md") == "plugins (repo-level)"


# --- exemption staleness ----------------------------------------------------

def test_exemptions_are_not_stale():
    """Every exempted path is still tracked, every whole-file anchor is still
    literal text in that file, and every snippet still matches at least one
    hit the scanner finds there -- an exemption whose file moved past the
    reasoning it records is silently wrong, not silently safe.

    Shown to fail: typo a word in any anchor or snippet above, or delete the
    hit a snippet covers, and this goes red immediately."""
    tracked = set(_git_ls_files())
    offenders = []
    for path, entry in _WHOLE_FILE_EXEMPTIONS.items():
        if path not in tracked:
            offenders.append(f"{path}: not tracked by git ls-files")
            continue
        text = (_REPO_ROOT / path).read_text(encoding="utf-8")
        if entry.anchor not in text:
            offenders.append(f"{path}: anchor {entry.anchor!r} not found in file")
    for path, entries in _SNIPPET_EXEMPTIONS.items():
        if path not in tracked:
            offenders.append(f"{path}: not tracked by git ls-files")
            continue
        text = (_REPO_ROOT / path).read_text(encoding="utf-8")
        hits = scan_text(path, text)
        for entry in entries:
            if entry.snippet not in text:
                offenders.append(f"{path}: snippet {entry.snippet!r} not found in file")
            elif not any(entry.snippet in hit.text for hit in hits):
                offenders.append(
                    f"{path}: snippet {entry.snippet!r} covers no hit any more "
                    "(the exemption is unnecessary -- remove it)"
                )
    assert not offenders, "stale exemption:\n" + "\n".join(offenders)
