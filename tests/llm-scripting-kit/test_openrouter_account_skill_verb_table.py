"""Pins skills/openrouter-account/SKILL.md's CLI verb table against the CLI's
actual subcommands.

cli.py registers twelve subcommands (status, set-key, which, endpoints, probe,
usage, choose, seats, models, resolve, complete, request-schema), but the
SKILL.md's "The CLI" section used to claim the plugin "ships a single CLI
script ... with three subcommands" and only tabled those three. That total
count was wrong the moment a fourth subcommand was added, and nothing caught
it. This test checks the doc no longer asserts a total count, and that every
verb its table DOES name is a real subcommand (so the table itself never
drifts from the parser).
"""

import re
from pathlib import Path

from llm_scripting_kit.cli import _parser


SKILL_PATH = (
    Path(__file__).resolve().parents[2]
    / "plugins"
    / "llm-scripting-kit"
    / "skills"
    / "openrouter-account"
    / "SKILL.md"
)


def _skill_text() -> str:
    return SKILL_PATH.read_text(encoding="utf-8")


def _table_verbs(text: str) -> list[str]:
    """Extract the backtick-wrapped verb names from "The CLI" section's table."""
    section = text.split("## The CLI", 1)[1].split("### Invocation", 1)[0]
    return re.findall(r"^\|\s*`([a-z-]+)", section, flags=re.MULTILINE)


def _parser_subcommands() -> set[str]:
    parser = _parser()
    for action in parser._subparsers._group_actions:  # argparse internals, test-only introspection
        if hasattr(action, "choices"):
            return set(action.choices.keys())
    raise AssertionError("could not find the subparsers action on the CLI parser")


def test_table_verbs_are_all_real_cli_subcommands():
    text = _skill_text()
    table_verbs = _table_verbs(text)
    assert table_verbs, "expected at least one verb row in the CLI table"
    real = _parser_subcommands()
    missing = [v for v in table_verbs if v not in real]
    assert not missing, f"SKILL.md table names verbs the CLI does not register: {missing}"


def test_doc_does_not_assert_a_total_subcommand_count():
    text = _skill_text()
    cli_section = text.split("## The CLI", 1)[1].split("## ", 1)[0]
    # The old wording: "ships a single CLI script ... with three subcommands" --
    # a claim about the CLI's TOTAL surface, which cli.py's twelve subcommands
    # already contradicted. The reworded doc must not restate a total count
    # (in digits or words) and must point at README.md for the full verb set.
    assert not re.search(
        r"\bwith\s+(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|\d+)\s+subcommands?\b",
        cli_section,
        flags=re.IGNORECASE,
    ), "SKILL.md's CLI section still asserts a total subcommand count"
    assert "README" in cli_section, "SKILL.md's CLI section should point at README.md for the full verb set"
