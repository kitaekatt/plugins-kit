"""Static guards for the presence-kit skills afk, autonomous and recap."""
import re
from pathlib import Path

import pytest
import yaml

PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "presence-kit"
SKILLS = PLUGIN / "skills"
NAMES = ("afk", "autonomous", "recap")

DESCRIPTIONS = {
    "afk": (
        "Use when the user says afk or steps away, even with nothing running: "
        "cover jobs, park questions, recap. Do NOT use for a hand-over "
        "(presence-kit:autonomous)."
    ),
    "autonomous": (
        "Use when the user hands work over to finish without them: decide "
        "instead of asking, recap at the end. Do NOT use for a plain afk or a "
        "quiet user."
    ),
    "recap": (
        "Use when the user asks for a recap, returns from afk, or an afk or "
        "autonomous run ends: a recap fit to its trigger. Do NOT use to "
        "summarize documents or code."
    ),
}
LENGTHS = {"afk": 157, "autonomous": 145, "recap": 158}

MODEL_REFERENCE = "${CLAUDE_PLUGIN_ROOT}/references/presence.md"
# Technique-wrapper skills: SKILL.md is a stub whose single step points at a
# procedure document in references/ (skill-standards 3.3). The commands live in
# the procedure, a reference document, where the harness substitutes nothing, so
# they name the plugin root in the agent-typed form.
WRAPPERS = {
    "afk": "afk-procedure.md",
    "autonomous": "autonomous-procedure.md",
    "recap": "recap-procedure.md",
}
WRAPPER_ROOT = '"${PRESENCE_KIT_ROOT:?presence-kit is not provisioned}/scripts/presence.py"'
HARNESS_ROOT = '"${CLAUDE_PLUGIN_ROOT}/scripts/presence.py"'
CONFIG_COMMAND = (
    '"${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" '
    '"${CLAUDE_PLUGIN_ROOT}/scripts/presence.py" config'
)
# Commands (not prose mentions) that change state; the config command precedes them.
STATE_CHANGERS = (
    '"${CLAUDE_PLUGIN_ROOT}/scripts/presence.py" state set',
    '"${CLAUDE_PLUGIN_ROOT}/scripts/presence.py" recap-done',
)

# contracts.md section 9, "Trigger" table: (trigger cell, window, phase 1, ends with).
RECAP_ROWS = (
    (
        "bare, user present",
        "recent turns, widened if thin",
        '"What got done", "Decisions I need from you" (text only)',
        "nothing (not an away row)",
    ),
    (
        "`return`, `greeted` false",
        "since `afk_since`",
        "finished, decided for you, waiting on you, and still running when owned work runs",
        "`presence.py recap-done return <pid>`",
    ),
    (
        "`return`, `greeted` true",
        "--",
        "none; the standing recap is not repeated",
        "`presence.py recap-done return <pid>`",
    ),
    (
        "`afk-ending` (nothing owned)",
        "since `afk_since`",
        "finished, decided for you, waiting on you; read cold",
        "`presence.py recap-done afk-ending <pid>`",
    ),
    (
        "`autonomous-done`",
        "since `goal_since`",
        "outcome against `goal`, each load-bearing decision, waiting on you",
        "`presence.py recap-done autonomous-done <pid>`",
    ),
)
AWAY_TRIGGERS = ("return", "afk-ending", "autonomous-done")


def _text(name: str) -> str:
    return (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")


def _split(name: str) -> tuple[dict, str]:
    text = _text(name)
    assert text.startswith("---\n"), f"{name}: no frontmatter"
    _, front, body = text.split("---", 2)
    return yaml.safe_load(front), body


def _contract(name: str) -> dict:
    _, body = _split(name)
    blocks = re.findall(r"```yaml\n(.*?)\n```", body, flags=re.DOTALL)
    assert len(blocks) == 1, f"{name}: expected one yaml contract block"
    return yaml.safe_load(blocks[0])


def _references(name: str) -> dict[str, str]:
    """The skill's reference documents by file name (empty when it has none)."""
    folder = SKILLS / name / "references"
    if not folder.is_dir():
        return {}
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(folder.glob("*.md"))}


def _steps_text(name: str) -> str:
    """Where the skill's commands live: the procedure document for a wrapper, else the body."""
    if name in WRAPPERS:
        return _references(name)[WRAPPERS[name]]
    return _split(name)[1]


def _root(name: str) -> str:
    return WRAPPER_ROOT if name in WRAPPERS else HARNESS_ROOT


def _table_rows(body: str) -> list[list[str]]:
    rows = []
    for line in body.splitlines():
        if line.startswith("|") and not set(line) <= set("|- "):
            rows.append([cell.strip() for cell in line.strip().strip("|").split("|")])
    return rows


def test_all_three_skills_exist():
    # The parametrized tests below read these files; an empty skills
    # directory must fail here instead of passing vacuously.
    assert sorted(p.name for p in SKILLS.iterdir() if p.is_dir()) == sorted(NAMES)
    for name in NAMES:
        assert (SKILLS / name / "SKILL.md").is_file()


@pytest.mark.parametrize("name", NAMES)
def test_frontmatter_name(name):
    frontmatter, _ = _split(name)
    assert frontmatter["name"] == name


@pytest.mark.parametrize("name", NAMES)
def test_description_is_exact(name):
    frontmatter, _ = _split(name)
    assert frontmatter["description"] == DESCRIPTIONS[name]
    assert len(frontmatter["description"]) == LENGTHS[name]


@pytest.mark.parametrize("name", NAMES)
def test_description_meets_skill_standards(name):
    description = _split(name)[0]["description"]
    assert len(description) <= 160
    assert description.startswith("Use when")
    # skills-kit's mechanical criterion (audit.py _eval_desc_exclusion) accepts
    # "Do NOT use"; the recap description is "Do NOT use to summarize ...".
    assert "Do NOT use" in description


@pytest.mark.parametrize("name", NAMES)
def test_files_are_ascii(name):
    _text(name).encode("ascii")


@pytest.mark.parametrize("name", NAMES)
def test_reference_files_are_ascii(name):
    for text in _references(name).values():
        text.encode("ascii")


@pytest.mark.parametrize("name", NAMES)
def test_body_cites_the_model_reference(name):
    _, body = _split(name)
    assert MODEL_REFERENCE in body


@pytest.mark.parametrize("name", NAMES)
def test_config_runs_before_any_state_change(name):
    body = _steps_text(name)
    config = CONFIG_COMMAND.replace(HARNESS_ROOT, _root(name))
    assert config in body
    first_config = body.index(config)
    swapped = [word.replace(HARNESS_ROOT, _root(name)) for word in STATE_CHANGERS]
    changers = [body.index(word) for word in swapped if word in body]
    assert changers, f"{name}: no state-changing command found"
    assert first_config < min(changers)


@pytest.mark.parametrize("name", NAMES)
def test_commands_use_the_interpreter_variable_and_harness_root(name):
    _, body = _split(name)
    text = _steps_text(name)
    commands = [line for line in text.splitlines() if "presence.py" in line and "scripts/" in line]
    assert commands
    for line in commands:
        assert '"${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}"' in line, line
        assert _root(name) in line, line
    if name in WRAPPERS:
        # a wrapper's body carries no command at all
        assert "presence.py" not in body
    else:
        assert "PRESENCE_KIT_ROOT" not in body
    assert not re.search(r"(?<![\w{}:?-])(python3?|py)\s+\S*presence\.py", body)


@pytest.mark.parametrize("name", NAMES)
def test_contract_block_parses_with_ordered_steps(name):
    if name in WRAPPERS:
        # a wrapper is a short prose stub with one numbered step and no contract block
        _, body = _split(name)
        assert "```yaml" not in body
        assert re.search(r"(?m)^1\. Follow ", body)
        return
    contract = _contract(name)["technique_skill"]
    assert contract["techniques"]
    for technique in contract["techniques"]:
        numbers = [step["n"] for step in technique["steps"]]
        assert numbers == list(range(1, len(numbers) + 1))


@pytest.mark.parametrize("name", NAMES)
def test_skills_are_named_in_full_plugin_skill_form(name):
    _, body = _split(name)
    # A bare "/afk", "/autonomous" or "/recap" would be the built-in or nothing.
    assert not re.search(r"(?<![\w:-])/(afk|autonomous|recap)\b", body)


@pytest.mark.parametrize(
    "name, state_set, behavior",
    (
        ("afk", "state set afk", "afk-on-departure"),
        ("autonomous", "state set autonomous", "autonomous-on-handover"),
    ),
)
def test_by_flag_paths(name, state_set, behavior):
    body = _steps_text(name)
    # The two commands are separate lines, so one path cannot be dropped
    # while the other keeps the test green.
    user_lines = [l for l in body.splitlines() if f"{state_set} --by user" in l]
    model_lines = [l for l in body.splitlines() if f"{state_set} --by model" in l]
    assert len(user_lines) == 1
    assert len(model_lines) == 1
    assert "/presence-kit:" + name in body
    assert behavior in body
    assert "skipped" in body


def test_autonomous_passes_the_goal_and_can_revoke():
    body = _steps_text("autonomous")
    assert body.count('--goal "<granted goal>"') == 2
    assert "state set normal" in body


def test_afk_uses_the_configured_waiter_model():
    body = _steps_text("afk")
    assert "values.waiter_model" in body


def test_autonomous_uses_the_configured_waiter_model():
    body = _steps_text("autonomous")
    assert "values.waiter_model" in body


def test_recap_contains_every_trigger_row():
    rows = _table_rows(_references("recap")["recap-rows.md"])
    for expected in RECAP_ROWS:
        assert list(expected) in rows, f"missing trigger row: {expected[0]}"


def test_recap_records_each_away_trigger():
    body = _steps_text("recap")
    away_rows = [
        row for row in _table_rows(_references("recap")["recap-rows.md"]) if "recap-done" in row[3]
    ]
    assert [row[3] for row in away_rows] == [
        f"`presence.py recap-done {trigger} <pid>`"
        for trigger in ("return", "return", "afk-ending", "autonomous-done")
    ]
    # The one command template every away row runs stays in the body.
    assert (
        '"${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" '
        + WRAPPER_ROOT
        + " recap-done <trigger> <pid>"
    ) in body


def test_recap_ports_the_awesome_kit_phases():
    refs = _references("recap")
    templates, interview = refs["recap-templates.md"], refs["recap-interview.md"]
    for heading in (
        "## Phase 1 -- the recap: text only, NO AskUserQuestion",
        "## What \"what got done\" means",
        "## Selecting decisions",
    ):
        assert heading in templates
    assert "# Phase 2 -- on a neutral reply: interview" in interview
    assert "No decisions pending." in templates
    assert "ONE AskUserQuestion call carrying every pending decision (max 4)" in interview


@pytest.mark.parametrize(
    "name, files",
    (
        (
            "recap",
            ("recap-procedure.md", "recap-rows.md", "recap-templates.md", "recap-interview.md"),
        ),
        ("afk", ("afk-procedure.md",)),
        ("autonomous", ("autonomous-procedure.md",)),
    ),
)
def test_repeat_invoked_skill_body_points_at_its_references(name, files):
    """A technique-wrapper keeps procedure and content out of its body (skill-standards 3.3).

    The body cites each reference by its skill-dir path, the file exists, and the
    content the references carry is absent from the body: reverting the move
    (inlining a template or a table row back) turns this red.
    """
    _, body = _split(name)
    assert sorted(_references(name)) == sorted(files)
    # the stub stays short: that is the point of the form
    assert len(body.split()) <= 150, f"{name}: stub is {len(body.split())} words"
    # the wrapper's body points only at its procedure; the procedure points at the rest
    assert f"${{CLAUDE_SKILL_DIR}}/references/{WRAPPERS[name]}" in body
    procedure = _steps_text(name)
    for file in files:
        if file != WRAPPERS[name]:
            assert file in procedure
    for moved in (
        "Read the configuration",
        "recap-done <trigger>",
        "What got done",
        "Decided for you",
        "No decisions pending.",
        "(Recommended)",
        "| Trigger | Window |",
    ):
        assert moved not in body, f"{name}: '{moved}' belongs in references/"


def test_recap_names_its_behavior_ids_and_state_reads():
    body = _steps_text("recap")
    for token in ("recap-on-return", "ending-recap", "unresolved", "greeted", "state get"):
        assert token in body


def test_afk_undoes_a_hook_set_afk_when_the_user_is_not_leaving():
    """K3: the prompt hook sets afk itself; step 2 of the procedure is the undo branch (`state set present`)."""
    procedure = _steps_text("afk")
    step = procedure[procedure.index("2. **Decide whether to continue."):procedure.index("3. **Only when")]
    action = " ".join(step.split())
    assert 'beginning "set the user afk" or "kept the user afk"' in action
    assert "id `afk-on-departure`" in action
    assert "the user is not leaving" in action and "undo it" in action
    # the undo is the only place the skill unsets afk, and it sits in step 2
    commands = [l for l in procedure.splitlines() if "state set present" in l]
    assert len(commands) == 1
    assert "state set present" in step
    # the config still runs first
    config = CONFIG_COMMAND.replace(HARNESS_ROOT, WRAPPER_ROOT)
    assert procedure.index(config) < procedure.index("state set present")
    # and the stub itself carries no command, so the undo cannot be reached around the procedure
    assert "state set present" not in _split("afk")[1]


def test_afk_description_is_unchanged_by_the_prompt_hook():
    """Ruling 17 fixed the description text; K3 must not touch it."""
    front, _ = _split("afk")
    assert front["description"] == DESCRIPTIONS["afk"]
