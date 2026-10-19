"""presence-kit prompt hook: scripts/presence_prompt.py and the `prompt` verb of presence.py.

Contract: dev/tasks/presence-kit/contracts.md sections 1, 2, 5 and 7; design.md "Hooks"
(UserPromptSubmit). The real config, state and prompt modules run against a temp data
root (CLAUDE_BOOTSTRAP_DATA_ROOT) and a temp config dir (CLAUDE_CONFIG_DIR); nothing
touches the real ~/.claude. Time is injected through `now`; nothing sleeps. Expected
texts are restated literally (L) so a change to presence_config.py cannot silently
move them.

The hook WRITES state on exactly one input: a configured departure word while
`afk-on-departure` is on (rulings 18 and 19). Every other path is read-only.
"""
import io
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "presence-kit"
SCRIPTS = PLUGIN / "scripts"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(SCRIPTS))

import presence  # noqa: E402
import presence_config as pc  # noqa: E402
import presence_prompt as pprompt  # noqa: E402
import presence_state as ps  # noqa: E402

SID = "sess-prompt"
PID = "prompt-1"
NOW = datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc)
SINCE = ps.format_timestamp(NOW - timedelta(minutes=30))
CFG_HINT = "See references/configuring.md."
ABSENT = object()
AWAY_IDS = "terse-while-away,park-questions,notify-for-input"
UNDO = (
    '"${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" '
    '"${PRESENCE_KIT_ROOT:?presence-kit is not provisioned}/scripts/presence.py" '
    "state set present --session " + SID
)


def L(ids, pid, text):
    """A hook line, restated literally from contracts.md section 7."""
    return f"presence-kit [{ids}] prompt {pid}: {text} {CFG_HINT}"


def line_P1(since, pid=PID, goal=None):
    goal_part = "" if goal is None else f" [goal: {goal}]"
    return L(
        AWAY_IDS, pid,
        f"the user counts as away since {since}{goal_part}. "
        "Apply the away behaviors in references/presence.md that are on.",
    )


def line_P2(pid=PID):
    return L(
        "recap-on-return", pid,
        "if this prompt is the user's own message, not a scheduled task, agent report, "
        "task notification or another session's message, invoke "
        f"`presence-kit:recap return {pid}` before answering.",
    )


def line_P3(word, pid=PID):
    return L(
        "afk-on-departure", pid,
        f'set the user afk because the prompt contains the departure word "{word}". '
        "If that is wrong (the word is quoted or discussed, or the prompt is not the user's own "
        f"message), run `{UNDO}` and tell the user once in one line; "
        "otherwise invoke `presence-kit:afk` to cover running work.",
    )


def line_P3_kept(word, pid=PID, recap=True):
    clause = (
        f" (if it was the user's own message, they are back: also invoke `presence-kit:recap return {pid}`)"
        if recap else ""
    )
    return L(
        "afk-on-departure", pid,
        f'kept the user afk because the prompt contains the departure word "{word}". '
        "If that is wrong (the word is quoted or discussed, or the prompt is not the user's own "
        f"message), run `{UNDO}` and tell the user once in one line{clause}; "
        "otherwise invoke `presence-kit:afk` to cover running work.",
    )


def payload(prompt, pid=PID, **extra):
    obj = {
        "session_id": SID,
        "transcript_path": "<none>",
        "cwd": "<SCRATCH>",
        "prompt_id": pid,
        "permission_mode": "default",
        "hook_event_name": "UserPromptSubmit",
        "prompt": prompt,
    }
    obj.update(extra)
    return obj


class World:
    """A temp data root, config dir and project dir; evaluate() and run() share them."""

    def __init__(self, root: Path):
        self.root = root
        self.project = root / "proj"
        self.project.mkdir()
        self.env = {
            "CLAUDE_BOOTSTRAP_DATA_ROOT": str(root / "data"),
            "CLAUDE_CONFIG_DIR": str(root / "cfg"),
            "CLAUDE_PROJECT_DIR": str(self.project),
        }

    def config(self, behaviors=None, values=None, raw=None):
        target = self.root / "cfg" / "config"
        target.mkdir(parents=True, exist_ok=True)
        layer = {}
        if behaviors:
            layer["behaviors"] = behaviors
        if values:
            layer["values"] = values
        (target / "presence-kit.json").write_text(
            raw if raw is not None else json.dumps(layer), encoding="ascii"
        )

    def cfg(self):
        return pc.resolve(self.project, self.env)

    def evaluate(self, prompt, pid=PID, now=NOW, **extra):
        return pprompt.evaluate(payload(prompt, pid, **extra), self.cfg(), self.env, now)

    def run(self, *argv, stdin="", **env_over):
        env = dict(self.env)
        for key, value in env_over.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        out, err = io.StringIO(), io.StringIO()
        stream = stdin if hasattr(stdin, "read") else io.StringIO(stdin)
        rc = presence.main(list(argv), stdin=stream, stdout=out, stderr=err, env=env, now=NOW)
        return rc, out.getvalue(), err.getvalue()

    def hook(self, obj, **env_over):
        env_over.setdefault("CLAUDE_CODE_SESSION_ATTENDED", "1")
        rc, out, err = self.run("prompt", stdin=json.dumps(obj), **env_over)
        return rc, json.loads(out), err

    def state(self):
        return ps.load(SID, self.env)

    def state_path(self):
        return ps.state_path(SID, self.env)

    def go_afk(self, goal=None, afk_prompt=None):
        def fn(doc):
            ps.enter_afk(doc, SINCE, prompt=afk_prompt)
            if goal is not None:
                ps.enter_autonomous(doc, goal, SINCE)

        ps.update(SID, fn, self.env)


@pytest.fixture
def w(tmp_path):
    return World(tmp_path)


def context(output):
    return output.additional_context


# ============================================================ the departure write

def test_departure_word_writes_afk_state(w):
    """Fails if the write is reverted: ATTENDED, "going afk" -> afk, afk_prompt == pid, P3 then P1."""
    out = w.evaluate("going afk for lunch")
    doc = w.state()
    assert doc["presence"] == "afk"
    assert doc["afk_since"] == ps.format_timestamp(NOW)
    assert doc["afk_prompt"] == PID
    assert doc["greeted"] is False
    assert out.system_message is None
    assert context(out) == line_P3("afk") + "\n" + line_P1(ps.format_timestamp(NOW))


def test_departure_through_the_cli_prints_the_hook_object_and_writes(w):
    rc, obj, err = w.hook(payload("brb, going afk"))
    assert (rc, err) == (0, "")
    assert list(obj) == ["hookSpecificOutput"]
    assert obj["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert obj["hookSpecificOutput"]["additionalContext"] == (
        line_P3("afk") + "\n" + line_P1(ps.format_timestamp(NOW))
    )
    assert w.state()["afk_prompt"] == PID


def test_the_departure_word_is_reported_as_typed(w):
    out = w.evaluate("AFK.")
    assert context(out).startswith(line_P3("AFK"))


@pytest.mark.parametrize("text", ["afk", "AFK.", "(afk)", "Afk for a bit", "going afk, back at 3", "x\nafk\ny"])
def test_departure_forms_that_match(w, text):
    w.evaluate(text)
    assert w.state()["presence"] == "afk"


@pytest.mark.parametrize("text", [
    "what is afk-on-departure",
    "invoke presence-kit:afk",
    "the `afk` skill",
    "afk_since is a field",
    "afkish",
    "xafk",
    "see skills/afk/SKILL.md",
    "hello there",
    "",
])
def test_text_that_does_not_match(w, text):
    out = w.evaluate(text)
    assert context(out) is None
    assert not w.state_path().exists()


def test_quotes_and_questions_still_match_and_claude_corrects(w):
    """Documented limit: the hook does no inference; P3 carries the correction."""
    out = w.evaluate('what does the word "afk" do?')
    assert w.state()["presence"] == "afk"
    assert "run `" in context(out) and "state set present" in context(out)


def test_custom_departure_words(w):
    w.config(values={"departure_words": ["brb"]})
    assert context(w.evaluate("going afk")) is None
    assert w.state()["presence"] == "present"
    out = w.evaluate("BRB lunch")
    assert context(out).startswith(line_P3("BRB"))
    assert w.state()["presence"] == "afk"


def test_afk_on_departure_off_means_no_write_and_no_output(w):
    w.config(behaviors={"afk-on-departure": False})
    out = w.evaluate("going afk")
    assert out == pprompt.PromptOutput(None, None)
    assert not w.state_path().exists()


def test_afk_on_departure_off_still_gives_the_away_lines_while_afk(w):
    w.config(behaviors={"afk-on-departure": False})
    w.go_afk()
    out = w.evaluate("going afk")
    assert context(out) == line_P1(SINCE) + "\n" + line_P2()
    assert w.state()["afk_prompt"] is None


@pytest.mark.parametrize("prompt", [
    "/presence-kit:afk",
    "  /presence-kit:afk",
    "\n\t/presence-kit:recap afk-ending prompt-9",
    "/presence-kit:recap return afk",
    "/presence-kit:autonomous finish the afk work",
])
def test_the_plugins_own_commands_are_never_a_departure(w, prompt):
    out = w.evaluate(prompt)
    assert context(out) is None
    assert not w.state_path().exists()


def test_a_task_notification_containing_afk_is_not_a_departure(w):
    notification = json.loads((FIXTURES / "a1-04-UserPromptSubmit.json").read_text(encoding="ascii"))
    notification["prompt"] = notification["prompt"].replace(
        "completed (exit code 0)", "completed (exit code 0): the user is afk"
    )
    assert notification["prompt"].startswith("<task-notification>") and "afk" in notification["prompt"]
    obj = dict(notification, session_id=SID)
    out = pprompt.evaluate(obj, w.cfg(), w.env, NOW)
    assert context(out) is None
    assert not w.state_path().exists()


def test_the_prefix_exclusion_applies_only_at_the_start(w):
    w.evaluate("please run /presence-kit:afk now, I am going afk")
    assert w.state()["presence"] == "afk"


# ===================================================== already afk (ruling 19)

def test_departure_word_while_afk_keeps_afk(w):
    """Ruling 19: saying afk again keeps you afk; afk_since stays, afk_prompt moves, no P2."""
    w.go_afk(afk_prompt="old-prompt")
    out = w.evaluate("afk again", pid="prompt-2")
    doc = w.state()
    assert doc["presence"] == "afk"
    assert doc["afk_since"] == SINCE
    assert doc["afk_prompt"] == "prompt-2"
    assert context(out) == line_P3_kept("afk", "prompt-2") + "\n" + line_P1(SINCE, "prompt-2")
    assert line_P2("prompt-2") not in context(out)


def test_kept_line_has_no_return_clause_with_recap_on_return_off(w):
    w.config(behaviors={"recap-on-return": False})
    w.go_afk()
    out = w.evaluate("afk again")
    assert context(out) == line_P3_kept("afk", recap=False) + "\n" + line_P1(SINCE)
    assert "recap return" not in context(out)


def test_a_returning_user_who_only_mentions_afk_is_kept_afk_and_told_how_to_undo(w):
    """Cross-check fix 1: the hook keeps them afk; P3 tells Claude to run `state set present`."""
    w.go_afk(goal="ship it")
    out = w.evaluate("what does afk do exactly?", pid="back-1")
    assert w.state()["presence"] == "afk" and w.state()["afk_prompt"] == "back-1"
    assert "state set present --session " + SID in context(out)
    assert "they are back: also invoke `presence-kit:recap return back-1`" in context(out)
    assert line_P1(SINCE, "back-1", goal="ship it") in context(out)


def test_no_p2_on_the_prompt_the_hook_set_afk_on(w):
    """A mid-turn notification repeats the pid: the second call has P1 and no P2."""
    w.evaluate("going afk")
    again = w.evaluate("<task-notification>done</task-notification>")
    assert context(again) == line_P1(ps.format_timestamp(NOW))
    other = w.evaluate("hello", pid="prompt-2")
    assert context(other) == line_P1(ps.format_timestamp(NOW), "prompt-2") + "\n" + line_P2("prompt-2")


def test_departure_is_idempotent_for_the_afk_skills_own_write(w):
    """The afk skill's `state set afk` on the same prompt must not clear afk_prompt."""
    w.evaluate("going afk")
    before = w.state()
    ps.state_set(SID, "afk", "model", None, {"afk-on-departure": True}, w.env, NOW + timedelta(minutes=1))
    assert w.state() == before


# ======================================================== the read-only paths

def test_present_without_a_departure_is_silent_and_writes_nothing(w):
    out = w.evaluate("go")
    assert out == pprompt.PromptOutput(None, None)
    assert out.to_output() == {}
    assert not w.state_path().exists()


def test_away_line_with_goal(w):
    w.go_afk(goal="finish the migration")
    out = w.evaluate("status?")
    assert context(out).split("\n")[0] == line_P1(SINCE, goal="finish the migration")


def test_away_lines_are_read_only_and_ordered_p1_then_p2(w):
    w.go_afk()
    path = w.state_path()
    before = path.read_bytes()
    out = w.evaluate("hello")
    assert context(out) == line_P1(SINCE) + "\n" + line_P2()
    assert path.read_bytes() == before


def test_recap_clause_off_gives_only_the_away_line(w):
    w.config(behaviors={"recap-on-return": False})
    w.go_afk()
    assert context(w.evaluate("hello")) == line_P1(SINCE)


def test_afk_without_afk_since_fails_loudly(w):
    path = w.state_path()
    path.parent.mkdir(parents=True)
    doc = ps.default_state()
    doc["presence"] = "afk"
    path.write_text(json.dumps(doc), encoding="ascii")
    with pytest.raises(ps.StateError) as err:
        w.evaluate("hello")
    assert err.value.kind == "malformed" and "afk_since is null" in err.value.text


@pytest.mark.parametrize("value", [ABSENT, None, 7, ["afk"]])
def test_a_missing_or_non_string_prompt_is_empty(w, value):
    obj = payload("x")
    if value is ABSENT:
        del obj["prompt"]
    else:
        obj["prompt"] = value
    assert context(pprompt.evaluate(obj, w.cfg(), w.env, NOW)) is None


def test_bad_ids_raise_state_errors(w):
    with pytest.raises(ps.StateError) as err:
        pprompt.evaluate(payload("x", session_id="a/b"), w.cfg(), w.env, NOW)
    assert err.value.kind == "bad-session-id"
    with pytest.raises(ps.StateError) as err:
        pprompt.evaluate(payload("x", pid="a b"), w.cfg(), w.env, NOW)
    assert err.value.kind == "bad-prompt-id"


def test_evaluate_makes_one_state_update_per_prompt(w, monkeypatch):
    calls = []
    real = ps.update
    monkeypatch.setattr(ps, "update", lambda *a, **k: (calls.append(a), real(*a, **k))[1])
    w.evaluate("going afk")
    w.evaluate("hello", pid="p2")
    assert len(calls) == 2


def test_output_shape():
    both = pprompt.PromptOutput("ctx", "msg").to_output()
    assert list(both) == ["systemMessage", "hookSpecificOutput"]
    assert both["hookSpecificOutput"] == {"hookEventName": "UserPromptSubmit", "additionalContext": "ctx"}
    assert pprompt.PromptOutput("ctx", None).to_output() == {
        "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "ctx"}
    }
    assert pprompt.PromptOutput(None, "msg").to_output() == {"systemMessage": "msg"}
    assert pprompt.PromptOutput(None, None).to_output() == {}


# ============================================================ the CLI verb

def test_nothing_to_say_prints_an_empty_object(w):
    rc, out, err = w.run("prompt", stdin=json.dumps(payload("hello")), CLAUDE_CODE_SESSION_ATTENDED="1")
    assert (rc, out, err) == (0, "{}\n", "")


@pytest.mark.parametrize("attended", [None, "0", "", "true", " 1", "1 "])
def test_unattended_prints_an_empty_object_and_touches_nothing(w, attended):
    """Gate: only exactly `1` acts. No stdin read, no file read or written."""

    class NoRead:
        def read(self):
            raise AssertionError("stdin was read while unattended")

    rc, out, err = w.run("prompt", stdin=NoRead(), CLAUDE_CODE_SESSION_ATTENDED=attended)
    assert (rc, out, err) == (0, "{}\n", "")
    assert not (w.root / "data").exists()


def test_unattended_gate_precedes_payload_errors(w):
    rc, out, err = w.run("prompt", stdin="not json", CLAUDE_CODE_SESSION_ATTENDED="0")
    assert (rc, out, err) == (0, "{}\n", "")


def test_extra_argv_is_checked_before_the_gate(w):
    for attended in ("1", "0", None):
        rc, out, err = w.run("prompt", "now", "--force", CLAUDE_CODE_SESSION_ATTENDED=attended)
        assert rc == 1
        obj = json.loads(out)
        assert obj == {"error": "usage", "kind": "extra-argv", "message": "prompt takes no arguments; got now --force"}
        assert err == "prompt takes no arguments; got now --force\n"


def test_help_flag_is_extra_argv_not_argparse(w):
    rc, out, err = w.run("prompt", "--help", CLAUDE_CODE_SESSION_ATTENDED="1")
    assert rc == 1 and json.loads(out)["kind"] == "extra-argv"


@pytest.mark.parametrize("raw, kind, message", [
    ("", "empty", "hook payload is empty"),
    ("[1]", "not-object", "hook payload must be a JSON object, got array"),
    ("null", "not-object", "hook payload must be a JSON object, got null"),
    ('{"prompt_id": "p"}', "missing-field", "hook payload field 'session_id' must be a non-empty string"),
    ('{"session_id": "s"}', "missing-field", "hook payload field 'prompt_id' must be a non-empty string"),
    ('{"session_id": "a/b", "prompt_id": "p"}', "bad-session-id", "session id 'a/b' must match [A-Za-z0-9_-]{1,128}"),
    ('{"session_id": "s", "prompt_id": "a b"}', "bad-prompt-id", "prompt id 'a b' must be 1-128 characters without whitespace"),
])
def test_payload_errors_are_error_objects_with_exit_1(w, raw, kind, message):
    rc, out, err = w.run("prompt", stdin=raw, CLAUDE_CODE_SESSION_ATTENDED="1")
    assert rc == 1
    assert json.loads(out) == {"error": "payload", "kind": kind, "message": message}
    assert err == message + "\n"
    assert not (w.root / "data").exists()


def test_malformed_json_is_a_payload_error(w):
    rc, out, err = w.run("prompt", stdin="{nope", CLAUDE_CODE_SESSION_ATTENDED="1")
    assert rc == 1
    assert json.loads(out)["kind"] == "malformed-json" and err.startswith("hook payload is not valid JSON -- ")


def test_config_error_reports_to_the_user_and_claude_and_the_prompt_proceeds(w):
    """P4 to the user, P5 to Claude; exit 0; no state change even for a departure word."""
    w.config(raw="{not json")
    rc, obj, err = w.hook(payload("going afk"))
    assert (rc, err) == (0, "")
    assert list(obj) == ["systemMessage", "hookSpecificOutput"]
    path = w.root / "cfg" / "config" / "presence-kit.json"
    assert obj["systemMessage"].startswith(f"presence-kit [config] prompt {PID}: {path}: malformed JSON -- ")
    assert obj["systemMessage"].endswith("Presence behavior is unavailable until the file is fixed. " + CFG_HINT)
    assert obj["hookSpecificOutput"] == {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": L(
            "config", PID,
            "presence behavior is unavailable because its configuration is invalid; "
            "apply no presence-kit rule this turn.",
        ),
    }
    assert not w.state_path().exists()


def test_a_malformed_state_file_is_a_state_error_and_is_not_repaired(w):
    path = w.state_path()
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="ascii")
    rc, out, err = w.run("prompt", stdin=json.dumps(payload("hello")), CLAUDE_CODE_SESSION_ATTENDED="1")
    assert rc == 1
    assert json.loads(out)["error"] == "state" and "malformed state file" in err
    assert path.read_text(encoding="ascii") == "{broken"


def test_unexpected_exception_is_exit_1_with_an_internal_object(w, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(presence.pprompt, "evaluate", boom)
    rc, out, err = w.run("prompt", stdin=json.dumps(payload("hi")), CLAUDE_CODE_SESSION_ATTENDED="1")
    assert rc == 1
    assert json.loads(out) == {"error": "internal", "kind": "unexpected", "message": "RuntimeError: boom"}
    assert "Traceback" in err


def test_prompt_never_exits_2(w):
    for raw in ("", "{", "[]", '{"session_id": 1}', "{}"):
        assert w.run("prompt", stdin=raw, CLAUDE_CODE_SESSION_ATTENDED="1")[0] == 1


@pytest.mark.parametrize("source", ["env", "payload-cwd"])
def test_project_dir_sources(w, tmp_path, source):
    """CLAUDE_PROJECT_DIR, else the payload's cwd, decides which project layer applies."""
    other = tmp_path / "other"
    (other / ".claude").mkdir(parents=True)
    (other / ".claude" / "presence-kit.json").write_text(
        json.dumps({"values": {"departure_words": ["brb"]}}), encoding="ascii"
    )
    if source == "env":
        rc, obj, _ = w.hook(payload("brb"), CLAUDE_PROJECT_DIR=str(other))
    else:
        rc, obj, _ = w.hook(payload("brb", cwd=str(other)), CLAUDE_PROJECT_DIR=None)
    assert rc == 0 and "afk-on-departure" in obj["hookSpecificOutput"]["additionalContext"]
    rc, obj, _ = w.hook(payload("brb", pid="p-other", session_id="other-sess"))   # default project: "brb" is no word
    assert obj == {}


# ================================================ the undo command, run under bash

def _bash():
    found = shutil.which("bash")
    return found


@pytest.mark.skipif(_bash() is None, reason="no bash available")
def test_the_undo_command_in_p3_runs_under_bash_and_restores_presence(w, tmp_path):
    """The exact command text P3 carries, run with both variables set (cross-check fix 5)."""
    out = w.evaluate("going afk")
    command = context(out).split("run `", 1)[1].split("` and tell the user", 1)[0]
    assert command == UNDO
    assert w.state()["presence"] == "afk"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE_", "BOOTSTRAP_", "PRESENCE_KIT_"))}
    env.update(
        BOOTSTRAP_PYTHON=sys.executable,
        PRESENCE_KIT_ROOT=str(PLUGIN),
        CLAUDE_BOOTSTRAP_DATA_ROOT=w.env["CLAUDE_BOOTSTRAP_DATA_ROOT"],
        CLAUDE_CONFIG_DIR=w.env["CLAUDE_CONFIG_DIR"],
    )
    run = subprocess.run([_bash(), "-c", command], capture_output=True, text=True, env=env, cwd=str(tmp_path), timeout=60)
    assert run.returncode == 0, run.stderr
    reply = json.loads(run.stdout)
    assert reply["written"] is True and reply["by"] == "user"
    doc = w.state()
    assert doc["presence"] == "present" and doc["afk_prompt"] is None and doc["pending_return"] is None
    assert doc["afk_since"] == ps.format_timestamp(NOW)   # kept for the recap window


@pytest.mark.skipif(_bash() is None, reason="no bash available")
@pytest.mark.parametrize("missing", ["BOOTSTRAP_PYTHON", "PRESENCE_KIT_ROOT"])
def test_the_undo_command_fails_loudly_when_a_variable_is_unset(w, tmp_path, missing):
    w.evaluate("going afk")
    command = UNDO
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE_", "BOOTSTRAP_", "PRESENCE_KIT_"))}
    env.update(BOOTSTRAP_PYTHON=sys.executable, PRESENCE_KIT_ROOT=str(PLUGIN),
               CLAUDE_BOOTSTRAP_DATA_ROOT=w.env["CLAUDE_BOOTSTRAP_DATA_ROOT"])
    del env[missing]
    run = subprocess.run([_bash(), "-c", command], capture_output=True, text=True, env=env, cwd=str(tmp_path), timeout=60)
    assert run.returncode != 0
    assert missing in run.stderr
    assert w.state()["presence"] == "afk"


# ===================================================== subprocess, the real script

def _clean_env(root, **extra):
    env = {k: v for k, v in os.environ.items()
           if not (k.startswith("CLAUDE_") or k.startswith("BOOTSTRAP_") or k in ("HOME", "USERPROFILE"))}
    env.update(
        CLAUDE_BOOTSTRAP_DATA_ROOT=str(root / "data"),
        CLAUDE_CONFIG_DIR=str(root / "cfg"),
        CLAUDE_PROJECT_DIR=str(root / "proj"),
        HOME=str(root / "home"),
        USERPROFILE=str(root / "home"),
    )
    env.update(extra)
    return env


@pytest.mark.parametrize("attended", ["1", "0", None])
def test_subprocess_prompt_with_the_attended_gate(tmp_path, attended):
    (tmp_path / "proj").mkdir()
    extra = {} if attended is None else {"CLAUDE_CODE_SESSION_ATTENDED": attended}
    obj = payload("going afk")
    run = subprocess.run(
        [sys.executable, str(SCRIPTS / "presence.py"), "prompt"], input=json.dumps(obj),
        capture_output=True, text=True, env=_clean_env(tmp_path, **extra), cwd=str(tmp_path), timeout=60,
    )
    state_file = tmp_path / "data" / "plugins-kit" / "presence-kit" / "sessions" / f"{SID}.json"
    assert run.returncode == 0 and run.stderr == ""
    out = json.loads(run.stdout)
    if attended == "1":
        assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
        assert "afk-on-departure" in out["hookSpecificOutput"]["additionalContext"]
        assert json.loads(state_file.read_text(encoding="ascii"))["afk_prompt"] == PID
    else:
        assert out == {}
        assert not (tmp_path / "data").exists()


def test_subprocess_prompt_reads_utf8_bytes(tmp_path):
    (tmp_path / "proj").mkdir()
    obj = payload("caf" + chr(0xE9) + " " + chr(0x2713) + " and going afk")
    run = subprocess.run(
        [sys.executable, str(SCRIPTS / "presence.py"), "prompt"],
        input=json.dumps(obj, ensure_ascii=False).encode("utf-8"),
        capture_output=True, env=_clean_env(tmp_path, CLAUDE_CODE_SESSION_ATTENDED="1"), cwd=str(tmp_path), timeout=60,
    )
    assert run.returncode == 0, run.stderr
    assert "afk-on-departure" in json.loads(run.stdout.decode("ascii"))["hookSpecificOutput"]["additionalContext"]
