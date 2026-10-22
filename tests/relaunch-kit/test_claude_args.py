"""claude_args: the Commander-style lexer and its flag table.

The table is pinned to the live `claude --help` (skipped when claude is absent),
and every session selector is removed in every accepted spelling without
consuming the values and subcommand arguments around it.
"""

import re
import shutil
import subprocess

import pytest

import claude_args
from claude_args import (
    FLAGS, NONE, OPTIONAL, REQUIRED, SELECTOR, SUBCOMMANDS, VARIADIC, ArgsError, Flag, parse, with_request,
)

MODE_FLAGS = [f for f in FLAGS if f.role == "mode"]
SELECTOR_FLAGS = [f for f in FLAGS if f.role == SELECTOR]

BEFORE = ["--model", "m", "--add-dir", "a", "b"]
AFTER = ["--effort", "high", "-n", "my name"]


def _spellings(flag: Flag):
    """Every valid way to write `flag` once, as a list of argv tokens."""
    out = []
    for name in flag.names:
        if flag.arity in (NONE, OPTIONAL):
            out.append([name])
        if flag.arity in (REQUIRED, OPTIONAL, VARIADIC):
            out.append([name, "VALUE"])
            out.append([name + "=VALUE"] if name.startswith("--") else [name + "VALUE"])
    return out


def _cases():
    for flag in SELECTOR_FLAGS:
        for spelling in _spellings(flag):
            yield pytest.param(flag, spelling, id=" ".join(spelling))


# --- session selectors -------------------------------------------------------


@pytest.mark.parametrize("flag,spelling", list(_cases()))
def test_selector_is_removed_without_touching_its_neighbours(flag, spelling):
    parsed = parse(BEFORE + spelling + AFTER)
    assert parsed.pass_through is None
    assert list(parsed.carried) == BEFORE + AFTER
    assert parsed.operands == ()


def test_every_selector_in_the_help_is_covered_by_the_cases():
    covered = {name for p in _cases() for name in p.values[0].names}
    assert covered == {n for f in SELECTOR_FLAGS for n in f.names}
    assert {n for f in SELECTOR_FLAGS for n in f.names} >= {
        "-c", "--continue", "-r", "--resume", "--session-id", "--fork-session", "--from-pr", "--teleport"
    }


@pytest.mark.parametrize("argv,expected", [
    (["-cr"], []),
    (["-cr", "ID"], []),
    (["-crID"], []),
    (["-cn", "my name"], ["-n", "my name"]),
    (["-cnname"], ["-nname"]),
    (["-c", "--model", "m"], ["--model", "m"]),
])
def test_selector_inside_a_short_flag_cluster(argv, expected):
    assert list(parse(argv).carried) == expected


def test_optional_selector_value_does_not_swallow_a_flag():
    assert list(parse(["-r", "--model", "m"]).carried) == ["--model", "m"]
    assert list(parse(["--resume", "--model", "m"]).carried) == ["--model", "m"]


def test_optional_selector_takes_a_following_word_like_claude_does():
    parsed = parse(["-r", "fix", "bug"])
    assert parsed.carried == ()
    assert parsed.operands == ("bug",)  # "fix" was the --resume search term


def test_selector_next_to_a_variadic_option_ends_it():
    parsed = parse(["--add-dir", "a", "-c", "b"])
    assert list(parsed.carried) == ["--add-dir", "a"]
    assert parsed.operands == ("b",)


def test_selector_next_to_a_subcommand_is_pass_through_and_not_lexed():
    parsed = parse(["-c", "mcp", "add", "-s", "user", "--resume", "x"])
    assert parsed.pass_through == "subcommand mcp"


# --- non-selector flags keep their spelling ----------------------------------


@pytest.mark.parametrize("argv", [
    ["--model=opus"],
    ["-nfoo"],
    ["-n", "foo bar"],
    ["--allowedTools", "Bash(git *)", "Edit", "--tools", "x"],
    ["--allowed-tools=Bash"],
    ["-d"],
    ["-dhooks,api"],
    ["--debug", "hooks"],
    ["--worktree"],
    ["-w", "name"],
    ["--system-prompt", "-starts-with-dash"],
    ["--settings", '{"a":1}'],
    ["--no-chrome", "--chrome"],
    ["--dangerously-skip-permissions"],
])
def test_other_flags_are_carried_verbatim(argv):
    parsed = parse(argv)
    assert parsed.pass_through is None
    assert list(parsed.carried) == argv


def test_required_value_may_look_like_a_flag():
    assert list(parse(["--system-prompt", "-x", "--model", "m"]).carried) == [
        "--system-prompt", "-x", "--model", "m"]


def test_variadic_consumes_until_an_option_like_token():
    parsed = parse(["--tools", "a", "b", "--verbose", "prompt"])
    assert list(parsed.carried) == ["--tools", "a", "b", "--verbose"]
    assert parsed.operands == ("prompt",)


# --- operands, the terminator, and prompts that start with a dash ------------


def test_prompt_is_dropped_but_flags_around_it_are_kept():
    parsed = parse(["--model", "m", "fix the bug", "--effort", "high"])
    assert list(parsed.carried) == ["--model", "m", "--effort", "high"]
    assert parsed.operands == ("fix the bug",)


def test_terminator_and_prompt_starting_with_a_dash():
    parsed = parse(["--model", "m", "--", "-weird", "--prompt"])
    assert parsed.pass_through is None
    assert list(parsed.carried) == ["--model", "m"]
    assert parsed.operands == ("-weird", "--prompt")


def test_flags_after_the_terminator_are_operands_not_flags():
    parsed = parse(["--", "-p"])
    assert parsed.pass_through is None
    assert parsed.operands == ("-p",)


def test_lone_dash_is_an_operand():
    assert parse(["-"]).operands == ("-",)


def test_subcommand_after_the_terminator_is_still_a_subcommand():
    assert parse(["--", "mcp", "list"]).pass_through == "subcommand mcp"


# --- relaunch command line ---------------------------------------------------


def test_request_after_a_variadic_option_is_not_swallowed():
    parsed = parse(["--add-dir", "a", "--allowedTools", "Bash"])
    for kwargs, prompt in (({"resume_id": "ID", "prompt": None}, None),
                           ({"resume_id": None, "prompt": "/task work x"}, "/task work x"),
                           ({"resume_id": "ID", "prompt": "-dash prompt"}, "-dash prompt")):
        argv = with_request(parsed.carried, **kwargs)
        again = parse(argv)
        assert list(again.carried) == list(parsed.carried)  # the selector was dropped again, nothing eaten
        assert again.operands == ((prompt,) if prompt else ())


def test_with_request_shapes():
    assert with_request(("--model", "m"), resume_id="ID", prompt=None) == ["--model", "m", "--resume", "ID"]
    assert with_request((), resume_id=None, prompt="p") == ["--", "p"]
    assert with_request((), resume_id="ID", prompt="p") == ["--resume", "ID", "--", "p"]


# --- pass-through ------------------------------------------------------------


@pytest.mark.parametrize("flag", MODE_FLAGS, ids=lambda f: f.names[0])
def test_every_mode_flag_is_pass_through_in_every_spelling(flag):
    for spelling in _spellings(flag):
        assert parse(["--model", "m"] + spelling).pass_through is not None, spelling


def test_mode_flag_as_a_value_is_not_a_mode():
    assert parse(["--system-prompt", "-p"]).pass_through is None
    assert parse(["--model=-p"]).pass_through is None


@pytest.mark.parametrize("name", sorted(SUBCOMMANDS))
def test_subcommand_is_pass_through_whatever_follows(name):
    parsed = parse(["--model", "m", name, "--not-a-claude-flag", "-s", "user"])
    assert parsed.pass_through == "subcommand " + name


# --- refusals ----------------------------------------------------------------


@pytest.mark.parametrize("argv,fragment", [
    (["--bogus"], "unknown option '--bogus'"),
    (["--bogus=1"], "unknown option '--bogus'"),
    (["--bare=1"], "unknown option '--bare'"),  # a flag with no value rejects =value, like claude
    (["--tmux=classic"], "unknown option '--tmux'"),
    (["-z"], "unknown option '-z'"),
    (["-5"], "unknown option '-5'"),
    (["--model"], "'--model' argument missing"),
    (["--tools"], "'--tools' argument missing"),
    (["--session-id"], "'--session-id' argument missing"),
])
def test_unrepresentable_command_lines_are_refused_loudly(argv, fragment):
    with pytest.raises(ArgsError) as info:
        parse(argv)
    assert fragment in str(info.value)


# --- the table itself --------------------------------------------------------


def test_table_has_no_duplicate_spelling():
    names = [n for f in FLAGS for n in f.names]
    assert len(names) == len(set(names))
    assert all(n.startswith("-") for n in names)


# --- the table against the live `claude --help` ------------------------------

_CLAUDE = shutil.which("claude")


def _help_text() -> str:
    result = subprocess.run([_CLAUDE, "--help"], capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    assert result.returncode == 0, result.stderr
    return result.stdout


def _arity_of(spec: str) -> str:
    if spec is None:
        return NONE
    if spec.startswith("<"):
        return VARIADIC if spec.endswith("...>") else REQUIRED
    if spec.startswith("["):
        return OPTIONAL
    raise AssertionError("unrecognised argument spec %r" % spec)


def parse_help(text: str):
    """(options, commands, descriptions) from Commander's help layout."""
    options = {}  # frozenset(names) -> arity
    descriptions = {}  # frozenset(names) -> text
    commands = set()
    section = None
    current = None
    for line in text.splitlines():
        if line.startswith("Options:"):
            section = "options"
            continue
        if line.startswith("Commands:"):
            section = "commands"
            continue
        if not line.strip():
            continue
        if section == "options":
            match = re.match(r"^ {2}(-\S.*?)(?: {2,}(\S.*))?$", line)
            if match and not line.startswith("   "):
                names, arg = [], None
                for part in match.group(1).split(", "):
                    pieces = part.split(None, 1)
                    names.append(pieces[0])
                    if len(pieces) > 1:
                        arg = pieces[1]
                key = frozenset(names)
                options[key] = _arity_of(arg)
                descriptions[key] = match.group(2) or ""
                current = key
            elif current is not None:
                descriptions[current] += " " + line.strip()
        elif section == "commands":
            match = re.match(r"^ {2}(\S+)", line)
            if match and not line.startswith("   "):
                commands.update(match.group(1).split("|"))
    return options, commands, descriptions


def test_help_parser_on_a_fixed_sample():
    sample = (
        "Options:\n"
        "  --add-dir <directories...>            Additional directories\n"
        "                                        to allow\n"
        "  --allowedTools, --allowed-tools <tools...>\n"
        "      Comma or space-separated list\n"
        "  --cloud [description|session_id|url]  Create a cloud session\n"
        "  -c, --continue                        Continue the most recent\n"
        "  -r, --resume [value]                  Resume a conversation\n"
        "  --model <model>                       Model\n"
        "\n"
        "Commands:\n"
        "  agents [options]                      Manage\n"
        "  plugin|plugins                        Manage plugins\n"
        "  stop|kill <id>                        Stop\n"
    )
    options, commands, descriptions = parse_help(sample)
    assert options[frozenset(["--add-dir"])] == VARIADIC
    assert options[frozenset(["--allowedTools", "--allowed-tools"])] == VARIADIC
    assert options[frozenset(["--cloud"])] == OPTIONAL
    assert options[frozenset(["-c", "--continue"])] == NONE
    assert options[frozenset(["-r", "--resume"])] == OPTIONAL
    assert options[frozenset(["--model"])] == REQUIRED
    assert commands == {"agents", "plugin", "plugins", "stop", "kill"}
    assert "to allow" in descriptions[frozenset(["--add-dir"])]


@pytest.mark.skipif(_CLAUDE is None, reason="claude is not installed")
def test_flag_table_matches_live_claude_help_aliases_and_arities():
    options, _commands, _ = parse_help(_help_text())
    table = {frozenset(f.names): f.arity for f in FLAGS}
    assert len(options) > 30, "the help parser found too few options: %r" % sorted(map(sorted, options))
    live_only = {tuple(sorted(k)): v for k, v in options.items() if table.get(k) != v}
    table_only = {tuple(sorted(k)): v for k, v in table.items() if options.get(k) != v}
    assert not live_only and not table_only, (
        "lib/claude_args.py FLAGS disagrees with `claude --help`. In the help only (or different arity): %s. "
        "In the table only (or different arity): %s" % (live_only, table_only)
    )


@pytest.mark.skipif(_CLAUDE is None, reason="claude is not installed")
def test_subcommand_table_matches_live_claude_help():
    _options, commands, _ = parse_help(_help_text())
    assert commands, "the help parser found no commands"
    assert commands == set(SUBCOMMANDS) - {"help"}


@pytest.mark.skipif(_CLAUDE is None, reason="claude is not installed")
def test_every_flag_that_mentions_resuming_is_a_reviewed_choice():
    """A new flag that picks or continues a session must be classified, not silently carried."""
    options, _commands, descriptions = parse_help(_help_text())
    reviewed_not_selectors = {
        "--bg", "--cloud", "--desktop", "--no-session-persistence", "--name", "--system-prompt-snapshot",
    }
    mentions = re.compile(r"\bresum|\bcontinu|session id|session_id", re.I)
    selectors = {n for f in SELECTOR_FLAGS for n in f.names}
    unreviewed = []
    for key, text in descriptions.items():
        if mentions.search(text) and not (key & selectors) and not (key & reviewed_not_selectors):
            unreviewed.append(sorted(key))
    assert not unreviewed, (
        "flags that mention resuming a session but are neither selectors nor reviewed: %s" % unreviewed
    )
    # and the roles in the table still name real flags
    live_names = {n for key in options for n in key}
    assert selectors <= live_names
    assert {n for f in MODE_FLAGS for n in f.names} <= live_names
