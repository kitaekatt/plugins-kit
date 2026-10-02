"""Durable guard: a plugin-root variable must be spelled for the surface it
sits on -- the harness variable where Claude Code substitutes it, the bootstrap
variable everywhere an agent's own shell resolves it.

THREE SURFACES substitute `${CLAUDE_PLUGIN_ROOT}` and `${CLAUDE_SKILL_DIR}`
before any command runs, per code.claude.com/docs/en/skills, "Available string
substitutions": (1) a `hooks/hooks.json` `command:` field (worked example:
`plugins/bootstrap/hooks/hooks.json`, whose SessionStart entry launches the only
writer of `BOOTSTRAP_PYTHON`); (2) a skill's `!` preload (the substitution set
is pinned as `CLAUDE_SKILL_SUBSTITUTIONS` in
`tests/repo-scripts/test_python_invocation_standard.py`); (3) a plugin skill's
own `SKILL.md` -- its markdown body and the Bash rules in its `allowed-tools`
frontmatter. Everywhere else -- references/*.md, READMEs, CLAUDE.md, scripts,
and the Bash tool's environment -- neither variable is substituted or set, so
`${CLAUDE_PLUGIN_ROOT}/scripts/x.py` degrades silently to `/scripts/x.py`. A
command an agent copies out of such a file names the plugin root through
`"${<PLUGIN>_ROOT:?<msg>}"` instead (`plugin_root_env_var_name` in
`plugins/bootstrap/bootstrap_lib/env_var_check.py`; exported each un-skipped
pass by `export_env_var` in `plugins/bootstrap/bootstrap_lib/engine.py`).

THE SURFACE-AWARE RULE this file enforces over tracked plugin files:
  - In a `plugins/*/skills/**/SKILL.md` (body and frontmatter),
    command-position `${CLAUDE_PLUGIN_ROOT}` and `${CLAUDE_SKILL_DIR}` are
    ALLOWED, and a command-position bootstrap name (`${<PLUGIN>_ROOT...`, the
    names `plugin_root_env_var_name` yields for the plugin directories present)
    is FLAGGED: it is a value the harness never substitutes in a skill body, so
    the harness variable is the form that works there.
  - In every other tracked plugin file, command-position
    `${CLAUDE_PLUGIN_ROOT}` and `${CLAUDE_SKILL_DIR}` are FLAGGED, except the
    hooks.json and preload exemptions below.

THE COMMAND-POSITION RULE this file implements. Deciding where prose ends and
a command begins is the whole difficulty, so the rule is a disjunction of five
narrow signals, each requiring the occurrence to look like a path to an
EXECUTABLE artifact (`_EXECUTABLE_SUFFIXES`, or an extension-less path under a
`bin/` segment) unless stated otherwise:

  S1 launcher-led -- an interpreter or launcher token appears earlier on the
     same line (`${BOOTSTRAP_PYTHON...}` / `${BOOTSTRAP_PROJECT_PYTHON...}`,
     a `<...python...>` placeholder, a literal `python`/`python3`/
     `python.exe` word, `uv run`, `bash`/`sh`/`node`/`pwsh`/`powershell`).
  S2 command-key-led -- the line is a YAML-ish command field
     (`_COMMAND_KEYS`: tool, command, operation, detail, action, invocation,
     satisfied_by), which is a value a reader executes.
  S3 first-word -- the occurrence begins the line's first shell word, inside a
     Markdown fenced code block, or anywhere in a non-Markdown file. (A
     continuation line of a multi-line command is caught here too: its first
     word is an argument the same shell must still resolve.)
  S4 inline span with arguments -- an inline code span whose content begins
     with the occurrence AND carries something after the path. A span that is
     EXACTLY the path is a doc coordinate, not a command, and is let through.
  S5 `cd` -- the occurrence immediately follows `cd `, with or without a path
     suffix (the `(cd ${CLAUDE_PLUGIN_ROOT} && ...)` shape).

WHAT IS DELIBERATELY NOT FLAGGED, preferring false negatives:
  - `hooks/hooks.json` entirely, and any `!` preload line: two of the
    expanded surfaces (`_is_expanded_surface`, `preload_lines`); the third, a
    SKILL.md, is handled by the surface-aware rule above.
  - The `${CLAUDE_PLUGIN_ROOT:-<fallback>}` form: a `:-` default is a hook
    script's deliberate self-location fallback, not a bare read.
  - Prose that names the variable with no path, or with a non-executable path
    (`${CLAUDE_PLUGIN_ROOT}/CLAUDE.md`, `/examples/`, `/skills_kit_lib/`
    used as a doc coordinate): no signal fires.
  - A `Read (${CLAUDE_PLUGIN_ROOT}/.../x.md)` tool argument: not a shell
    command, and a different defect class.
  - An inline code span that is exactly an executable path and nothing else
    (S4's argument requirement) -- it may be a command, but it reads as a
    coordinate and the scanner cannot tell.
  - A path assembled across adjacent Python string literals, where the
    filename lands on the next source line (`${{CLAUDE_PLUGIN_ROOT}}/scripts/`
    + `"refresh_unreal_stub.py ..."` in `plugins/unreal-kit/custom_bootstrap.py`
    and `plugins/unreal-kit/scripts/search_unreal_stub.py`): line-local
    scanning cannot see the suffix. Named here so the gap is a record, not a
    surprise.

STATUS: this lane holds at zero offenders. The allowlist covers ONLY
documentation that describes or quotes the mechanism, never a command awaiting
a migration: an offender is a finding to fix at the site, not an entry to add.
Per the root CLAUDE.md insights `a_check_must_be_shown_to_fail` and
`guard_cannot_see_its_own_subject`, the real-file lane asserts a property
against real files and the injected-string counterfactuals prove each signal
fires; do not quiet a genuine finding with an allowlist entry.
"""

from __future__ import annotations

import functools
import re
import subprocess
from pathlib import Path
from typing import NamedTuple

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: The braced read this guard is about. The doubled form is an f-string's
#: escaped spelling of the same text (`f"${{CLAUDE_PLUGIN_ROOT}}/..."`), which
#: renders identically at runtime.
_OCCURRENCE_RE = re.compile(r"\$\{\{?CLAUDE_PLUGIN_ROOT\}\}?")

#: The skill-directory variable, flagged outside a SKILL.md for the same
#: reason `${CLAUDE_PLUGIN_ROOT}` is.
_SKILL_DIR_RE = re.compile(r"\$\{\{?CLAUDE_SKILL_DIR\}\}?")

#: `${CLAUDE_PLUGIN_ROOT:-...}` -- a shell default, used by a hook script to
#: locate itself when launched outside the harness. Not a bare read.
_WITH_DEFAULT_RE = re.compile(r"\$\{CLAUDE_PLUGIN_ROOT:")

_SKILL_MD_RE = re.compile(r"(?:^|/)plugins/[^/]+/skills/(?:.+/)?SKILL\.md$")


def is_skill_md(path: str) -> bool:
    """A plugin skill's own SKILL.md -- the substituted surface (3)."""
    return bool(_SKILL_MD_RE.search(path.replace("\\", "/")))


@functools.lru_cache(maxsize=1)
def _kit_root_re() -> re.Pattern[str]:
    """`${<PLUGIN>_ROOT...}` for every plugin directory present, with the
    variable names DERIVED by `plugin_root_env_var_name` (never re-typed)."""
    from bootstrap_lib.env_var_check import plugin_root_env_var_name

    names = sorted(
        {plugin_root_env_var_name(d.name)
         for d in (_REPO_ROOT / "plugins").iterdir()
         if (d / ".claude-plugin" / "plugin.json").is_file()},
        key=len, reverse=True)
    assert names, "no plugin directories found to derive root variable names"
    return re.compile(
        r"\$\{\{?(?:" + "|".join(map(re.escape, names)) + r")(?::[^}\n]*)?\}\}?")


_EXECUTABLE_SUFFIXES = (".py", ".sh", ".cmd", ".bat", ".ps1", ".js", ".mjs")

#: Characters that end a shell word in the text shapes this repo uses.
_PATH_END_RE = re.compile(r"[\s\"'`)\];|&,]")

_LAUNCHER_RES = (
    re.compile(r"\$\{?\{?BOOTSTRAP_(?:PROJECT_)?PYTHON"),
    re.compile(r"<[^<>]*[Pp]ython[^<>]*>"),
    re.compile(r"(?:^|[\s\"'(/=`])(?:python3?|python\.exe)\b"),
    re.compile(r"\buv\s+run\b"),
    re.compile(r"(?:^|[\s\"'(`])(?:bash|sh|node|pwsh|powershell)\s"),
)

_COMMAND_KEYS = (
    "tool", "command", "operation", "detail", "action", "invocation",
    "satisfied_by",
)
_COMMAND_KEY_RE = re.compile(
    r"^\s*(?:[-|]\s*)?(?:" + "|".join(_COMMAND_KEYS) + r")\s*:"
)

#: Leading noise before the first shell word: indentation, a list bullet, a
#: table cell pipe, a shell-continuation remnant, and an opening quote.
_LEADING_NOISE_RE = re.compile(r"^[\s>|]*(?:[-*]\s+)?[\"']?")

_CD_RE = re.compile(r"\bcd\s+[\"']?$")

_FENCE_RE = re.compile(r"^\s*(?:`{3,}|~{3,})")

#: An inline `!` preload (`` !`cmd` ``) at line start or after whitespace --
#: the same shape `scan_skill_preloads` in
#: tests/repo-scripts/test_python_invocation_standard.py recognizes -- and a
#: ```` ```! ```` fenced block.
_INLINE_PRELOAD_RE = re.compile(r"(?:^|(?<=\s))!`[^`\n]+`")
_PRELOAD_FENCE_OPEN_RE = re.compile(r"^\s*`{3,}!\s*$")
_FENCE_CLOSE_RE = re.compile(r"^\s*`{3,}\s*$")

_INLINE_SPAN_RE = re.compile(r"`([^`\n]+)`")

#: A hooks manifest's `"command": "..."` field -- not the `"type": "command"`
#: discriminator that sits beside it.
_COMMAND_FIELD_RE = re.compile(r'"command"\s*:')


class Hit(NamedTuple):
    line: int
    signal: str
    text: str


def _path_after(line: str, end: int) -> str:
    """The path-looking remainder of the shell word starting at `end` (just
    past the occurrence). Empty when the occurrence is not followed by `/`."""
    if end >= len(line) or line[end] != "/":
        return ""
    stop = _PATH_END_RE.search(line, end)
    return line[end:stop.start()] if stop else line[end:]


def _is_executable_path(suffix: str) -> bool:
    """A path suffix that names something a shell would execute or hand to an
    interpreter: a known executable extension, or an extension-less path
    under a `bin/` segment (a CLI shim)."""
    if not suffix:
        return False
    tail = suffix.rstrip("\\")
    if tail.endswith(_EXECUTABLE_SUFFIXES):
        return True
    last = tail.rsplit("/", 1)[-1]
    return "/bin/" in tail and bool(last) and "." not in last


def preload_lines(text: str) -> set[int]:
    """1-based lines carrying a skill `!` preload command -- an inline
    `` !`cmd` `` or a line inside a ```` ```! ```` block. Claude Code
    substitutes `${CLAUDE_PLUGIN_ROOT}` there before the command runs."""
    lines: set[int] = set()
    in_preload_fence = False
    for lineno, line in enumerate(text.splitlines(), start=1):
        if in_preload_fence:
            if _FENCE_CLOSE_RE.match(line):
                in_preload_fence = False
            else:
                lines.add(lineno)
            continue
        if _PRELOAD_FENCE_OPEN_RE.match(line):
            in_preload_fence = True
            continue
        if _INLINE_PRELOAD_RE.search(line):
            lines.add(lineno)
    return lines


def _inline_span_signal(line: str, start: int, end: int) -> bool:
    """S4: an inline code span whose content BEGINS with the occurrence and
    carries arguments after the path. A span that is exactly the path is a
    doc coordinate and returns False."""
    for m in _INLINE_SPAN_RE.finditer(line):
        if m.start(1) != start or m.end(1) < end:
            continue
        suffix = _path_after(line, end)
        if not _is_executable_path(suffix):
            return False
        rest = line[end + len(suffix):m.end(1)].strip()
        return bool(rest)
    return False


def scan_text(path: str, text: str) -> list[Hit]:
    """Every command-position occurrence the surface-aware rule flags in one
    tracked file's text, per the five signals in the module docstring.

    `path` decides (a) whether Markdown fence tracking applies (S3 needs a
    fence in Markdown, where a line may begin with a prose code span, and
    needs none elsewhere) and (b) the surface: in a SKILL.md the flagged
    spelling is a bootstrap `<PLUGIN>_ROOT` name, anywhere else it is
    `${CLAUDE_PLUGIN_ROOT}` / `${CLAUDE_SKILL_DIR}`.
    """
    is_markdown = path.endswith(".md")
    skill_md = is_skill_md(path)
    flagged_res = ((_kit_root_re(),) if skill_md
                   else (_OCCURRENCE_RE, _SKILL_DIR_RE))
    skip = preload_lines(text) if is_markdown and not skill_md else set()
    hits: list[Hit] = []
    in_fence = False
    for lineno, line in enumerate(text.splitlines(), start=1):
        if is_markdown and _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if lineno in skip:
            continue
        for flagged_re in flagged_res:
            for m in flagged_re.finditer(line):
                if _WITH_DEFAULT_RE.match(line, m.start()):
                    continue
                signal = _classify(line, m.start(), m.end(),
                                   in_fence or not is_markdown)
                if signal:
                    hits.append(Hit(lineno, signal, line.strip()))
    return hits


def _classify(line: str, start: int, end: int, first_word_ok: bool) -> str:
    """The first signal that fires for one occurrence, or "" for none."""
    if _CD_RE.search(line[:start]):
        return "S5-cd"
    suffix = _path_after(line, end)
    if not _is_executable_path(suffix):
        return ""
    before = line[:start]
    if any(r.search(before) for r in _LAUNCHER_RES):
        return "S1-launcher"
    if _COMMAND_KEY_RE.match(line):
        return "S2-command-key"
    if first_word_ok and _LEADING_NOISE_RE.match(before).end() == start:
        return "S3-first-word"
    if _inline_span_signal(line, start, end):
        return "S4-inline-span"
    return ""


# --- real-file lane --------------------------------------------------------

#: Everything under plugins/ that can carry a command an agent types. `*.json`
#: is in the universe so the hooks.json exemption is an explicit rule rather
#: than an accident of which patterns were chosen.
_PATTERNS = (
    "plugins/*.md", "plugins/*/**/*.md", "plugins/*/*.md",
    "plugins/*/**/*.py", "plugins/*/*.py",
    "plugins/*/**/*.sh", "plugins/*/**/*.cmd", "plugins/*/**/*.bat",
    "plugins/*/**/*.json", "plugins/*/*.json",
    "plugins/*/bin/*",
)


def _is_expanded_surface(path: str) -> bool:
    """A file the harness reads before executing: a plugin's hooks manifest.
    `plugins/bootstrap/hooks/hooks.json` is the worked example -- its
    SessionStart `command:` launches session-bootstrap.sh, the only writer of
    `BOOTSTRAP_PYTHON`, and that variable is present in a Bash tool call, so
    the expansion demonstrably happened."""
    return path.endswith("hooks/hooks.json")


class AllowlistEntry(NamedTuple):
    anchor: str
    reason: str


#: Whole-file exemptions for documentation that NAMES the mechanism. Nothing
#: here may be a command awaiting the migration: the genuine Bash sites stay
#: red until they are fixed. `anchor` must still be a literal substring of the
#: file (see test_allowlist_is_not_stale).
_ALLOWLIST: dict[str, AllowlistEntry] = {
    "plugins/CLAUDE.md": AllowlistEntry(
        "Three surfaces expand `${CLAUDE_PLUGIN_ROOT}`",
        "states the three-surface rule this guard enforces; its "
        "`bash ${CLAUDE_PLUGIN_ROOT}/hooks/sessionstart/...` text is the "
        "hooks.json worked example, quoted, not a command to run"),
    "plugins/bootstrap/skills/bootstrap/references/plugin-reload-lifecycle.md":
        AllowlistEntry(
            "the script behind that",
            "describes how a hooks.json `command:` registration resolves and "
            "what a version update does to it; the quoted `bash "
            "${CLAUDE_PLUGIN_ROOT}/hooks/.../foo.sh` is that registration"),
    "plugins/skills-kit/skills/md-domain/references/skill-domain/"
    "example-verification.md":
        AllowlistEntry(
            "The composition broke.",
            "a record of two argument-passing bugs that shipped; the fenced "
            "block reproduces the BROKEN command verbatim as the evidence for "
            "the first one, so migrating it would falsify the historical "
            "record. Its paths are unreal-kit's "
            "(skills/ue-python-api/bin/ue-runner.cmd, "
            "skills/fix-up-redirectors/bin/apply_fixups.py) quoted inside a "
            "skills-kit document, so SKILLS_KIT_ROOT would be a false claim "
            "and UNREAL_KIT_ROOT would wrongly read as a command to run"),
}


def _git_ls_files(*patterns: str) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(_REPO_ROOT), "ls-files", *patterns],
        capture_output=True, text=True, check=True,
    )
    return sorted({line for line in result.stdout.splitlines() if line})


def collect_offenders() -> list[str]:
    """Every command-position hit in a tracked plugins/** file that is
    neither a harness-expanded surface nor allowlisted."""
    offenders: list[str] = []
    for rel in _git_ls_files(*_PATTERNS):
        if _is_expanded_surface(rel) or rel in _ALLOWLIST:
            continue
        text = (_REPO_ROOT / rel).read_text(encoding="utf-8")
        for hit in scan_text(rel, text):
            offenders.append(f"{rel}:{hit.line} [{hit.signal}] {hit.text}")
    return offenders


def test_tracked_plugin_files_have_no_command_position_plugin_root():
    """No tracked plugins/** file spells a plugin-root variable for the
    wrong surface: `${<PLUGIN>_ROOT...}` in a SKILL.md, or
    `${CLAUDE_PLUGIN_ROOT}` / `${CLAUDE_SKILL_DIR}` anywhere else in command
    position.

    Revert proof: the assertion reads real tracked files and reports real
    hits, so it needs no fixture to show it fails -- re-breaking any one site
    (restoring `${CLAUDE_PLUGIN_ROOT}/scripts/x.py` after a launcher) turns it
    red, which `test_each_signal_fires_on_an_injected_command` proves per
    signal without touching a tracked file.
    """
    offenders = collect_offenders()
    assert not offenders, (
        "plugin-root variable spelled for the wrong surface (a SKILL.md body "
        "uses ${CLAUDE_SKILL_DIR} / ${CLAUDE_PLUGIN_ROOT}; every other file "
        "uses \"${<PLUGIN>_ROOT:?...}\"):\n" + "\n".join(offenders)
    )


# --- counterfactuals: each signal must be shown to fire --------------------

def test_each_signal_fires_on_an_injected_command():
    """Prove every signal detects a real command shape. Revert proof for the
    test itself: drop a signal's branch from `_classify` and its case here
    goes red, so this is not a vacuous "== []" check."""
    launcher = ('run `"${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" '
                '${CLAUDE_PLUGIN_ROOT}/scripts/x.py` first\n')
    assert [h.signal for h in scan_text("plugins/p/SKILL.md", launcher)] == [
        "S1-launcher"]

    key = "          tool: ${CLAUDE_PLUGIN_ROOT}/scripts/x.py\n"
    assert [h.signal for h in scan_text("plugins/p/SKILL.md", key)] == [
        "S2-command-key"]

    fenced = ('```bash\n'
              '  "${CLAUDE_PLUGIN_ROOT}/scripts/x.py" --flag \\\n'
              '```\n')
    assert [h.signal for h in scan_text("plugins/p/SKILL.md", fenced)] == [
        "S3-first-word"]

    in_python = '    "${CLAUDE_PLUGIN_ROOT}/scripts/x.py" <path>\n'
    assert [h.signal for h in scan_text("plugins/p/scripts/x.py", in_python)] == [
        "S3-first-word"]

    span = "calls `${CLAUDE_PLUGIN_ROOT}/scripts/s.sh qwen36|qwen38`; this\n"
    assert [h.signal for h in scan_text("plugins/p/CLAUDE.md", span)] == [
        "S4-inline-span"]

    cd = "(cd ${CLAUDE_PLUGIN_ROOT} && <venvPython> scripts/r.py)\n"
    assert [h.signal for h in scan_text("plugins/p/SKILL.md", cd)] == ["S5-cd"]

    shim = '```sh\n"${CLAUDE_PLUGIN_ROOT}/bin/plugin-cli" status\n```\n'
    assert [h.signal for h in scan_text("plugins/p/README.md", shim)] == [
        "S3-first-word"]


def test_doubled_f_string_brace_form_is_recognized():
    """An f-string writes the same runtime text as `${{CLAUDE_PLUGIN_ROOT}}`;
    the occurrence regex must see both spellings. Revert proof: drop `\\{?`
    and `\\}?` from `_OCCURRENCE_RE` and this goes red."""
    plain = '    "${CLAUDE_PLUGIN_ROOT}/scripts/x.py" a\n'
    doubled = '    "${{CLAUDE_PLUGIN_ROOT}}/scripts/x.py" a\n'
    assert len(scan_text("plugins/p/x.py", plain)) == 1
    assert len(scan_text("plugins/p/x.py", doubled)) == 1


def test_prose_and_doc_coordinates_are_not_flagged():
    """The shapes the module docstring promises to let through. Revert proof:
    delete `_is_executable_path`'s suffix test (accept any path) and the
    `.md`/`/examples/` cases go red; delete `_inline_span_signal`'s
    argument requirement and the bare-span case goes red."""
    bare = "`${CLAUDE_PLUGIN_ROOT}` of the CURRENT skill is NOT it\n"
    doc_coordinate = "a Dec-N entry in `${CLAUDE_PLUGIN_ROOT}/CLAUDE.md` is\n"
    directory = "an example under `${CLAUDE_PLUGIN_ROOT}/examples/`.\n"
    strip_prefix = 'rel = raw.replace("${CLAUDE_PLUGIN_ROOT}/", "")\n'
    bare_span = "the contract is in `${CLAUDE_PLUGIN_ROOT}/lib/registry.py`\n"
    read_tool = '          tool: "Read (${CLAUDE_PLUGIN_ROOT}/refs/a.md)"\n'
    with_default = 'S="${CLAUDE_PLUGIN_ROOT:-$(dirname "$0")/..}/scripts"\n'
    for text in (bare, doc_coordinate, directory, strip_prefix, bare_span,
                 read_tool, with_default):
        assert scan_text("plugins/p/SKILL.md", text) == [], text


def test_preload_and_hooks_json_surfaces_are_exempt():
    """The preload and hooks.json surfaces. Revert proof: drop the
    `lineno in skip` guard and the preload cases go red; drop
    `_is_expanded_surface` and `plugins/bootstrap/hooks/hooks.json` appears
    in `collect_offenders()`, which the real-file assertion below pins."""
    inline = ('!`uv run --no-project python '
              '"${CLAUDE_PLUGIN_ROOT}/scripts/x.py" $ARGUMENTS`\n')
    assert scan_text("plugins/p/SKILL.md", inline) == []

    fenced = ('```!\n'
              'bash "${CLAUDE_PLUGIN_ROOT}/scripts/x.sh"\n'
              '```\n')
    assert scan_text("plugins/p/SKILL.md", fenced) == []

    assert _is_expanded_surface("plugins/bootstrap/hooks/hooks.json")
    assert _is_expanded_surface("plugins/unreal-kit/hooks/hooks.json")
    assert not _is_expanded_surface("plugins/p/scripts/hooks.json")


def test_hooks_json_command_fields_still_use_the_harness_variable():
    """The positive half of the surface rule, asserted against real
    files: every tracked `hooks/hooks.json` reaches its script through
    `${CLAUDE_PLUGIN_ROOT}`. Without this the guard would read as "the
    variable is always wrong", and a later edit could replace a working
    hook registration with a `<PLUGIN>_ROOT` form the harness never sets.

    Revert proof: rewrite any tracked hooks.json `command:` to an absolute or
    `<PLUGIN>_ROOT`-rooted path and this goes red."""
    manifests = [p for p in _git_ls_files("plugins/*/hooks/hooks.json")]
    assert manifests, "no tracked plugin hooks manifest found"
    offenders = []
    for rel in manifests:
        text = (_REPO_ROOT / rel).read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if not _COMMAND_FIELD_RE.search(line):
                continue
            if not _OCCURRENCE_RE.search(line):
                offenders.append(f"{rel}:{lineno} {line.strip()}")
    assert not offenders, (
        "hooks.json command field not rooted at ${CLAUDE_PLUGIN_ROOT}:\n"
        + "\n".join(offenders)
    )


# --- surface-aware rule: both directions -----------------------------------

def test_skill_md_allows_harness_variables_and_flags_bootstrap_names():
    """In a SKILL.md the harness spelling is allowed and the bootstrap name is
    flagged, in the body and in `allowed-tools`/capability frontmatter.

    Revert proof: make `scan_text` ignore `is_skill_md` and the first two
    cases go red (harness spelling flagged), or swap `flagged_res` for the
    skill branch and the last two go red (bootstrap name let through)."""
    skill = "plugins/p/skills/s/SKILL.md"
    fenced_plugin = ('```bash\n'
                     '"${CLAUDE_PLUGIN_ROOT}/scripts/x.py" --flag\n'
                     '```\n')
    fenced_skill = ('```bash\n'
                    '"${CLAUDE_SKILL_DIR}/scripts/x.py" --flag\n'
                    '```\n')
    frontmatter = ("          operation: '\"${BOOTSTRAP_PYTHON:?requires bootstrap "
                   ">= 0.120.0}\" \"${CLAUDE_SKILL_DIR}/scripts/x.py\" a'\n")
    assert scan_text(skill, fenced_plugin) == []
    assert scan_text(skill, fenced_skill) == []
    assert scan_text(skill, frontmatter) == []
    bootstrap_name = ('```bash\n'
                      '"${AWESOME_KIT_ROOT:?x}/scripts/task.py" <verb>\n'
                      '```\n')
    assert [h.signal for h in scan_text(skill, bootstrap_name)] == [
        "S3-first-word"]
    launched = ("          tool: '\"${BOOTSTRAP_PYTHON:?requires bootstrap >= "
                "0.120.0}\" \"${GIT_KIT_ROOT:?x}/scripts/p.py\"'\n")
    assert [h.signal for h in scan_text(skill, launched)] == ["S1-launcher"]


def test_other_files_flag_harness_variables_and_allow_bootstrap_names():
    """Outside a SKILL.md the harness spellings are flagged, the bootstrap
    name is the correct form. A references/*.md under a skill directory is NOT
    a SKILL.md.

    Revert proof: drop `_SKILL_DIR_RE` from the non-skill branch and the
    `${CLAUDE_SKILL_DIR}` case goes red."""
    ref = "plugins/p/skills/s/references/r.md"
    skill_dir = ('```bash\n'
                 '"${CLAUDE_SKILL_DIR}/scripts/x.py" --flag\n'
                 '```\n')
    plugin_root = ('```bash\n'
                   '"${CLAUDE_PLUGIN_ROOT}/scripts/x.py" --flag\n'
                   '```\n')
    assert [h.signal for h in scan_text(ref, skill_dir)] == ["S3-first-word"]
    assert [h.signal for h in scan_text(ref, plugin_root)] == ["S3-first-word"]
    assert [h.signal for h in scan_text("plugins/p/scripts/x.py",
                                        '    "${CLAUDE_SKILL_DIR}/scripts/y.py" a\n')
            ] == ["S3-first-word"]
    kit_root = ('```bash\n'
                '"${AWESOME_KIT_ROOT:?x}/scripts/task.py" <verb>\n'
                '```\n')
    assert scan_text(ref, kit_root) == []
    assert not is_skill_md(ref)
    assert is_skill_md("plugins/p/skills/s/SKILL.md")
    assert is_skill_md("plugins/p/skills/deep/er/SKILL.md")
    assert not is_skill_md("plugins/p/README.md")


def test_kit_root_names_are_derived_from_the_producer():
    """The flagged names come from `plugin_root_env_var_name` over the plugin
    directories, so a new plugin is covered without editing this file.

    Revert proof: hard-code a list in `_kit_root_re` that omits a plugin and
    this goes red for that plugin."""
    from bootstrap_lib.env_var_check import plugin_root_env_var_name

    for d in (_REPO_ROOT / "plugins").iterdir():
        if (d / ".claude-plugin" / "plugin.json").is_file():
            read = "${" + plugin_root_env_var_name(d.name) + ":?x}"
            assert _kit_root_re().search(read), d.name


# --- allowlist staleness ---------------------------------------------------

def test_allowlist_is_not_stale():
    """Every allowlisted path is still tracked, still produces a hit worth
    exempting, and still contains its anchor text -- an entry whose file was
    renamed, fixed, or edited away from the reasoning it records is silently
    wrong, not silently safe.

    Revert proof: typo any anchor above and this goes red; fix an allowlisted
    file's last hit and the "no longer needed" branch goes red."""
    tracked = set(_git_ls_files())
    offenders = []
    for path, entry in _ALLOWLIST.items():
        if path not in tracked:
            offenders.append(f"{path}: not tracked by git ls-files")
            continue
        text = (_REPO_ROOT / path).read_text(encoding="utf-8")
        if entry.anchor not in text:
            offenders.append(f"{path}: anchor {entry.anchor!r} not in file")
        if not scan_text(path, text):
            offenders.append(f"{path}: no longer hits; drop the entry")
    assert not offenders, "stale allowlist entry:\n" + "\n".join(offenders)
