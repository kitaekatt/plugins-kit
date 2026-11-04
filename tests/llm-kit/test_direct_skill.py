"""The /direct skill (skills/direct/SKILL.md) stays user-invocable only and matches the grant hook.

Owner rule: /direct must stay unreachable by the model on its own -- frontmatter
``disable-model-invocation: true`` plus the grant hook. Never weaken either to make a
test or demo pass.

The checks are pure functions of the skill text, so each has a counterfactual test that
feeds it text with the property removed and asserts it fails. Each guard test also names
the edit to SKILL.md that turns it red:

* frontmatter -- delete ``disable-model-invocation: true`` (or set it to false) from the
  frontmatter: test_skill_is_not_model_invocable goes red; the same for ``user-invocable``.
* no preload -- add a line ``!`llm-kit relay client machines --session S``` (or a ```!
  fence): test_skill_has_no_preload goes red. A preload would run before Claude sees the
  skill, and the design (design.md 7b, P0) refuted interpolating typed arguments into one.
* verbs match the grant hook -- change ``relay client sessions`` to ``relay client lookup``
  in a command line: test_named_verbs_are_accepted_by_the_grant_hook goes red.
* every verb the hook accepts is documented -- delete the ``answer`` row:
  test_every_grant_hook_verb_is_documented goes red.
* the session reaches each call -- delete ``--session S`` from one command line:
  test_every_command_carries_the_session goes red.
* literal command -- put ``"${CLAUDE_BOOTSTRAP_DATA_ROOT:-$HOME/...}"/llm-kit`` (a braced
  expansion) back as the command word: test_command_word_is_a_literal_path goes red.
* allow rule -- delete the ``allowed-tools`` line, or change its pattern so it no longer
  covers the command: test_allow_rule_covers_the_command goes red.
* gate parses it -- change the command word to a name the gate's tool pattern rejects
  (e.g. ``llmkit``): test_gate_recognizes_the_command_form goes red.
* honesty (AD-5) -- delete "not a security boundary": test_skill_states_the_gate_is_a_guardrail
  goes red.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PLUGIN = REPO / "plugins" / "llm-kit"
SKILL = PLUGIN / "skills" / "direct" / "SKILL.md"
HOOKS = PLUGIN / "hooks"
GRANT_HOOK = HOOKS / "userpromptsubmit" / "direct_grant.py"
GATE_HOOK = HOOKS / "pretooluse" / "direct_gate.py"

#: A client call as the skill writes it. The verb must be a lowercase word, so the
#: placeholder `<verb>` in prose is not a named verb.
_CALL_RE = re.compile(r"llm-kit relay client\s+([a-z][a-z-]*)")
_INLINE_PRELOAD_RE = re.compile(r"(?:^|(?<=\s))!`([^`\n]+)`")
_PRELOAD_FENCE_OPEN_RE = re.compile(r"^\s*```!")


def grant_hook_verbs() -> frozenset[str]:
    """The client verbs direct_grant.py accepts, taken from the hook module itself."""
    sys.path.insert(0, str(HOOKS))
    try:
        spec = importlib.util.spec_from_file_location("direct_grant_under_test", GRANT_HOOK)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(HOOKS))
    return frozenset(module.R.CLIENT_VERBS)


def frontmatter(text: str) -> dict[str, str]:
    """The top-level ``key: value`` lines of the leading ``---`` block (no YAML dependency)."""
    lines = text.splitlines()
    assert lines and lines[0] == "---", "SKILL.md must start with a frontmatter block"
    end = lines.index("---", 1)
    fields: dict[str, str] = {}
    for line in lines[1:end]:
        key, sep, value = line.partition(":")
        if sep and not line.startswith((" ", "\t", "#")):
            fields[key.strip()] = value.strip()
    return fields


def frontmatter_problems(text: str) -> list[str]:
    fields = frontmatter(text)
    return [
        f"frontmatter {key} must be 'true', found {fields.get(key)!r}"
        for key in ("disable-model-invocation", "user-invocable")
        if fields.get(key) != "true"
    ]


def preload_lines(text: str) -> list[str]:
    """Lines that are a skill `!` preload: an inline !`cmd` or a ```! fence."""
    return [
        line.strip()
        for line in text.splitlines()
        if _INLINE_PRELOAD_RE.search(line) or _PRELOAD_FENCE_OPEN_RE.match(line)
    ]


def named_verbs(text: str) -> set[str]:
    return set(_CALL_RE.findall(text))


def verbs_not_accepted(text: str, accepted: frozenset[str]) -> set[str]:
    return named_verbs(text) - accepted


def verbs_not_documented(text: str, accepted: frozenset[str]) -> set[str]:
    return set(accepted) - named_verbs(text)


def calls_without_session(text: str) -> list[str]:
    """Lines that name a verb's call but do not show --session."""
    return [
        line.strip()
        for line in text.splitlines()
        if _CALL_RE.search(line) and "--session" not in line
    ]


_ROOT_VAR = "${CLAUDE_PLUGIN_ROOT}"
#: What Claude Code writes for ${CLAUDE_PLUGIN_ROOT} in a plugin skill (a sample cache path).
SAMPLE_ROOT = "/home/u/.claude/plugins/cache/plugins-kit/llm-kit/1.2.3"


def substitute(text: str) -> str:
    """The skill text as Claude sees it after the harness substitutes the plugin root."""
    return text.replace(_ROOT_VAR, SAMPLE_ROOT)


def command_word_problems(text: str) -> list[str]:
    """The command example (the line carrying ``relay client <verb>``) must start with the
    shim path and, once substituted, hold no shell expansion (``$``, braces, backtick, ``~``)
    that makes Claude Code ask before running it."""
    lines = [ln.strip() for ln in text.splitlines() if re.match(r"^\s*\$\{CLAUDE_PLUGIN_ROOT\}/\S+ relay client", ln)]
    if not lines:
        return ["no command example starts with ${CLAUDE_PLUGIN_ROOT}/bin/llm-kit relay client"]
    problems = []
    for line in lines:
        word = substitute(line).split(" ", 1)[0]
        if not word.endswith("/bin/llm-kit"):
            problems.append(f"command word is not the bin/llm-kit shim: {word}")
        if re.search(r"[$`{}~]", word):
            problems.append(f"command word still has a shell expansion: {word}")
    if "CLAUDE_BOOTSTRAP_DATA_ROOT" in text:
        problems.append("the skill still tells Claude to expand CLAUDE_BOOTSTRAP_DATA_ROOT in the command")
    return problems


def allow_rule_problems(text: str) -> list[str]:
    """The allowed-tools rule must be a Bash prefix rule that covers the command example."""
    rule = frontmatter(text).get("allowed-tools", "")
    m = re.fullmatch(r"Bash\((\S+) relay client \*\)", rule)
    if not m:
        return [f"allowed-tools must be one Bash(<shim> relay client *) rule, found {rule!r}"]
    example = [ln.strip() for ln in text.splitlines() if re.match(r"^\s*\$\{CLAUDE_PLUGIN_ROOT\}/\S+ relay client", ln)]
    if not example or not all(ln.startswith(m.group(1) + " relay client ") for ln in example):
        return ["the allowed-tools rule does not cover the command example"]
    return []


def gate_parser():
    sys.path.insert(0, str(HOOKS))
    try:
        spec = importlib.util.spec_from_file_location("direct_gate_under_test", GATE_HOOK)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(HOOKS))
    return module


def gate_problems(text: str, gate) -> list[str]:
    """The command form the skill shows (substituted shim path + each documented verb) must be
    found by the gate's finder and parsed to that verb."""
    word = substitute(_ROOT_VAR + "/bin/llm-kit")
    template = [ln for ln in substitute(text).splitlines() if re.match(r"^\s*/\S+/bin/\S+ relay client <verb>", ln)]
    if not template:
        return ["no command example to check"]
    shim = template[0].split(" relay client", 1)[0].strip()
    problems = []
    for verb in sorted(named_verbs(text)):
        command = f"{shim} relay client {verb} --session S"
        args = gate.find_client_args(command)
        if len(args) != 1:
            problems.append(f"gate finds {len(args)} client calls in {command!r}")
            continue
        call = gate.parse_client_args(args[0], "m")
        if call is None or call.client_verb != verb:
            problems.append(f"gate does not parse verb {verb!r} from {command!r}")
    return problems or ([] if word.startswith("/") else ["substituted shim path is not absolute"])


def guardrail_problems(text: str) -> list[str]:
    return [] if "not a security boundary" in text else ["the skill does not say the gate is not a security boundary"]


def interpreter_problems(text: str) -> list[str]:
    problems = []
    if not text.isascii():
        problems.append("non-ASCII text")
    if re.search(r"\buv run python\b|(?<![\w/.-])python3?\s", text):
        problems.append("bare python or uv run python")
    return problems


@pytest.fixture(scope="module")
def skill_text() -> str:
    return SKILL.read_text(encoding="utf-8")


# -- the real skill ---------------------------------------------------------------------

def test_skill_is_not_model_invocable(skill_text: str) -> None:
    assert frontmatter_problems(skill_text) == []


def test_skill_has_no_preload(skill_text: str) -> None:
    assert preload_lines(skill_text) == []


def test_named_verbs_are_accepted_by_the_grant_hook(skill_text: str) -> None:
    accepted = grant_hook_verbs()
    assert accepted, "the grant hook exposes no client verbs"
    assert named_verbs(skill_text), "the skill names no `llm-kit relay client <verb>` call"
    assert verbs_not_accepted(skill_text, accepted) == set()


def test_every_grant_hook_verb_is_documented(skill_text: str) -> None:
    assert verbs_not_documented(skill_text, grant_hook_verbs()) == set()


def test_every_command_carries_the_session(skill_text: str) -> None:
    assert calls_without_session(skill_text) == []


def test_command_word_is_a_literal_path(skill_text: str) -> None:
    assert command_word_problems(skill_text) == []


def test_allow_rule_covers_the_command(skill_text: str) -> None:
    assert allow_rule_problems(skill_text) == []


def test_gate_recognizes_the_command_form(skill_text: str) -> None:
    gate = gate_parser()
    assert any("relay client" in ln for ln in substitute(skill_text).splitlines())
    assert gate_problems(skill_text, gate) == []
    # the substituted example is also found by the gate's own finder as a whole command
    example = substitute("${CLAUDE_PLUGIN_ROOT}/bin/llm-kit relay client machines --session S")
    assert gate.find_client_args(example) == [["machines", "--session", "S"]]


def test_skill_states_the_gate_is_a_guardrail(skill_text: str) -> None:
    assert guardrail_problems(skill_text) == []


def test_skill_is_ascii_and_has_no_bare_python(skill_text: str) -> None:
    assert interpreter_problems(skill_text) == []


# -- counterfactuals: each check fails when its property is removed ---------------------

GOOD = """---
name: direct
disable-model-invocation: true
user-invocable: true
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/bin/llm-kit relay client *)
---
Run `llm-kit relay client machines --session S`.
This gate is not a security boundary.

${CLAUDE_PLUGIN_ROOT}/bin/llm-kit relay client <verb> --session <session id> ...
"""


def test_counterfactual_frontmatter() -> None:
    assert frontmatter_problems(GOOD) == []
    assert frontmatter_problems(GOOD.replace("disable-model-invocation: true\n", "")) != []
    assert frontmatter_problems(GOOD.replace("disable-model-invocation: true", "disable-model-invocation: false")) != []
    assert frontmatter_problems(GOOD.replace("user-invocable: true\n", "")) != []
    assert frontmatter_problems(GOOD.replace("user-invocable: true", "user-invocable: false")) != []


def test_counterfactual_preload() -> None:
    assert preload_lines(GOOD) == []
    inline = GOOD + "!`llm-kit relay client machines --session S`\n"
    assert preload_lines(inline) != []
    fenced = GOOD + "```!\nllm-kit relay client machines --session S\n```\n"
    assert preload_lines(fenced) != []
    # inline code that merely shows a bang is not a preload
    assert preload_lines(GOOD + "prefix it with `!` to run it\n") == []


def test_counterfactual_verbs_not_accepted() -> None:
    accepted = grant_hook_verbs()
    assert verbs_not_accepted(GOOD, accepted) == set()
    assert verbs_not_accepted(GOOD.replace("machines", "lookup"), accepted) == {"lookup"}


def test_counterfactual_verbs_not_documented() -> None:
    accepted = grant_hook_verbs()
    assert verbs_not_documented(GOOD, accepted) == set(accepted) - {"machines"}
    # the real skill, with one verb's mentions removed, reports exactly that verb
    real = SKILL.read_text(encoding="utf-8").replace("relay client answer", "relay client")
    assert verbs_not_documented(real, accepted) == {"answer"}


def test_counterfactual_session_missing() -> None:
    assert calls_without_session(GOOD) == []
    assert calls_without_session(GOOD.replace(" --session S", "")) != []


def test_counterfactual_guardrail_statement() -> None:
    assert guardrail_problems(GOOD) == []
    assert guardrail_problems(GOOD.replace("not a security boundary", "enforced")) != []


def test_counterfactual_command_word() -> None:
    assert command_word_problems(GOOD) == []
    braced = GOOD.replace("${CLAUDE_PLUGIN_ROOT}/bin/llm-kit relay", '"${CLAUDE_BOOTSTRAP_DATA_ROOT:-$HOME/.claude/plugins/data}"/plugins-kit/llm-kit/.venv/bin/llm-kit relay')
    assert command_word_problems(braced) != []
    # a command word that is not the shim, or one with an expansion left in it, is a problem
    assert command_word_problems(GOOD.replace("/bin/llm-kit relay", "/bin/other relay")) != []
    assert command_word_problems(GOOD.replace("${CLAUDE_PLUGIN_ROOT}/bin/llm-kit relay", "$HOME/bin/llm-kit relay")) != []
    assert command_word_problems(GOOD.replace("\n${CLAUDE_PLUGIN_ROOT}/bin/llm-kit relay client <verb>", "\nllm-kit relay client <verb>")) != []


def test_counterfactual_allow_rule() -> None:
    assert allow_rule_problems(GOOD) == []
    assert allow_rule_problems(GOOD.replace("allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/bin/llm-kit relay client *)\n", "")) != []
    assert allow_rule_problems(GOOD.replace("llm-kit relay client *)", "llm-kit *)")) != []
    wrong = GOOD.replace("allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/bin/llm-kit", "allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/bin/elsewhere")
    assert allow_rule_problems(wrong) != []


def test_counterfactual_gate_form() -> None:
    gate = gate_parser()
    assert gate_problems(GOOD, gate) == []
    # a command word the gate's tool pattern rejects is not seen as a relay client call
    assert gate_problems(GOOD.replace("/bin/llm-kit relay", "/bin/llmkit relay"), gate) != []


def test_counterfactual_interpreter_and_ascii() -> None:
    assert interpreter_problems(GOOD) == []
    assert interpreter_problems(GOOD + "run uv run python x.py\n") != []
    assert interpreter_problems(GOOD + "run python3 x.py\n") != []
    assert interpreter_problems(GOOD + "an em dash \u2014\n") != []
