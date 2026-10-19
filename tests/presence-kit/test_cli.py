"""presence-kit CLI: scripts/presence.py (config, state get|set, recap-done, stop).

Contract: dev/tasks/presence-kit/contracts.md sections 2, 5 and 11. The real K2a
(config), K2b (state) and K2d (stop) modules run against a temp data root
(CLAUDE_BOOTSTRAP_DATA_ROOT) and a temp config dir (CLAUDE_CONFIG_DIR); nothing
touches the real ~/.claude. In-process tests call `main(...)` with an injected
env, streams and clock; the subprocess tests at the end run the script itself
through the real fixture payloads with CLAUDE_CODE_SESSION_ATTENDED 1 / 0 / unset.

Expected strings are restated literally (L) so a change to a shared module
cannot silently move them. The `prompt` verb and its hook are unit K3: here it
is an unknown verb (usage error, exit 2).
"""
import copy
import io
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "plugins" / "presence-kit" / "scripts"
SCRIPT = SCRIPTS / "presence.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(SCRIPTS))

import presence  # noqa: E402
import presence_state as ps  # noqa: E402

BEHAVIOR_IDS = (
    "afk-on-departure", "afk-on-quiet", "autonomous-on-handover", "recap-on-return",
    "waiting-guard", "ending-recap", "terse-while-away", "park-questions",
    "notify-for-input",
)
AWAY_IDS = ("terse-while-away", "park-questions", "notify-for-input")
SID = "cli-sess"
T0 = datetime(2026, 10, 17, 12, 0, 0, tzinfo=timezone.utc)
CFG_HINT = "See references/configuring.md."


def L(ids, pid, text):
    """A hook line, restated literally from contracts.md section 7."""
    return f"presence-kit [{ids}] prompt {pid}: {text} {CFG_HINT}"


def stamp(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


class Result:
    def __init__(self, rc, out, err):
        self.rc, self.out, self.err = rc, out, err

    @property
    def obj(self):
        assert self.out.endswith("\n") and self.out.count("\n") == 1, repr(self.out)
        assert self.out.isascii()
        return json.loads(self.out)


class Env:
    """A temp world: data root, config dir, project dir; run() calls main in process."""

    def __init__(self, root):
        self.root = root
        self.project = root / "proj"
        self.project.mkdir()
        self.env = {
            "CLAUDE_BOOTSTRAP_DATA_ROOT": str(root / "data"),
            "CLAUDE_CONFIG_DIR": str(root / "cfg"),
            "CLAUDE_PROJECT_DIR": str(self.project),
        }

    def config(self, behaviors=None, values=None, raw=None, where="user"):
        target = (self.root / "cfg" / "config" if where == "user" else self.project / ".claude")
        target.mkdir(parents=True, exist_ok=True)
        layer = {}
        if behaviors:
            layer["behaviors"] = behaviors
        if values:
            layer["values"] = values
        (target / "presence-kit.json").write_text(
            raw if raw is not None else json.dumps(layer), encoding="ascii"
        )

    def run(self, *argv, stdin="", now=T0, **env_over):
        env = dict(self.env)
        for key, value in env_over.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        out, err = io.StringIO(), io.StringIO()
        stream = stdin if hasattr(stdin, "read") else io.StringIO(stdin)
        rc = presence.main(list(argv), stdin=stream, stdout=out, stderr=err, env=env, now=now)
        return Result(rc, out.getvalue(), err.getvalue())

    def state(self, sid=SID):
        return ps.load(sid, self.env)

    def state_file(self, sid=SID):
        return ps.state_path(sid, self.env)


@pytest.fixture
def world(tmp_path):
    return Env(tmp_path)


def error_of(res, rc, error, kind, message=None):
    """One failure object on stdout, the same text on stderr."""
    assert res.rc == rc, (res.rc, res.out, res.err)
    obj = res.obj
    assert list(obj) == ["error", "kind", "message"]
    assert (obj["error"], obj["kind"]) == (error, kind), obj
    if message is not None:
        assert obj["message"] == message
    assert res.err.endswith(obj["message"] + "\n")
    return obj


# --------------------------------------------------------------------- config

def test_config_defaults(world):
    res = world.run("config")
    assert res.rc == 0 and res.err == ""
    obj = res.obj
    assert list(obj) == ["behaviors", "values", "layers"]
    assert tuple(obj["behaviors"]) == BEHAVIOR_IDS
    assert all(v is True for v in obj["behaviors"].values())
    assert obj["values"] == {"afk_quiet_minutes": 10, "departure_words": ["afk"], "waiter_model": "haiku"}
    assert [layer["name"] for layer in obj["layers"]] == ["defaults", "user", "project"]
    assert [layer["present"] for layer in obj["layers"]] == [True, False, False]
    assert obj["layers"][1]["path"] == str(world.root / "cfg" / "config" / "presence-kit.json")
    assert obj["layers"][2]["path"] == str(world.project / ".claude" / "presence-kit.json")


def test_config_prints_waiter_model(world):
    assert world.run("config").obj["values"]["waiter_model"] == "haiku"
    world.config(values={"waiter_model": "sonnet"})
    assert world.run("config").obj["values"]["waiter_model"] == "sonnet"


@pytest.mark.parametrize("behavior", AWAY_IDS)
def test_config_reports_away_behavior_off_then_on(world, behavior):
    """Matrix: terse-while-away, park-questions, notify-for-input -- off, then re-enabled."""
    world.config(behaviors={behavior: False})
    off = world.run("config").obj["behaviors"]
    assert off[behavior] is False
    assert all(v for k, v in off.items() if k != behavior)
    world.config(behaviors={behavior: "on"}, where="project")
    assert world.run("config").obj["behaviors"][behavior] is True


def test_config_project_dir_sources(world, tmp_path, monkeypatch):
    other = tmp_path / "other"
    (other / ".claude").mkdir(parents=True)
    (other / ".claude" / "presence-kit.json").write_text(
        json.dumps({"behaviors": {"waiting-guard": False}}), encoding="ascii"
    )
    # --project-dir wins over the environment
    assert world.run("config", "--project-dir", str(other)).obj["behaviors"]["waiting-guard"] is False
    assert world.run("config").obj["behaviors"]["waiting-guard"] is True
    # environment, then process cwd
    assert world.run("config", CLAUDE_PROJECT_DIR=str(other)).obj["behaviors"]["waiting-guard"] is False
    monkeypatch.chdir(other)
    res = world.run("config", CLAUDE_PROJECT_DIR=None)
    assert res.obj["behaviors"]["waiting-guard"] is False
    assert Path(res.obj["layers"][2]["path"]).parent.parent.resolve() == other.resolve()


def test_config_error_is_an_error_object(world):
    world.config(behaviors={"no-such-id": True})
    res = world.run("config")
    obj = error_of(res, 1, "config", "unknown-behavior")
    assert "unknown behavior id 'no-such-id'" in obj["message"]
    assert obj["message"].startswith(str(world.root / "cfg" / "config" / "presence-kit.json"))


def test_config_malformed_json_error(world):
    world.config(raw="{not json")
    error_of(world.run("config"), 1, "config", "malformed-json")


# ---------------------------------------------------------------- session ids

def test_state_get_without_a_session_is_a_state_error(world):
    error_of(
        world.run("state", "get"), 1, "state", "no-session",
        "no session id: pass --session or set CLAUDE_CODE_SESSION_ID",
    )


def test_session_comes_from_env_and_the_flag_wins(world):
    world.run("state", "set", "afk", "--by", "user", "--session", "flag-sid")
    assert world.run("state", "get", CLAUDE_CODE_SESSION_ID="env-sid").obj["exists"] is False
    assert world.run("state", "get", CLAUDE_CODE_SESSION_ID="flag-sid").obj["exists"] is True
    both = world.run("state", "get", "--session", "flag-sid", CLAUDE_CODE_SESSION_ID="env-sid")
    assert both.obj["exists"] is True


@pytest.mark.parametrize("bad", ["a/b", "a b", "..", "x" * 129, "caf\u00e9"])
def test_bad_session_id(world, bad):
    res = world.run("state", "get", "--session", bad)
    error_of(res, 1, "state", "bad-session-id", f"session id '{bad}' must match [A-Za-z0-9_-]{{1,128}}")


def test_empty_session_flag_is_no_session(world):
    error_of(world.run("state", "get", "--session", "", CLAUDE_CODE_SESSION_ID="env-sid"), 1, "state", "no-session")


# ------------------------------------------------------------------ state get

def test_state_get_absent_and_present(world):
    absent = world.run("state", "get", "--session", SID).obj
    assert list(absent) == ["exists", "state"] and absent["exists"] is False
    assert absent["state"] == ps.default_state()
    assert not world.state_file().exists()
    world.run("state", "set", "afk", "--by", "user", "--session", SID)
    present = world.run("state", "get", "--session", SID).obj
    assert present["exists"] is True and present["state"]["presence"] == "afk"


def test_state_malformed_file_is_a_state_error(world):
    path = world.state_file()
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="ascii")
    obj = error_of(world.run("state", "get", "--session", SID), 1, "state", "malformed")
    assert "malformed state file" in obj["message"]


# ------------------------------------------------------------------ state set

def test_state_set_afk_by_user_writes(world):
    res = world.run("state", "set", "afk", "--by", "user", "--session", SID)
    assert res.rc == 0 and res.err == ""
    obj = res.obj
    assert list(obj) == ["written", "by", "state"]
    assert obj["written"] is True and obj["by"] == "user"
    assert obj["state"]["presence"] == "afk"
    assert obj["state"]["afk_since"] == "2026-10-17T12:00:00Z"
    assert obj["state"] == world.state()


def test_state_set_afk_is_idempotent(world):
    world.run("state", "set", "afk", "--by", "user", "--session", SID, now=T0)
    later = world.run("state", "set", "afk", "--by", "user", "--session", SID, now=T0 + timedelta(hours=1))
    assert later.obj["state"]["afk_since"] == "2026-10-17T12:00:00Z"


def test_afk_by_model_skipped_while_off_written_when_on(world):
    """Matrix afk-on-departure: --by model skipped -> written; nothing written when skipped."""
    world.config(behaviors={"afk-on-departure": False})
    skipped = world.run("state", "set", "afk", "--by", "model", "--session", SID)
    assert skipped.rc == 0 and skipped.err == ""
    assert skipped.obj == {"skipped": "afk-on-departure", "reason": "off"}
    assert not world.state_file().exists()
    world.config(behaviors={"afk-on-departure": "on"})
    written = world.run("state", "set", "afk", "--by", "model", "--session", SID)
    assert written.obj["written"] is True and written.obj["by"] == "model"
    assert world.state()["presence"] == "afk"


def test_afk_by_user_writes_while_off(world):
    world.config(behaviors={"afk-on-departure": False})
    res = world.run("state", "set", "afk", "--by", "user", "--session", SID)
    assert res.obj["written"] is True and res.obj["by"] == "user"
    assert world.state()["presence"] == "afk"


def test_autonomous_by_model_skipped_while_off_written_when_on(world):
    """Matrix autonomous-on-handover."""
    world.config(behaviors={"autonomous-on-handover": False})
    skipped = world.run("state", "set", "autonomous", "--by", "model", "--goal", "ship", "--session", SID)
    assert skipped.obj == {"skipped": "autonomous-on-handover", "reason": "off"}
    assert not world.state_file().exists()
    world.config(behaviors={"autonomous-on-handover": True})
    written = world.run("state", "set", "autonomous", "--by", "model", "--goal", "ship", "--session", SID)
    assert written.obj["written"] is True and written.obj["by"] == "model"
    state = world.state()
    assert (state["presence"], state["goal"]) == ("afk", "ship")


def test_autonomous_by_user_writes_while_off(world):
    world.config(behaviors={"autonomous-on-handover": False})
    res = world.run("state", "set", "autonomous", "--by", "user", "--goal", "ship", "--session", SID)
    assert res.obj["written"] is True and res.obj["by"] == "user"


def test_autonomous_gate_ignores_the_other_id(world):
    """afk-on-departure off does not skip an autonomous hand-over, and the reverse."""
    world.config(behaviors={"afk-on-departure": False})
    assert world.run("state", "set", "autonomous", "--by", "model", "--goal", "g", "--session", SID).obj["written"]
    world.config(behaviors={"autonomous-on-handover": False})
    assert world.run("state", "set", "afk", "--by", "model", "--session", "other").obj["written"]


def test_by_user_never_loads_config(world):
    """A broken config cannot block the user's own command; --by model reports it."""
    world.config(raw="{not json")
    assert world.run("state", "set", "afk", "--by", "user", "--session", SID).obj["written"] is True
    assert world.run("state", "set", "autonomous", "--by", "user", "--goal", "g", "--session", SID).obj["written"]
    assert world.run("state", "set", "normal", "--session", SID).obj["written"] is True
    error_of(world.run("state", "set", "afk", "--by", "model", "--session", SID), 1, "config", "malformed-json")
    error_of(
        world.run("state", "set", "autonomous", "--by", "model", "--goal", "g", "--session", SID),
        1, "config", "malformed-json",
    )


def test_by_model_reads_the_project_layer(world, tmp_path):
    other = tmp_path / "other"
    (other / ".claude").mkdir(parents=True)
    (other / ".claude" / "presence-kit.json").write_text(
        json.dumps({"behaviors": {"afk-on-departure": False}}), encoding="ascii"
    )
    explicit = world.run("state", "set", "afk", "--by", "model", "--project-dir", str(other), "--session", SID)
    assert explicit.obj["skipped"] == "afk-on-departure"
    via_env = world.run("state", "set", "afk", "--by", "model", "--session", SID, CLAUDE_PROJECT_DIR=str(other))
    assert via_env.obj["skipped"] == "afk-on-departure"
    ignored = world.run("state", "set", "afk", "--by", "user", "--project-dir", str(other), "--session", SID)
    assert ignored.obj["written"] is True


def test_autonomous_goal_is_stripped_and_repeat_keeps_goal_since(world):
    first = world.run("state", "set", "autonomous", "--by", "user", "--goal", "  ship it  ", "--session", SID, now=T0)
    assert first.obj["state"]["goal"] == "ship it"
    assert first.obj["state"]["goal_since"] == "2026-10-17T12:00:00Z"
    again = world.run(
        "state", "set", "autonomous", "--by", "user", "--goal", "ship it", "--session", SID,
        now=T0 + timedelta(hours=1),
    )
    assert again.obj["state"]["goal_since"] == "2026-10-17T12:00:00Z"
    other = world.run(
        "state", "set", "autonomous", "--by", "user", "--goal", "different", "--session", SID,
        now=T0 + timedelta(hours=2),
    )
    assert other.obj["state"]["goal"] == "different"
    assert other.obj["state"]["goal_since"] == "2026-10-17T14:00:00Z"


def test_state_set_normal_clears_goal_keeps_presence(world):
    world.run("state", "set", "autonomous", "--by", "user", "--goal", "g", "--session", SID)
    res = world.run("state", "set", "normal", "--session", SID)
    assert res.obj["written"] is True and res.obj["by"] == "user"
    state = res.obj["state"]
    assert (state["goal"], state["goal_since"], state["presence"]) == (None, None, "afk")


@pytest.mark.parametrize("goal", ["", " ", "\t \n"])
def test_blank_goal_is_a_usage_error(world, goal):
    res = world.run("state", "set", "autonomous", "--by", "user", "--goal", goal, "--session", SID)
    error_of(res, 2, "usage", "bad-argv", "argument --goal: must not be blank")
    assert not world.state_file().exists()


@pytest.mark.parametrize("argv", [
    ["state", "set", "autonomous", "--by", "user"],                       # --goal missing
    ["state", "set", "afk"],                                              # --by missing
    ["state", "set", "afk", "--by", "robot"],                             # bad --by
    ["state", "set", "normal", "--by", "user"],                           # normal takes no --by
    ["state", "set", "afk", "--by", "user", "--goal", "g"],               # afk takes no --goal
    ["state", "set", "away", "--by", "user"],                             # unknown target
    ["state", "set"], ["state"], ["state", "frob"],
    ["config", "--bogus"], ["config", "extra"],
    ["recap-done"], ["recap-done", "return"], ["recap-done", "later", "p1"],
    ["recap-done", "return", "p1", "extra"],
    [], ["frobnicate"], ["--bogus"],
])
def test_usage_errors_exit_2_with_an_object(world, argv):
    res = world.run(*argv, CLAUDE_CODE_SESSION_ID=SID)
    error_of(res, 2, "usage", "bad-argv")
    assert not world.state_file().exists()


def test_prompt_verb_is_not_implemented_yet(world):
    """K3 owns `prompt`: until it lands it is an unknown verb, never a pretend success."""
    res = world.run("prompt", stdin='{"session_id": "s", "prompt_id": "p"}')
    obj = error_of(res, 2, "usage", "bad-argv")
    assert "invalid choice: 'prompt'" in obj["message"]


def test_abbreviated_flags_are_refused(world):
    error_of(world.run("config", "--project", str(world.project)), 2, "usage", "bad-argv")


def test_help_exits_0(world):
    res = world.run("--help")
    assert res.rc == 0 and res.err == ""
    assert "usage: presence.py" in res.out
    for verb in ("config", "state", "recap-done", "stop"):
        assert verb in res.out
    assert world.run("state", "set", "afk", "--help").rc == 0


# ----------------------------------------------------------------- recap-done

@pytest.mark.parametrize("trigger", ["return", "afk-ending", "autonomous-done"])
def test_recap_done_records_the_trigger(world, trigger):
    world.run("state", "set", "autonomous", "--by", "user", "--goal", "g", "--session", SID)
    res = world.run("recap-done", trigger, "p-1", "--session", SID)
    assert res.rc == 0 and res.err == ""
    obj = res.obj
    assert list(obj) == ["recorded", "prompt_id", "state"]
    assert (obj["recorded"], obj["prompt_id"]) == (trigger, "p-1")
    state = obj["state"]
    assert state["recaps"] == {"p-1": trigger}
    assert state == world.state()
    if trigger == "return":
        assert state["presence"] == "present" and state["goal"] == "g"
    elif trigger == "afk-ending":
        assert state["presence"] == "afk" and state["goal"] == "g"
    else:
        assert state["presence"] == "afk" and state["goal"] is None and state["goal_since"] is None


def test_recap_done_last_write_wins_and_uses_env_session(world):
    world.run("recap-done", "return", "p-1", CLAUDE_CODE_SESSION_ID=SID)
    res = world.run("recap-done", "afk-ending", "p-1", CLAUDE_CODE_SESSION_ID=SID)
    assert res.obj["state"]["recaps"] == {"p-1": "afk-ending"}


@pytest.mark.parametrize("pid", ["a b", "x" * 129])
def test_recap_done_bad_prompt_id(world, pid):
    res = world.run("recap-done", "return", pid, "--session", SID)
    error_of(res, 1, "state", "bad-prompt-id", f"prompt id '{pid}' must be 1-128 characters without whitespace")


def test_recap_done_without_session(world):
    error_of(world.run("recap-done", "return", "p-1"), 1, "state", "no-session")


# ----------------------------------------------------------- internal errors

def test_unexpected_exception_is_an_internal_error(world, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(presence.ps, "state_get", boom)
    res = world.run("state", "get", "--session", SID)
    error_of(res, 1, "internal", "unexpected", "RuntimeError: boom")
    assert "Traceback (most recent call last)" in res.err


# ----------------------------------------------------------------------- stop

def load_fixture(name, root, **patch):
    """A fixture Stop payload with its transcript copied under `root` and paths made real."""
    payload = json.loads((FIXTURES / f"{name}.json").read_text(encoding="ascii"))
    transcript = root / f"{name}.jsonl"
    src = FIXTURES / f"{name}.transcript.jsonl"
    if src.exists():
        transcript.write_bytes(src.read_bytes())
    payload["transcript_path"] = str(transcript)
    payload["cwd"] = str(root / "proj")
    payload.update(patch)
    return payload


def sdk_line(name):
    return json.loads((FIXTURES / f"{name}.transcript.jsonl").read_text(encoding="ascii").splitlines()[0])


def human_entry(pid, when):
    """SYNTHETIC human entry: the captured sdk entry with origin and time edited."""
    obj = copy.deepcopy(sdk_line("a1-03-Stop"))
    obj.update(promptId=pid, turnOrigin="human", promptSource="human", timestamp=stamp(when))
    obj["uuid"] = f"uuid-{pid}"
    return obj


def run_stop(world, payload, attended="1", **kw):
    return world.run("stop", stdin=json.dumps(payload), CLAUDE_CODE_SESSION_ATTENDED=attended, **kw)


C_LINE = "add a waiter agent; the terminal shows idle."
W_LINE = "end with `Waiting on <what>`."


def test_stop_nudge_end_to_end(world):
    """a1-03: a running shell, an sdk entry, a last line that is not `Waiting on`: C and W in one nudge."""
    payload = load_fixture("a1-03-Stop", world.root)
    res = run_stop(world, payload)
    assert res.rc == 0 and res.err == ""
    pid = payload["prompt_id"]
    assert res.out == json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "Stop",
            "additionalContext": L("waiting-guard", pid, C_LINE) + "\n" + L("waiting-guard", pid, W_LINE),
        }
    }) + "\n"
    assert world.state(payload["session_id"])["nudges"] == {pid: 1}


def test_stop_silent_allow_prints_nothing(world):
    payload = load_fixture("a1-05-Stop", world.root)
    res = run_stop(world, payload)
    assert (res.rc, res.out, res.err) == (0, "", "")


@pytest.mark.parametrize("attended", [None, "0", "", "true", "yes", " 1", "1 "])
def test_stop_unattended_is_a_silent_no_op(world, attended):
    """Gate: only exactly `1` acts. No stdin read, nothing created."""

    class NoRead:
        def read(self):
            raise AssertionError("stdin was read while unattended")

    res = world.run("stop", stdin=NoRead(), CLAUDE_CODE_SESSION_ATTENDED=attended)
    assert (res.rc, res.out, res.err) == (0, "", "")
    assert not (world.root / "data").exists()


def test_stop_attended_gate_precedes_payload_errors(world):
    res = world.run("stop", stdin="not json", CLAUDE_CODE_SESSION_ATTENDED="0")
    assert (res.rc, res.out, res.err) == (0, "", "")


def test_stop_extra_argv_is_checked_before_the_gate(world):
    for attended in ("1", "0", None):
        res = world.run("stop", "now", "--force", stdin="", CLAUDE_CODE_SESSION_ATTENDED=attended)
        assert res.rc == 1 and res.out == ""
        assert res.err == "stop takes no arguments; got now --force\n"


def test_stop_help_flag_is_extra_argv_not_argparse(world):
    res = world.run("stop", "--help", CLAUDE_CODE_SESSION_ATTENDED="1")
    assert (res.rc, res.out) == (1, "")
    assert res.err == "stop takes no arguments; got --help\n"


@pytest.mark.parametrize("raw, message", [
    ("", "hook payload is empty"),
    (" \n\t", "hook payload is empty"),
    ("[1, 2]", "hook payload must be a JSON object, got array"),
    ('"s"', "hook payload must be a JSON object, got string"),
    ("7", "hook payload must be a JSON object, got number"),
    ("null", "hook payload must be a JSON object, got null"),
    ("true", "hook payload must be a JSON object, got boolean"),
    ('{"prompt_id": "p"}', "hook payload field 'session_id' must be a non-empty string"),
    ('{"session_id": "", "prompt_id": "p"}', "hook payload field 'session_id' must be a non-empty string"),
    ('{"session_id": 7, "prompt_id": "p"}', "hook payload field 'session_id' must be a non-empty string"),
    ('{"session_id": "s"}', "hook payload field 'prompt_id' must be a non-empty string"),
    ('{"session_id": "s", "prompt_id": null}', "hook payload field 'prompt_id' must be a non-empty string"),
    ('{"session_id": "a/b", "prompt_id": "p"}', "session id 'a/b' must match [A-Za-z0-9_-]{1,128}"),
    ('{"session_id": "s", "prompt_id": "a b"}', "prompt id 'a b' must be 1-128 characters without whitespace"),
])
def test_stop_payload_errors_are_stderr_only(world, raw, message):
    res = world.run("stop", stdin=raw, CLAUDE_CODE_SESSION_ATTENDED="1")
    assert res.rc == 1 and res.out == ""
    assert res.err == message + "\n"
    assert not (world.root / "data").exists()


def test_stop_malformed_json_is_stderr_only(world):
    res = world.run("stop", stdin="{not json", CLAUDE_CODE_SESSION_ATTENDED="1")
    assert res.rc == 1 and res.out == ""
    assert res.err.startswith("hook payload is not valid JSON -- ")


def test_stop_invalid_utf8_is_malformed_json(world):
    stdin = io.TextIOWrapper(io.BytesIO(b'{"session_id": "\xff"}'), encoding="utf-8")
    res = world.run("stop", stdin=stdin, CLAUDE_CODE_SESSION_ATTENDED="1")
    assert res.rc == 1 and res.out == ""
    assert res.err.startswith("hook payload is not valid JSON -- ")
    assert "utf-8" in res.err


def test_stop_bad_payload_ids_are_payload_class_kinds(world, monkeypatch):
    """The class is `payload` even though the texts are the state ones (stderr shows text only)."""
    seen = []
    real = presence._failure
    monkeypatch.setattr(presence, "_failure", lambda err, ctx, *, hook: (seen.append((err.error, err.kind)), real(err, ctx, hook=hook))[1])
    run_stop(world, {"session_id": "a/b", "prompt_id": "p"})
    run_stop(world, {"session_id": "s", "prompt_id": "a b"})
    run_stop(world, {"session_id": "s"})
    assert seen == [("payload", "bad-session-id"), ("payload", "bad-prompt-id"), ("payload", "missing-field")]


def test_stop_config_error_is_reported_and_allows(world):
    """Row 1: a ConfigError in a hook is exit 0 with a systemMessage, no state change."""
    world.config(raw="{not json", where="project")
    payload = load_fixture("a1-03-Stop", world.root)
    res = run_stop(world, payload)
    assert res.rc == 0 and res.err == ""
    obj = res.obj
    assert list(obj) == ["systemMessage"]
    path = world.project / ".claude" / "presence-kit.json"
    assert obj["systemMessage"].startswith(f"presence-kit [config] prompt {payload['prompt_id']}: {path}: malformed JSON -- ")
    assert obj["systemMessage"].endswith("Presence behavior is unavailable until the file is fixed. " + CFG_HINT)
    assert not world.state_file(payload["session_id"]).exists()


def test_stop_state_error_is_stderr_only_exit_1(world):
    payload = load_fixture("a1-03-Stop", world.root)
    path = world.state_file(payload["session_id"])
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="ascii")
    res = run_stop(world, payload)
    assert res.rc == 1 and res.out == ""
    assert "malformed state file" in res.err


def test_stop_unexpected_exception_is_exit_1_stderr_only(world, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(presence.pstop, "evaluate", boom)
    res = run_stop(world, load_fixture("a1-03-Stop", world.root))
    assert res.rc == 1 and res.out == ""
    assert "RuntimeError: boom" in res.err and "Traceback" in res.err


def test_stop_never_exits_2(world):
    for raw in ("", "{", "[]", '{"session_id": 1}', "{}"):
        assert world.run("stop", stdin=raw, CLAUDE_CODE_SESSION_ATTENDED="1").rc == 1


def test_stop_notice_once_per_session(world):
    """Row 2 through the CLI: a missing transcript entry -> one systemMessage, then silence."""
    payload = load_fixture("a1-05-Stop", world.root, transcript_path=str(world.root / "absent.jsonl"))
    first = run_stop(world, payload)
    pid = payload["prompt_id"]
    assert first.obj == {"systemMessage": L(
        "afk-on-quiet,recap-on-return", pid,
        "skipped quiet-afk and return detection: the transcript has no user entry for this prompt yet.",
    )}
    second = run_stop(world, payload)
    assert (second.rc, second.out) == (0, "")


def test_stop_notice_and_nudge_share_one_object(world):
    payload = load_fixture("a1-03-Stop", world.root, transcript_path=str(world.root / "absent.jsonl"))
    res = run_stop(world, payload)
    obj = res.obj
    assert list(obj) == ["systemMessage", "hookSpecificOutput"]
    assert obj["hookSpecificOutput"]["hookEventName"] == "Stop"
    assert C_LINE in obj["hookSpecificOutput"]["additionalContext"]


@pytest.mark.parametrize("source", ["env", "payload-cwd", "process-cwd"])
def test_stop_project_dir_sources(world, tmp_path, monkeypatch, source):
    """waiting-guard off in a project layer silences a1-03's C/W nudge, so the nudge shows which dir was read."""
    off = tmp_path / "off"
    (off / ".claude").mkdir(parents=True)
    (off / ".claude" / "presence-kit.json").write_text(
        json.dumps({"behaviors": {"waiting-guard": False}}), encoding="ascii"
    )
    payload = load_fixture("a1-03-Stop", world.root)
    env_over = {"CLAUDE_PROJECT_DIR": None}
    if source == "env":
        env_over["CLAUDE_PROJECT_DIR"] = str(off)
        payload["cwd"] = str(world.project)      # env wins over the payload cwd
    elif source == "payload-cwd":
        payload["cwd"] = str(off)
    else:
        del payload["cwd"]
        monkeypatch.chdir(off)
    res = run_stop(world, payload, **env_over)
    assert (res.rc, res.out, res.err) == (0, "", "")
    # control: the same payload against the default project nudges
    control = load_fixture("a1-03-Stop", world.root, session_id="control-sid")
    assert run_stop(world, control).obj["hookSpecificOutput"]


def test_stop_ignores_a_blank_payload_cwd(world, monkeypatch):
    payload = load_fixture("a1-03-Stop", world.root, cwd="")
    monkeypatch.chdir(world.root)
    assert run_stop(world, payload, CLAUDE_PROJECT_DIR=None).obj["hookSpecificOutput"]


def test_stop_return_flow_yields_exactly_one_recap(world):
    """afk (user) -> a human prompt -> R nudge -> recap-done return -> silence; no second recap."""
    sid, pid = "flow-return", "p-return"
    world.run("state", "set", "afk", "--by", "user", "--session", sid, now=T0)
    transcript = world.root / "flow.jsonl"
    transcript.write_text(json.dumps(human_entry(pid, T0 + timedelta(minutes=30))) + "\n", encoding="ascii")
    payload = {"session_id": sid, "prompt_id": pid, "transcript_path": str(transcript),
               "last_assistant_message": "hello", "background_tasks": [], "session_crons": []}
    first = run_stop(world, payload, now=T0 + timedelta(minutes=31))
    r_line = L("recap-on-return", pid, f"the user is back; invoke `presence-kit:recap return {pid}`.")
    assert first.obj == {"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": r_line}}
    assert world.state(sid)["presence"] == "present"
    done = world.run("recap-done", "return", pid, "--session", sid, now=T0 + timedelta(minutes=32))
    assert done.obj["state"]["recaps"] == {pid: "return"}
    after = run_stop(world, payload, now=T0 + timedelta(minutes=33))
    assert (after.rc, after.out) == (0, "")


def test_stop_afk_ending_flow_yields_exactly_one_recap(world):
    sid, pid = "flow-afk", "p-end"
    world.run("state", "set", "afk", "--by", "user", "--session", sid, now=T0)
    transcript = world.root / "flow.jsonl"
    transcript.write_text(json.dumps(user_task_entry(pid, T0 + timedelta(minutes=1))) + "\n", encoding="ascii")
    payload = {"session_id": sid, "prompt_id": pid, "transcript_path": str(transcript),
               "last_assistant_message": "done", "background_tasks": [], "session_crons": []}
    nudge = run_stop(world, payload, now=T0 + timedelta(minutes=2))
    e_line = L("ending-recap", pid, f"invoke `presence-kit:recap afk-ending {pid}`.")
    assert nudge.obj["hookSpecificOutput"]["additionalContext"] == e_line
    world.run("recap-done", "afk-ending", pid, "--session", sid)
    assert run_stop(world, payload, now=T0 + timedelta(minutes=3)).out == ""
    assert world.state(sid)["greeted"] is True


def test_stop_autonomous_done_flow_yields_exactly_one_recap(world):
    sid, pid = "flow-aut", "p-goal"
    world.run("state", "set", "autonomous", "--by", "user", "--goal", "ship it", "--session", sid, now=T0)
    transcript = world.root / "flow.jsonl"
    transcript.write_text(json.dumps(user_task_entry(pid, T0 + timedelta(minutes=1))) + "\n", encoding="ascii")
    payload = {"session_id": sid, "prompt_id": pid, "transcript_path": str(transcript),
               "last_assistant_message": "done", "background_tasks": [], "session_crons": []}
    nudge = run_stop(world, payload, now=T0 + timedelta(minutes=2))
    text = ("autonomous: if ship it is done, invoke "
            f"`presence-kit:recap autonomous-done {pid}`, else keep working.")
    assert nudge.obj["hookSpecificOutput"]["additionalContext"] == L("ending-recap", pid, text)
    world.run("recap-done", "autonomous-done", pid, "--session", sid)
    assert run_stop(world, payload, now=T0 + timedelta(minutes=3)).out == ""
    state = world.state(sid)
    assert state["goal"] is None and state["greeted"] is True


def user_task_entry(pid, when):
    obj = copy.deepcopy(sdk_line("a1-05-Stop"))
    obj.update(promptId=pid, timestamp=stamp(when))
    return obj


def test_stop_budget_exhaustion_through_the_cli(world):
    """Three nudges, then the exhaustion systemMessage with no nudge."""
    payload = load_fixture("a1-03-Stop", world.root)
    for _ in range(3):
        assert "hookSpecificOutput" in run_stop(world, payload).obj
    last = run_stop(world, payload).obj
    assert list(last) == ["systemMessage"]
    assert last["systemMessage"].count("unresolved after 3 nudges") == 2


# --------------------------------------------------- subprocess, real script

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


def _subprocess(root, argv, stdin="", **extra):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *argv], input=stdin, capture_output=True, text=True,
        env=_clean_env(root, **extra), cwd=str(root), timeout=60,
    )


@pytest.mark.parametrize("attended", ["1", "0", None])
def test_subprocess_stop_with_attended_gate(tmp_path, attended):
    (tmp_path / "proj").mkdir()
    payload = load_fixture("a1-03-Stop", tmp_path)
    extra = {} if attended is None else {"CLAUDE_CODE_SESSION_ATTENDED": attended}
    run = _subprocess(tmp_path, ["stop"], json.dumps(payload), **extra)
    state_file = tmp_path / "data" / "plugins-kit" / "presence-kit" / "sessions" / f"{payload['session_id']}.json"
    assert run.returncode == 0 and run.stderr == ""
    if attended == "1":
        out = json.loads(run.stdout)
        ctx = out["hookSpecificOutput"]
        assert ctx["hookEventName"] == "Stop" and C_LINE in ctx["additionalContext"] and W_LINE in ctx["additionalContext"]
        assert state_file.is_file()
    else:
        assert run.stdout == ""
        assert not state_file.exists()
        assert not (tmp_path / "data").exists()


def test_subprocess_cli_verbs_and_exit_codes(tmp_path):
    (tmp_path / "proj").mkdir()
    ok = _subprocess(tmp_path, ["config"])
    assert ok.returncode == 0 and json.loads(ok.stdout)["values"]["waiter_model"] == "haiku"
    wrote = _subprocess(tmp_path, ["state", "set", "afk", "--by", "model", "--session", "sub-sid"])
    assert wrote.returncode == 0 and json.loads(wrote.stdout)["written"] is True
    got = _subprocess(tmp_path, ["state", "get"], CLAUDE_CODE_SESSION_ID="sub-sid")
    assert json.loads(got.stdout)["state"]["presence"] == "afk"
    done = _subprocess(tmp_path, ["recap-done", "return", "p9", "--session", "sub-sid"])
    assert json.loads(done.stdout)["state"]["presence"] == "present"
    usage = _subprocess(tmp_path, ["prompt"])
    assert usage.returncode == 2 and json.loads(usage.stdout)["kind"] == "bad-argv"
    nosession = _subprocess(tmp_path, ["state", "get"])
    assert nosession.returncode == 1 and json.loads(nosession.stdout)["kind"] == "no-session"
    assert nosession.stderr.strip() == json.loads(nosession.stdout)["message"]


def test_subprocess_stop_failure_is_stderr_only(tmp_path):
    (tmp_path / "proj").mkdir()
    run = _subprocess(tmp_path, ["stop"], "{not json", CLAUDE_CODE_SESSION_ATTENDED="1")
    assert run.returncode == 1 and run.stdout == ""
    assert run.stderr.startswith("hook payload is not valid JSON -- ")


def test_subprocess_stop_reads_utf8_bytes(tmp_path):
    """The payload is decoded as UTF-8 whatever the console code page says."""
    (tmp_path / "proj").mkdir()
    payload = load_fixture("a1-03-Stop", tmp_path, last_assistant_message="caf\u00e9 \u2713")
    run = subprocess.run(
        [sys.executable, str(SCRIPT), "stop"], input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        capture_output=True, env=_clean_env(tmp_path, CLAUDE_CODE_SESSION_ATTENDED="1"), cwd=str(tmp_path), timeout=60,
    )
    assert run.returncode == 0, run.stderr
    assert "hookSpecificOutput" in json.loads(run.stdout.decode("ascii"))
