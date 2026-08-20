"""Static drift test for the B1 worker assets (step 12).

Pins `skills/execute-work-unit/SKILL.md`'s enumerated-invocations block
against `content_pipeline.execution.drivers.claude_bg.enumerate_worker_invocations`
in BOTH directions: every command string in the SKILL.md block must be one
the function actually produces, and every string the function produces must
appear in the block. Prose drift in that file -- a hand-edited invocation
that no longer matches what the library emits -- is exactly the failure this
test exists to catch; the plan of record documents a 2026-08-17 probe that
stalled precisely because a worker was given an outcome to satisfy rather
than the library's own literal invocation strings, so the SKILL.md's claim to
carry those strings verbatim has to be checked, not merely asserted in prose.

The SKILL.md's own "Machine-checked contract" section documents this
extraction format for a human reader; this module is where it is enforced.

Separator portability -- established, not assumed: `answer_path_for` and
`envelope_path_for` (``execution/drivers/claude_bg.py``) build their paths
with ``os.path.join``, which emits ``\\`` on Windows and ``/`` on
macOS/Linux. The SKILL.md block is written with forward slashes throughout
(the documentation spelling). Comparing it byte-for-byte against the
function's OWN-platform output would make this test fail on every
non-Windows machine, so ``_normalize_sep`` is applied to BOTH sides -- the
strings extracted from SKILL.md and the strings the function actually
returns -- before every comparison below. Normalizing only the function's
side would still leave a platform-dependent literal comparison against
whatever happens to be in the file; normalizing both sides to the same
canonical form is what makes the comparison platform-independent rather
than platform-fragile.

A second, coupled effect of the same root cause: ``enumerate_worker_invocations``
builds its ``read``/``submit``/``fail`` strings (each ``<argv>
protocol @<envelope path>``, ``submit`` additionally carrying
``--text-file=<answer path>``) with ``shlex.join``, which quotes a token
containing a backslash but leaves a plain forward-slash path unquoted -- so
on Windows the function's raw strings wrap the ``@<path>``/
``--text-file=<path>`` tokens in single quotes; on macOS/Linux they do not.
The three ``write`` strings, by contrast, are plain f-strings, never
shell-quoted at all -- each is a Write-tool target, not a subprocess argv,
and re-tokenizing one with ``shlex.split``/``shlex.join`` would itself
corrupt it (``->`` is outside ``shlex``'s unquoted-safe character set and
would come back wrapped in quotes that were never there). So
``_normalize_sep`` does NOT round-trip through ``shlex`` at all: it swaps
every backslash for a forward slash first (a plain string replace, safe for
both command shapes), then strips any ``'`` characters that remain.
Stripping is safe here specifically because the quotes ``shlex.join`` adds
exist ONLY to escape the backslash that the separator swap has, by that
point, already removed, and none of these six invocation strings ever
legitimately contains a literal apostrophe.
"""

from __future__ import annotations

import os
import re
import shlex

import pytest

from content_pipeline.execution.drivers.claude_bg import (
    WorkerCommand,
    enumerate_worker_invocations,
)

PLUGIN_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "plugins", "content-pipeline-kit")
)
SKILL_PATH = os.path.join(
    PLUGIN_ROOT, "skills", "execute-work-unit", "SKILL.md"
)

BEGIN_MARKER = "<!-- BEGIN ENUMERATED-INVOCATIONS -->"
END_MARKER = "<!-- END ENUMERATED-INVOCATIONS -->"

# The exact example inputs the SKILL.md's fenced block was generated from
# (see that file's "Where the exact invocations come from" section). Any
# change to these constants must be paired with regenerating the block by
# calling `enumerate_worker_invocations` again and pasting its real output --
# never hand-edited.
EXAMPLE_ANSWER_DIR = "/path/to/answers"
EXAMPLE_ENVELOPE_DIR = "/path/to/envelopes"
EXAMPLE_ARGV = ("python", "mount.py", "run")
EXAMPLE_RUN_ID = "RUN_ID"
EXAMPLE_UNIT_ID = "UNIT_ID"
EXAMPLE_WORKER_ID = "WORKER_ID"

_LABEL_LINE_RE = re.compile(r"^(read|submit|fail|write):\s(.+)$")


def _normalize_sep(command: str) -> str:
    """Canonicalize a command string for cross-platform comparison: swap
    every backslash for a forward slash, then strip any ``'`` characters
    that remain. See the module docstring's "Separator portability" section
    for why this is a plain string transform rather than a
    `shlex.split`/`shlex.join` round-trip (the `write` invocation is not
    shell syntax at all, and re-tokenizing it would corrupt it). Applied to
    BOTH the SKILL.md-extracted strings and the function's live output --
    normalizing only one side is still a platform-dependent comparison
    against whatever literal text is on the other side."""
    return command.replace("\\", "/").replace("'", "")


def _normalize_all(commands) -> set:
    return {_normalize_sep(c) for c in commands}


def _read_skill_text() -> str:
    with open(SKILL_PATH, "r", encoding="utf-8") as f:
        return f.read()


def _extract_block(text: str) -> str:
    start = text.index(BEGIN_MARKER)
    end = text.index(END_MARKER, start)
    return text[start + len(BEGIN_MARKER) : end]


def _extract_invocation_commands(block: str) -> list[str]:
    """Every `label: command` line inside the fenced ```text``` block,
    label stripped. Order-preserving; duplicates are NOT deduplicated (the
    bidirectional set comparison in the tests below would still catch a
    duplicate that changed the SET of distinct commands, but this helper's
    job is just extraction)."""
    commands: list[str] = []
    for line in block.splitlines():
        match = _LABEL_LINE_RE.match(line.strip())
        if match:
            commands.append(match.group(2))
    return commands


def _expected_invocations() -> tuple[str, ...]:
    wc = WorkerCommand(
        argv=EXAMPLE_ARGV, answer_dir=EXAMPLE_ANSWER_DIR, envelope_dir=EXAMPLE_ENVELOPE_DIR
    )
    return enumerate_worker_invocations(
        wc, EXAMPLE_RUN_ID, EXAMPLE_UNIT_ID, EXAMPLE_WORKER_ID
    )


def test_skill_md_has_the_enumerated_invocations_markers():
    text = _read_skill_text()
    assert BEGIN_MARKER in text
    assert END_MARKER in text
    assert text.index(BEGIN_MARKER) < text.index(END_MARKER)


def test_skill_md_block_extracts_exactly_six_labeled_commands():
    block = _extract_block(_read_skill_text())
    commands = _extract_invocation_commands(block)
    assert len(commands) == 6, (
        f"expected exactly 6 labeled invocation lines in the SKILL.md block, "
        f"found {len(commands)}: {commands!r}"
    )


def test_skill_md_block_carries_no_claim_line():
    """The dispatcher claims (see ``dispatch_unit``), so ``claim`` left the
    worker's set entirely. Dropping it from ``_LABEL_LINE_RE``'s alternation
    without also dropping it from the generated block would silently stop
    covering a verb: the extractor would skip the line, and the bidirectional
    set comparison below would still pass. This asserts the block's RAW text,
    which that regex never touches."""
    block = _extract_block(_read_skill_text())
    assert "claim:" not in block
    assert ".claim.json" not in block


def test_every_skill_md_invocation_is_produced_by_the_function():
    """Direction 1: nothing in the SKILL.md block is invented -- every
    command string it carries is one `enumerate_worker_invocations` actually
    produces for the documented example inputs."""
    block = _extract_block(_read_skill_text())
    skill_commands = _normalize_all(_extract_invocation_commands(block))
    expected = _normalize_all(_expected_invocations())
    extra = skill_commands - expected
    assert not extra, (
        f"SKILL.md carries invocation(s) enumerate_worker_invocations does "
        f"not produce: {extra!r}"
    )


def test_every_function_invocation_appears_in_skill_md():
    """Direction 2: nothing the function produces is missing from the
    SKILL.md block -- a worker following the documented procedure is never
    missing a step the library actually requires."""
    block = _extract_block(_read_skill_text())
    skill_commands = _normalize_all(_extract_invocation_commands(block))
    expected = _normalize_all(_expected_invocations())
    missing = expected - skill_commands
    assert not missing, (
        f"enumerate_worker_invocations produces invocation(s) missing from "
        f"SKILL.md: {missing!r}"
    )


def test_skill_md_invocations_exactly_match_the_function_output():
    """Both directions folded into one assertion, matching the brief's
    'assert BOTH DIRECTIONS' requirement as a single equality check."""
    block = _extract_block(_read_skill_text())
    skill_commands = _normalize_all(_extract_invocation_commands(block))
    expected = _normalize_all(_expected_invocations())
    assert skill_commands == expected


def test_normalization_holds_across_platform_separators():
    """Portability demonstration, not just an assertion: reconstruct the
    ``read``/``submit``/``fail``/``write``x3 invocations using the
    EXACT construction ``enumerate_worker_invocations`` uses (``protocol
    @<envelope path>`` for the three subprocess commands, ``shlex.join``,
    plain f-strings for the three Write-tool targets), but with the answer
    and envelope paths joined using EACH platform's separator explicitly --
    rather than relying on ``os.path.join``'s native (host-determined)
    choice -- and show the normalized comparison against SKILL.md holds for
    both. This does not merely assert the fix works; it proves the fix
    holds for the OTHER platform's spelling too, not just whichever OS
    happens to run this suite."""
    block = _extract_block(_read_skill_text())
    skill_commands = _normalize_all(_extract_invocation_commands(block))

    base = EXAMPLE_ARGV
    stem = f"{EXAMPLE_RUN_ID}__{EXAMPLE_UNIT_ID}"

    for sep, label in (("\\", "Windows"), ("/", "POSIX")):
        answer_path = EXAMPLE_ANSWER_DIR + sep + f"{stem}.answer.txt"
        read_path = EXAMPLE_ENVELOPE_DIR + sep + f"{stem}.read.json"
        submit_path = EXAMPLE_ENVELOPE_DIR + sep + f"{stem}.submit.json"
        fail_path = EXAMPLE_ENVELOPE_DIR + sep + f"{stem}.fail.json"
        variant = (
            shlex.join(base + ("protocol", f"@{read_path}")),
            shlex.join(
                base + ("protocol", f"@{submit_path}", f"--text-file={answer_path}")
            ),
            shlex.join(base + ("protocol", f"@{fail_path}")),
            f"Write tool -> {answer_path}",
            f"Write tool -> {submit_path}",
            f"Write tool -> {fail_path}",
        )
        assert _normalize_all(variant) == skill_commands, (
            f"normalized comparison does not hold for the {label} "
            f"({sep!r}) separator spelling of the function's own construction"
        )
