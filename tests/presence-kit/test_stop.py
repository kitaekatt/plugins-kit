"""presence-kit Stop logic: presence_transcript.py and presence_stop.py.

Contract: dev/tasks/presence-kit/contracts.md sections 5-7 and 11; design.md "Hooks"
(Stop rows 1-9). The real K2a (config) and K2b (state) modules run against a temp
data root (CLAUDE_BOOTSTRAP_DATA_ROOT) and a temp config dir (CLAUDE_CONFIG_DIR).
Time is injected through `now`; nothing sleeps. Expected message texts are restated
here literally (L) so a change to presence_config.py cannot silently move them.

Transcript lines for the captured entry shape come from the P0 fixtures; human,
peer and unknown-origin entries are SYNTHETIC (P0 could not capture them): the
captured `sdk` entry with `turnOrigin`, `promptSource` and `timestamp` edited.
"""
import copy
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "plugins" / "presence-kit" / "scripts"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(SCRIPTS))

import presence_config as pc  # noqa: E402
import presence_prompt as pprompt  # noqa: E402
import presence_state as ps  # noqa: E402
import presence_stop as pstop  # noqa: E402
import presence_transcript as pt  # noqa: E402

SID = "sess-stop"
PID = "prompt-1"
NOW = datetime(2026, 10, 17, 12, 0, 0, tzinfo=timezone.utc)
ABSENT = object()
CFG_HINT = "See references/configuring.md."


def mins(n):
    """The instant n minutes before NOW."""
    return NOW - timedelta(minutes=n)


def stamp(dt):
    """A transcript timestamp: milliseconds and Z, like the captures."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def L(ids, pid, text):
    """A hook line, restated literally from contracts.md section 7."""
    return f"presence-kit [{ids}] prompt {pid}: {text} {CFG_HINT}"


def fixture_lines(name):
    return [json.loads(x) for x in (FIXTURES / name).read_text(encoding="ascii").splitlines()]


_SDK_ENTRY = fixture_lines("a1-03-Stop.transcript.jsonl")[0]
_TOOL_RESULT = fixture_lines("a1-03-Stop.transcript.jsonl")[1]
SHELL = {"id": "t1", "type": "shell", "status": "running", "description": "job", "command": "sleep 1"}
AGENT = {"id": "t2", "type": "subagent", "status": "running", "description": "agent"}
WAITING_LAST = "Waiting on the job"

# L(...) texts
R_TEXT = "the user is back; invoke `presence-kit:recap return {pid}`."
C_TEXT = "add a waiter agent; the terminal shows idle."
W_TEXT = "end with `Waiting on <what>`."
E_AFK_TEXT = "invoke `presence-kit:recap afk-ending {pid}`."


def line_R(pid=PID):
    return L("recap-on-return", pid, R_TEXT.format(pid=pid))


def line_C(pid=PID):
    return L("waiting-guard", pid, C_TEXT)


def line_W(pid=PID):
    return L("waiting-guard", pid, W_TEXT)


def line_E(pid=PID):
    return L("ending-recap", pid, E_AFK_TEXT.format(pid=pid))


def user_entry(pid, origin, when, **extra):
    """A prompt entry: the captured sdk entry with its origin and time edited."""
    obj = copy.deepcopy(_SDK_ENTRY)
    obj.update(promptId=pid, turnOrigin=origin, promptSource=origin, timestamp=stamp(when))
    obj["uuid"] = f"uuid-{pid}-{origin}"
    obj.update(extra)
    return obj


def make_config(tmp_path, behaviors=None, values=None):
    """Real K2a resolve over the shipped defaults plus an optional user layer."""
    cfg_dir = tmp_path / "cfg"
    layer = {}
    if behaviors:
        layer["behaviors"] = behaviors
    if values:
        layer["values"] = values
    if layer:
        target = cfg_dir / "config" / "presence-kit.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(layer), encoding="ascii")
    return pc.resolve(None, {"CLAUDE_CONFIG_DIR": str(cfg_dir)})


class Session:
    """One session's transcript, state root and config; stop() runs a Stop payload."""

    def __init__(self, root, cfg):
        self.cfg = cfg
        self.env = {"CLAUDE_BOOTSTRAP_DATA_ROOT": str(root / "data")}
        self.transcript = root / "transcript.jsonl"
        self.lines = []

    # transcript
    def add(self, obj):
        self.lines.append(obj)

    def entry(self, pid, origin, when, **extra):
        self.add(user_entry(pid, origin, when, **extra))

    def human(self, pid, when, **extra):
        self.entry(pid, "human", when, **extra)

    def write_transcript(self):
        if self.lines:
            self.transcript.write_text(
                "".join(json.dumps(x) + "\n" for x in self.lines), encoding="ascii"
            )

    # state
    def state(self):
        return ps.load(SID, self.env)

    def set_state(self, **fields):
        def fn(doc):
            doc.update(fields)

        ps.update(SID, fn, self.env)

    def go_afk(self, since=None, goal=None):
        since = ps.format_timestamp(since or mins(30))

        def fn(doc):
            ps.enter_afk(doc, since)
            if goal is not None:
                ps.enter_autonomous(doc, goal, since)

        ps.update(SID, fn, self.env)

    def recap_done(self, trigger, pid=PID):
        return ps.recap_done(SID, trigger, pid, self.env, NOW)

    # stop
    def payload(self, pid=PID, tasks=(), crons=(), last="DONE", **extra):
        payload = {
            "session_id": SID,
            "transcript_path": str(self.transcript),
            "cwd": "<SCRATCH>",
            "prompt_id": pid,
            "hook_event_name": "Stop",
            "stop_hook_active": False,
            "last_assistant_message": last,
            "background_tasks": list(tasks) if isinstance(tasks, tuple) else tasks,
            "session_crons": list(crons) if isinstance(crons, tuple) else crons,
        }
        payload.update(extra)
        return {k: v for k, v in payload.items() if v is not ABSENT}

    def stop(self, pid=PID, now=NOW, **fields):
        self.write_transcript()
        return pstop.evaluate(self.payload(pid, **fields), self.cfg, self.env, now)


@pytest.fixture
def make(tmp_path):
    """make(behaviors, values, name): an isolated session (own data root, config, transcript)."""

    def factory(behaviors=None, values=None, name="s"):
        root = tmp_path / name
        root.mkdir(parents=True, exist_ok=True)
        return Session(root, make_config(root, behaviors, values))

    return factory


@pytest.fixture
def s(make):
    return make()


def owned_quiet():
    """Fields that hold no condition by themselves: an agent runs and the turn says so."""
    return dict(tasks=[AGENT], last=WAITING_LAST)


# =============================================================== fixtures inventory

EXPECTED_FIXTURES = sorted(
    [f"{p}-Stop.json" for p in ("a1-03", "a1-05", "b1-04", "c1-04", "d1-03", "e1-03", "f1-04", "g01-03", "g21-05")]
    + [f"{p}-Stop.transcript.jsonl" for p in ("a1-03", "a1-05", "b1-04", "c1-04", "d1-03", "e1-03", "f1-04", "g01-03", "g21-05")]
    + ["b1-04-Stop.transcript_extra.jsonl", "f1-04-Stop.transcript_extra.jsonl",
       "f1-03-Stop.json", "f2-04-Stop.transcript.jsonl",
       "a1-02-UserPromptSubmit.json", "a1-04-UserPromptSubmit.json",
       "e1-04-UserPromptSubmit.json", "b1-03-UserPromptSubmit.json"]
)


def test_fixture_inventory_is_the_26_listed_files_ascii_without_machine_paths():
    assert sorted(p.name for p in FIXTURES.iterdir()) == EXPECTED_FIXTURES
    assert len(EXPECTED_FIXTURES) == 26
    for path in FIXTURES.iterdir():
        text = path.read_bytes().decode("ascii")  # raises on non-ASCII
        for marker in ("C:\\Users", "/Users/", "/home/", "D--Dev", "truff"):
            assert marker not in text, (path.name, marker)


# ============================================================ presence_transcript

def write_lines(path, lines):
    path.write_text("".join((x if isinstance(x, str) else json.dumps(x)) + "\n" for x in lines), encoding="ascii")
    return str(path)


def test_known_origins_are_the_four_contract_values():
    assert pt.KNOWN_ORIGINS == ("human", "task_notification", "peer", "sdk")


def test_find_entry_reads_a_captured_sdk_entry(tmp_path):
    path = str(FIXTURES / "a1-03-Stop.transcript.jsonl")
    pid = json.loads((FIXTURES / "a1-03-Stop.json").read_text())["prompt_id"]
    entry, cause = pt.find_prompt_entry(path, pid)
    assert cause is None
    assert entry == pt.Entry(pid, "sdk", datetime(2026, 10, 17, 17, 16, 10, 89000, tzinfo=timezone.utc), 0)


def test_find_entry_first_matching_line_wins_and_index_is_the_physical_line(tmp_path):
    path = write_lines(tmp_path / "t.jsonl", [
        {"type": "assistant", "message": "x"},
        "",
        "{not json",
        user_entry("other", "human", mins(5)),
        user_entry("p", "peer", mins(3)),
        _TOOL_RESULT | {"promptId": "p"},
        user_entry("p", "human", mins(1)),
    ])
    entry, cause = pt.find_prompt_entry(path, "p")
    assert cause is None
    assert (entry.turn_origin, entry.index) == ("peer", 4)


@pytest.mark.parametrize("path_kind", ["none", "empty", "nofile", "noline"])
def test_find_entry_missing(tmp_path, path_kind):
    path = {
        "none": None,
        "empty": "",
        "nofile": str(tmp_path / "absent.jsonl"),
        "noline": write_lines(tmp_path / "t.jsonl", [user_entry("other", "human", mins(1))]),
    }[path_kind]
    assert pt.find_prompt_entry(path, "p") == (None, "entry-missing")


def test_find_entry_read_error_other_than_missing_file_propagates(tmp_path):
    with pytest.raises(OSError):
        pt.find_prompt_entry(str(tmp_path), "p")  # a directory


@pytest.mark.parametrize("break_what", [
    "type-assistant", "type-missing", "no-timestamp", "naive-timestamp", "bad-timestamp",
    "no-origin", "origin-not-string",
])
def test_find_entry_malformed(tmp_path, break_what):
    obj = user_entry("p", "human", mins(1))
    if break_what == "type-assistant":
        obj["type"] = "assistant"
    elif break_what == "type-missing":
        del obj["type"]
    elif break_what == "no-timestamp":
        del obj["timestamp"]
    elif break_what == "naive-timestamp":
        obj["timestamp"] = "2026-10-17T11:59:00"
    elif break_what == "bad-timestamp":
        obj["timestamp"] = "yesterday"
    elif break_what == "no-origin":
        del obj["turnOrigin"]
    else:
        obj["turnOrigin"] = 7
    path = write_lines(tmp_path / "t.jsonl", [obj])
    assert pt.find_prompt_entry(path, "p") == (None, "entry-malformed")


def test_find_entry_decides_on_the_first_line_even_if_a_later_one_is_valid(tmp_path):
    bad = user_entry("p", "human", mins(2))
    del bad["turnOrigin"]
    path = write_lines(tmp_path / "t.jsonl", [bad, user_entry("p", "human", mins(1))])
    assert pt.find_prompt_entry(path, "p") == (None, "entry-malformed")


def test_find_entry_unknown_origin(tmp_path):
    path = write_lines(tmp_path / "t.jsonl", [user_entry("p", "scheduled-task", mins(1))])
    assert pt.find_prompt_entry(path, "p") == (None, "entry-origin-unknown")


def test_find_entry_origin_match_is_exact(tmp_path):
    path = write_lines(tmp_path / "t.jsonl", [user_entry("p", "Human", mins(1))])
    assert pt.find_prompt_entry(path, "p") == (None, "entry-origin-unknown")


def test_find_entry_in_a_full_file_with_assistant_and_attachment_lines(tmp_path):
    pid = json.loads((FIXTURES / "f1-04-Stop.json").read_text())["prompt_id"]
    extra = fixture_lines("f1-04-Stop.transcript_extra.jsonl")
    lines = fixture_lines("f1-04-Stop.transcript.jsonl") + extra
    path = write_lines(tmp_path / "t.jsonl", [{"type": "assistant", "message": {}}] + lines)
    entry, cause = pt.find_prompt_entry(path, pid)
    assert (cause, entry.turn_origin, entry.index) == (None, "sdk", 1)


def test_newest_human_before_picks_the_last_human_line_before_the_entry(tmp_path):
    path = write_lines(tmp_path / "t.jsonl", [
        user_entry("h1", "human", mins(40)),
        user_entry("h2", "human", mins(20)),
        user_entry("a", "sdk", mins(15)),
        user_entry("p", "task_notification", mins(1)),
        user_entry("h3", "human", mins(0)),
    ])
    entry, _ = pt.find_prompt_entry(path, "p")
    found = pt.newest_human_before(path, entry)
    assert (found.prompt_id, found.turn_origin, found.index) == ("h2", "human", 1)
    assert found.timestamp == mins(20)


def test_newest_human_before_skips_meta_unmarked_unparsable_and_nonhuman_lines(tmp_path):
    feedback = fixture_lines("f2-04-Stop.transcript.jsonl")[1]  # isMeta, no turnOrigin
    assert feedback.get("isMeta") is True and "turnOrigin" not in feedback
    bad_ts = user_entry("bad", "human", mins(5))
    bad_ts["timestamp"] = "soon"
    path = write_lines(tmp_path / "t.jsonl", [
        user_entry("good", "human", mins(30)),
        user_entry("meta", "human", mins(20), isMeta=True),
        _TOOL_RESULT | {"promptId": "tool"},
        feedback,
        bad_ts,
        user_entry("peer", "peer", mins(3)),
        user_entry("p", "sdk", mins(1)),
    ])
    entry, _ = pt.find_prompt_entry(path, "p")
    assert pt.newest_human_before(path, entry).prompt_id == "good"


def test_newest_human_before_none_when_no_earlier_human(tmp_path):
    path = write_lines(tmp_path / "t.jsonl", [user_entry("p", "sdk", mins(1)), user_entry("h", "human", mins(0))])
    entry, _ = pt.find_prompt_entry(path, "p")
    assert pt.newest_human_before(path, entry) is None


# ================================================================ fixture payloads

STOP_FIXTURES = {
    # name: (expected origin, expected held)
    "a1-03": ("sdk", ("C", "W")),             # running shell, last message DONE
    "a1-05": ("task_notification", ()),
    "b1-04": ("sdk", ()),
    "c1-04": ("sdk", ()),
    "d1-03": ("sdk", ()),
    "e1-03": ("sdk", ("W",)),                 # running subagent, last message DONE
    "f1-04": ("sdk", ()),                     # continuation: stop_hook_active true
    "g01-03": ("sdk", ()),
    "g21-05": ("task_notification", ()),
}


@pytest.mark.parametrize("name", sorted(STOP_FIXTURES))
def test_captured_stop_payloads(make, tmp_path, name):
    origin, held = STOP_FIXTURES[name]
    s = make()
    payload = json.loads((FIXTURES / f"{name}-Stop.json").read_text(encoding="ascii"))
    transcript = tmp_path / "captured.jsonl"
    transcript.write_bytes((FIXTURES / f"{name}-Stop.transcript.jsonl").read_bytes())
    payload["transcript_path"] = str(transcript)
    entry, cause = pt.find_prompt_entry(str(transcript), payload["prompt_id"])
    assert cause is None and entry.turn_origin == origin
    decision = pstop.evaluate(payload, s.cfg, {"CLAUDE_BOOTSTRAP_DATA_ROOT": str(tmp_path / "d")}, NOW)
    assert decision.held == held
    assert decision.system_message is None  # entry found, lists present: no row-2 notice


def test_continuation_flag_is_ignored(make, tmp_path):
    # f1-03 (first Stop) and f1-04 (continuation) share a prompt id and are evaluated alike.
    s = make()
    first = json.loads((FIXTURES / "f1-03-Stop.json").read_text(encoding="ascii"))
    second = json.loads((FIXTURES / "f1-04-Stop.json").read_text(encoding="ascii"))
    assert first["prompt_id"] == second["prompt_id"]
    assert (first["stop_hook_active"], second["stop_hook_active"]) == (False, True)
    transcript = tmp_path / "f1.jsonl"
    transcript.write_bytes((FIXTURES / "f1-04-Stop.transcript.jsonl").read_bytes())
    env = {"CLAUDE_BOOTSTRAP_DATA_ROOT": str(tmp_path / "d")}
    for payload in (first, second):
        payload["transcript_path"] = str(transcript)
        decision = pstop.evaluate(payload, s.cfg, env, NOW)
        assert decision == pstop.StopDecision(None, None, ())


def test_stop_hook_active_does_not_change_the_nudge_budget(s):
    s.entry(PID, "sdk", NOW)
    s.go_afk()
    first = s.stop(stop_hook_active=False)
    second = s.stop(stop_hook_active=True)
    assert first.nudge == second.nudge == line_E()
    assert s.state()["nudges"][PID] == 2


# =============================================================== row 1 (config error)

def test_row1_config_error_reports_allows_and_changes_nothing(tmp_path):
    bad = tmp_path / "cfg" / "config" / "presence-kit.json"
    bad.parent.mkdir(parents=True)
    bad.write_text('{"behaviors": {"no-such-behavior": true}}', encoding="ascii")
    with pytest.raises(pc.ConfigError) as caught:
        pc.resolve(None, {"CLAUDE_CONFIG_DIR": str(tmp_path / "cfg")})
    decision = pstop.config_error_decision(caught.value, {"session_id": SID, "prompt_id": PID})
    expected = L("config", PID, f"{caught.value.text} Presence behavior is unavailable until the file is fixed.")
    assert decision == pstop.StopDecision(None, expected, ())
    assert decision.to_output() == {"systemMessage": expected}
    assert not (tmp_path / "data").exists()  # no state touched


# ================================================================ row 2 (notices)

N1 = L("afk-on-quiet,recap-on-return", PID, "skipped quiet-afk and return detection: the transcript has no user entry for this prompt yet.")
N2 = L("afk-on-quiet,recap-on-return", PID, "skipped quiet-afk and return detection: the prompt's transcript entry has no valid timestamp or turnOrigin.")
N3 = L("afk-on-quiet,recap-on-return", PID, "skipped quiet-afk and return detection: the prompt's turnOrigin is not a known value.")
N4_ABSENT = L("waiting-guard,ending-recap", PID, "skipped waiting and ending checks: the Stop payload's background_tasks is missing.")
N4_BAD = L("waiting-guard,ending-recap", PID, "skipped waiting and ending checks: the Stop payload's background_tasks is not a list of objects.")
N5_ABSENT = L("waiting-guard,ending-recap", PID, "skipped owned-work checks: the Stop payload's session_crons is missing.")
N5_BAD = L("waiting-guard,ending-recap", PID, "skipped owned-work checks: the Stop payload's session_crons is not a list.")


def test_row2_missing_entry_notice_once_per_session_per_cause(s):
    first = s.stop()  # no transcript file at all
    assert first.system_message == N1
    assert s.state()["notices"] == ["entry-missing"]
    assert s.stop("prompt-2").system_message is None  # same cause, same session


@pytest.mark.parametrize("bad_path", ["", None, 42])
def test_row2_unusable_transcript_path_is_a_missing_entry(s, bad_path):
    s.entry(PID, "human", NOW)
    assert s.stop(transcript_path=bad_path).system_message == N1


def test_row2_malformed_entry_notice(s):
    s.entry(PID, "human", NOW, timestamp="not a time")
    assert s.stop().system_message == N2
    assert s.state()["notices"] == ["entry-malformed"]


def test_row2_unknown_origin_notice(s):
    s.entry(PID, "scheduled-task", NOW)
    assert s.stop().system_message == N3
    assert s.state()["notices"] == ["entry-origin-unknown"]


@pytest.mark.parametrize("value,expected", [
    (ABSENT, N4_ABSENT), (None, N4_BAD), ("running", N4_BAD), ({"id": "x"}, N4_BAD),
    ([SHELL, "oops"], N4_BAD), ([SHELL, None], N4_BAD),
])
def test_row2_background_tasks_notices(s, value, expected):
    s.go_afk()
    s.entry(PID, "sdk", NOW)
    decision = s.stop(tasks=value)
    assert decision.system_message == expected
    assert decision.nudge is None and decision.held == ()  # rows 5-7 skipped: no C, W or E
    s.entry("prompt-2", "sdk", NOW)
    assert s.stop("prompt-2", tasks=value).system_message is None


@pytest.mark.parametrize("value,expected", [
    (ABSENT, N5_ABSENT), (None, N5_BAD), ("*/5", N5_BAD), ({"cron": "x"}, N5_BAD),
])
def test_row2_session_crons_notices_skip_only_the_owned_rows(s, value, expected):
    s.go_afk()
    s.entry(PID, "sdk", NOW)
    decision = s.stop(tasks=[SHELL], crons=value, last="DONE")
    assert decision.system_message == expected
    assert decision.held == ("C",)  # row 5 still runs; W (owned) and E are skipped
    quiet = s.stop("prompt-2", tasks=[], crons=value)
    assert quiet.held == ()  # E needs "owned" known


def test_row2_empty_lists_are_the_normal_no_work_state(s):
    s.entry(PID, "sdk", NOW)
    s.go_afk()
    decision = s.stop(tasks=[], crons=[])
    assert decision.system_message is None
    assert decision.held == ("E",)
    assert s.state()["notices"] == []


def test_row2_several_causes_join_in_entry_tasks_crons_order(s):
    decision = s.stop(tasks=ABSENT, crons=ABSENT)
    assert decision.system_message == "\n".join([N1, N4_ABSENT, N5_ABSENT])
    assert s.state()["notices"] == ["entry-missing", "background-tasks-absent", "session-crons-absent"]


def test_row2_missing_entry_skips_rows_3_and_4_but_not_the_rest(s):
    s.go_afk()
    decision = s.stop()  # afk, no transcript
    assert s.state()["presence"] == "afk"
    assert decision.held == ("E",)
    assert decision.system_message == N1 and decision.nudge == line_E()
    assert decision.to_output() == {
        "systemMessage": N1,
        "hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": line_E()},
    }


# ======================================================================== row 3

def quiet_setup(s, gap_minutes=12, origin="sdk"):
    s.human("h0", mins(gap_minutes))
    s.entry(PID, origin, NOW)


def test_row3_quiet_gap_sets_afk_since_the_last_human_entry(s):
    quiet_setup(s, 12)
    decision = s.stop(**owned_quiet())
    state = s.state()
    assert (state["presence"], state["afk_since"], state["greeted"]) == ("afk", ps.format_timestamp(mins(12)), False)
    assert decision == pstop.StopDecision(None, None, ())


def test_row3_gap_within_the_threshold_stays_present(s):
    quiet_setup(s, 5)
    s.stop(**owned_quiet())
    assert s.state()["presence"] == "present"


def test_row3_gap_equal_to_the_threshold_is_not_over_it(s):
    quiet_setup(s, 10)
    s.stop(**owned_quiet())
    assert s.state()["presence"] == "present"


def test_row3_no_earlier_human_entry_skips_silently(s):
    s.entry(PID, "sdk", NOW)
    decision = s.stop(**owned_quiet())
    assert s.state()["presence"] == "present"
    assert decision.system_message is None and s.state()["notices"] == []


def test_row3_a_human_entry_after_the_prompt_entry_does_not_count(s):
    s.entry(PID, "sdk", NOW)
    s.human("later", mins(30))  # file order is what counts
    s.stop(**owned_quiet())
    assert s.state()["presence"] == "present"


def test_row3_human_entry_is_not_row3(s):
    s.human("h0", mins(30))
    s.human(PID, NOW)
    s.stop(**owned_quiet())
    assert s.state()["presence"] == "present"


@pytest.mark.parametrize("origin", ["task_notification", "peer", "sdk"])
def test_row3_any_known_nonhuman_origin_can_trigger(s, origin):
    quiet_setup(s, 12, origin)
    s.stop(**owned_quiet())
    assert s.state()["presence"] == "afk"


def test_row3_already_afk_keeps_the_earlier_afk_since(s):
    s.go_afk(since=mins(60))
    quiet_setup(s, 12)
    s.stop(**owned_quiet())
    assert s.state()["afk_since"] == ps.format_timestamp(mins(60))


def test_matrix_afk_on_quiet_off_stays_present_on_sets_afk(make):
    outcomes = {}
    for enabled in (False, True):
        sess = make(behaviors={"afk-on-quiet": enabled}, name=f"quiet-{enabled}")
        quiet_setup(sess, 12)
        sess.stop(**owned_quiet())
        outcomes[enabled] = sess.state()["presence"]
    assert outcomes == {False: "present", True: "afk"}


def test_matrix_afk_quiet_minutes_15_vs_10_at_a_12_minute_gap(make):
    outcomes = {}
    for minutes in (10, 15):
        sess = make(values={"afk_quiet_minutes": minutes}, name=f"minutes-{minutes}")
        quiet_setup(sess, 12)
        sess.stop(**owned_quiet())
        outcomes[minutes] = sess.state()["presence"]
    assert outcomes == {10: "afk", 15: "present"}


# ======================================================================== row 4

def return_setup(s, afk_minutes=30, return_minutes=1, pid=PID):
    s.go_afk(since=mins(afk_minutes))
    s.human(pid, mins(return_minutes))


def test_row4_return_sets_present_and_holds_r(s):
    return_setup(s)
    decision = s.stop(**owned_quiet())
    state = s.state()
    assert state["presence"] == "present"
    assert state["afk_since"] == ps.format_timestamp(mins(30))  # kept for the recap window
    assert state["pending_return"] == PID
    assert decision.held == ("R",)
    assert decision.nudge == line_R()
    assert state["nudges"] == {PID: 1}


def test_row4_return_with_recap_on_return_off_ends_present_with_no_nudge(make):
    s = make(behaviors={"recap-on-return": False})
    return_setup(s)
    decision = s.stop(**owned_quiet())
    state = s.state()
    assert (state["presence"], state["pending_return"], state["nudges"]) == ("present", None, {})
    assert decision == pstop.StopDecision(None, None, ())
    assert decision.to_output() is None


def test_matrix_recap_on_return_off_no_r_on_r(make):
    outcomes = {}
    for enabled in (False, True):
        sess = make(behaviors={"recap-on-return": enabled}, name=f"recap-{enabled}")
        return_setup(sess)
        outcomes[enabled] = (sess.stop(**owned_quiet()).held, sess.state()["presence"])
    assert outcomes == {False: ((), "present"), True: (("R",), "present")}


def test_row4_entry_not_newer_than_afk_since_stays_afk(s):
    s.go_afk(since=mins(30))
    s.human(PID, mins(40))
    decision = s.stop(**owned_quiet())
    assert s.state()["presence"] == "afk"
    assert decision.held == ()


def test_row4_nonhuman_entry_does_not_return_the_user(s):
    s.go_afk(since=mins(30))
    s.entry(PID, "task_notification", mins(1))
    decision = s.stop(tasks=[], crons=[])
    assert s.state()["presence"] == "afk"
    assert decision.held == ("E",)


def test_row4_return_already_recapped_for_this_prompt_holds_no_r(s):
    # afk entered after the return recap of this prompt: marker present, state afk again
    s.set_state(recaps={PID: "return"})
    return_setup(s)
    decision = s.stop(**owned_quiet())
    assert s.state()["presence"] == "present"
    assert (s.state()["pending_return"], decision.held) == (None, ())


def test_row4_afk_without_afk_since_fails_loudly(s):
    s.set_state(presence="afk")
    s.human(PID, NOW)
    with pytest.raises(ps.StateError) as caught:
        s.stop(**owned_quiet())
    assert caught.value.kind == "malformed" and "afk_since" in caught.value.text


# ======================================================================== row 5

@pytest.mark.parametrize("task", [
    SHELL,
    {"type": "monitor", "status": "running"},
    {"type": "mcp", "status": "running"},
    {"type": "cloud-session", "status": "running"},
    {"status": "running"},
    {"type": 3, "status": "running"},
    {},
])
def test_row5_a_nonagent_task_alone_holds_c(s, task):
    s.entry(PID, "sdk", NOW)
    decision = s.stop(tasks=[task], last=WAITING_LAST)
    assert decision.held == ("C",)
    assert decision.nudge == line_C()


@pytest.mark.parametrize("tasks", [
    [AGENT],
    [{"type": "workflow", "status": "running"}],
    [SHELL, AGENT],
    [AGENT, SHELL],
    [SHELL, {"type": "workflow"}],
])
def test_row5_an_agent_or_workflow_task_covers_the_job(s, tasks):
    s.entry(PID, "sdk", NOW)
    assert s.stop(tasks=tasks, last=WAITING_LAST).held == ()


def test_row5_crons_alone_hold_no_c(s):
    s.entry(PID, "sdk", NOW)
    assert s.stop(tasks=[], crons=[{"id": "c"}], last=WAITING_LAST).held == ()


def test_matrix_waiting_guard_off_no_c_or_w_on_c_and_w(make):
    outcomes = {}
    for enabled in (False, True):
        sess = make(behaviors={"waiting-guard": enabled}, name=f"guard-{enabled}")
        sess.entry(PID, "sdk", NOW)
        outcomes[enabled] = sess.stop(tasks=[SHELL], last="DONE").held
    assert outcomes == {False: (), True: ("C", "W")}


# ======================================================================== row 6

@pytest.mark.parametrize("last,holds", [
    ("Waiting on the build", False),
    ("Working...\nWaiting on the build", False),
    ("Waiting on the build\n\n  \n", False),
    ("  Waiting on x  ", False),
    ("Waiting on", True),
    ("Waiting on ", True),
    ("waiting on x", True),
    ("Waiting on x\nand then some", True),
    ("I am Waiting on x", True),
    ("", True),
    ("\n\n", True),
    ("DONE", True),
    (ABSENT, True),
    (None, True),
    (42, True),
])
def test_row6_last_nonempty_line_must_be_waiting_on_something(s, last, holds):
    s.entry(PID, "sdk", NOW)
    decision = s.stop(tasks=[AGENT], last=last)
    assert decision.held == (("W",) if holds else ())
    if holds:
        assert decision.nudge == line_W()


def test_row6_owned_through_session_crons_only(s):
    s.entry(PID, "sdk", NOW)
    assert s.stop(tasks=[], crons=[{"id": "c"}], last="DONE").held == ("W",)


def test_row6_nothing_owned_no_w(s):
    s.entry(PID, "sdk", NOW)
    assert s.stop(tasks=[], crons=[], last="DONE").held == ()


# ======================================================================== row 7

def test_row7_afk_with_nothing_owned_holds_e_afk(s):
    s.go_afk()
    s.entry(PID, "sdk", NOW)
    decision = s.stop()
    assert decision.held == ("E",)
    assert decision.nudge == L("ending-recap", PID, f"invoke `presence-kit:recap afk-ending {PID}`.")


def test_row7_autonomy_variant_names_the_goal(s):
    s.go_afk(goal="ship the migration")
    s.entry(PID, "sdk", NOW)
    assert s.stop().nudge == L(
        "ending-recap", PID,
        f"autonomous: if ship the migration is done, invoke `presence-kit:recap autonomous-done {PID}`, else keep working.",
    )


def test_row7_present_user_gets_no_e(s):
    s.entry(PID, "sdk", NOW)
    assert s.stop().held == ()


@pytest.mark.parametrize("kwargs", [
    dict(tasks=[SHELL], last=WAITING_LAST),
    dict(tasks=[], crons=[{"id": "c"}], last=WAITING_LAST),
    dict(tasks=[AGENT], last=WAITING_LAST),
])
def test_row7_owned_work_holds_no_e(s, kwargs):
    s.go_afk()
    s.entry(PID, "sdk", NOW)
    assert "E" not in s.stop(**kwargs).held


@pytest.mark.parametrize("trigger,expect_e", [("afk-ending", False), ("autonomous-done", False), ("return", True)])
def test_row7_only_ending_recaps_for_this_prompt_close_e(s, trigger, expect_e):
    s.go_afk()
    s.entry(PID, "sdk", NOW)
    s.set_state(recaps={PID: trigger})
    assert s.stop().held == (("E",) if expect_e else ())


def test_row7_an_ending_recap_for_another_prompt_does_not_close_e(s):
    s.go_afk()
    s.entry(PID, "sdk", NOW)
    s.set_state(recaps={"other": "afk-ending"})
    assert s.stop().held == ("E",)


def test_row7_e_never_coexists_with_r_c_or_w(s):
    # E needs nothing owned and R not held; C and W need owned work; R needs presence present.
    s.go_afk()
    s.entry(PID, "sdk", NOW)
    assert s.stop(tasks=[SHELL], last="DONE").held == ("C", "W")
    return_setup(s, pid="prompt-2")
    assert s.stop("prompt-2", tasks=[], crons=[]).held == ("R",)


def test_matrix_ending_recap_off_no_e_on_e(make):
    outcomes = {}
    for enabled in (False, True):
        sess = make(behaviors={"ending-recap": enabled}, name=f"ending-{enabled}")
        sess.go_afk()
        sess.entry(PID, "sdk", NOW)
        outcomes[enabled] = sess.stop().held
    assert outcomes == {False: (), True: ("E",)}


# ================================================================ D8 and combining

def test_d8_rows_5_to_9_read_state_after_rows_3_and_4_write(s):
    # Present, quiet gap of 12 minutes, nothing owned: row 3 sets afk and E holds in this stop.
    quiet_setup(s, 12)
    decision = s.stop(tasks=[], crons=[])
    assert s.state()["presence"] == "afk"
    assert decision.held == ("E",)
    assert decision.nudge == line_E()


def test_d8_return_row4_present_state_is_seen_by_the_rows_after_it(s):
    # afk -> row 4 flips to present, so E does not hold in the same stop; R does.
    return_setup(s)
    assert s.stop(tasks=[], crons=[]).held == ("R",)


def test_r_c_w_hold_together_in_one_nudge_in_order(s):
    return_setup(s)
    decision = s.stop(tasks=[SHELL], last="DONE")
    assert decision.held == ("R", "C", "W")
    assert decision.nudge == "\n".join([line_R(), line_C(), line_W()])
    assert s.state()["nudges"] == {PID: 1}  # one nudge, one count
    assert decision.to_output() == {
        "hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": decision.nudge}
    }


def test_each_nudge_line_names_its_id_the_config_doc_and_the_prompt(s):
    return_setup(s)
    decision = s.stop(tasks=[SHELL], last="DONE")
    for text in decision.nudge.split("\n"):
        assert text.startswith("presence-kit [") and f"] prompt {PID}: " in text
        assert text.endswith(" See references/configuring.md.")


def test_evaluate_makes_one_state_update_per_stop(s, monkeypatch):
    calls = []
    real = ps.update
    monkeypatch.setattr(ps, "update", lambda *a, **k: calls.append(1) or real(*a, **k))
    quiet_setup(s, 12)
    s.stop(tasks=[], crons=[])
    assert len(calls) == 1


# ================================================================== rows 8 and 9

def test_row8_budget_three_nudges_then_system_message_and_unresolved(s):
    return_setup(s)
    kwargs = dict(tasks=[SHELL], last="DONE")
    nudges = [s.stop(**kwargs).nudge for _ in range(3)]
    expected = "\n".join([line_R(), line_C(), line_W()])
    assert nudges == [expected] * 3
    assert s.state()["nudges"] == {PID: 3}

    fourth = s.stop(**kwargs)
    assert fourth.nudge is None and fourth.held == ("R", "C", "W")
    assert fourth.system_message == "\n".join([
        L("recap-on-return", PID, "unresolved after 3 nudges: return recap not run."),
        L("waiting-guard", PID, "unresolved after 3 nudges: no waiter agent covering the job."),
        L("waiting-guard", PID, "unresolved after 3 nudges: turn did not end with `Waiting on <what>`."),
    ])
    state = s.state()
    assert state["nudges"] == {PID: 3}  # exhaustion does not count
    assert state["pending_return"] is None
    assert state["unresolved"] == [
        f"recap-on-return: return recap not run (prompt {PID})",
        f"waiting-guard: no waiter agent covering the job (prompt {PID})",
        f"waiting-guard: turn did not end with `Waiting on <what>` (prompt {PID})",
    ]
    assert fourth.to_output() == {"systemMessage": fourth.system_message}

    fifth = s.stop(**kwargs)  # R was cleared by the exhaustion; C and W still held
    assert fifth.nudge is None and fifth.held == ("C", "W")
    assert len(fifth.system_message.split("\n")) == 2
    assert len(s.state()["unresolved"]) == 3  # deduplicated


def test_row8_budget_is_per_prompt_id(s):
    s.go_afk()
    s.entry(PID, "sdk", NOW)
    s.entry("prompt-2", "sdk", NOW)
    for _ in range(3):
        assert s.stop(PID).nudge == line_E(PID)
    assert s.stop(PID).nudge is None
    assert s.stop("prompt-2").nudge == line_E("prompt-2")  # fresh budget
    assert s.state()["nudges"] == {PID: 3, "prompt-2": 1}


def test_row8_exhaustion_for_e_names_its_label(s):
    s.go_afk()
    s.entry(PID, "sdk", NOW)
    for _ in range(3):
        s.stop()
    final = s.stop()
    assert final.system_message == L("ending-recap", PID, "unresolved after 3 nudges: no ending recap or goal check.")
    assert s.state()["unresolved"] == [f"ending-recap: no ending recap or goal check (prompt {PID})"]


def test_row8_nudge_and_a_row2_notice_share_one_output_object(s):
    s.entry(PID, "sdk", NOW)
    s.entry("prompt-2", "sdk", NOW)
    decision = s.stop(tasks=[SHELL], crons=ABSENT, last=WAITING_LAST)  # C still runs; N5 is new
    assert decision.held == ("C",)
    assert decision.to_output() == {
        "systemMessage": N5_ABSENT,
        "hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": line_C()},
    }
    again = s.stop("prompt-2", tasks=[SHELL], crons=ABSENT, last=WAITING_LAST)
    assert again.system_message is None and again.nudge == line_C("prompt-2")  # notice only once


def test_row9_nothing_held_allows_silently(s):
    s.entry(PID, "sdk", NOW)
    decision = s.stop(**owned_quiet())
    assert decision == pstop.StopDecision(None, None, ())
    assert decision.to_output() is None


@pytest.mark.parametrize("trigger,greeted", [(None, False), ("return", False), ("afk-ending", True), ("autonomous-done", True)])
def test_row9_greeted_follows_the_ending_recap_marker(s, trigger, greeted):
    s.go_afk()
    s.set_state(greeted=not greeted)
    if trigger:
        s.set_state(recaps={PID: trigger})
    s.entry(PID, "sdk", NOW)
    s.stop(**owned_quiet())
    assert s.state()["greeted"] is greeted


def test_row9_greeted_is_untouched_while_something_is_held(s):
    s.go_afk()
    s.set_state(greeted=True)
    s.entry(PID, "sdk", NOW)
    assert s.stop().held == ("E",)
    assert s.state()["greeted"] is True


# ============================================================ exactly one recap each

def test_autonomous_completion_yields_exactly_one_recap(s):
    s.go_afk(goal="finish the audit")
    s.entry(PID, "sdk", NOW)
    assert "autonomous-done" in s.stop().nudge
    s.recap_done("autonomous-done")
    done = s.stop()
    assert done == pstop.StopDecision(None, None, ())
    state = s.state()
    assert (state["goal"], state["greeted"], state["recaps"]) == (None, True, {PID: "autonomous-done"})
    assert s.stop() == pstop.StopDecision(None, None, ())  # no afk-ending recap follows


def test_afk_ending_yields_exactly_one_recap(s):
    s.go_afk()
    s.entry(PID, "sdk", NOW)
    assert s.stop().nudge == line_E()
    s.recap_done("afk-ending")
    assert s.stop() == pstop.StopDecision(None, None, ())
    state = s.state()
    assert (state["presence"], state["greeted"]) == ("afk", True)


def test_return_yields_exactly_one_recap(s):
    return_setup(s)
    assert s.stop().nudge == line_R()
    assert s.stop().nudge == line_R()  # R persists across the nudges though presence is present
    s.recap_done("return")
    assert s.stop() == pstop.StopDecision(None, None, ())
    state = s.state()
    assert (state["presence"], state["pending_return"], state["unresolved"]) == ("present", None, [])
    assert s.stop() == pstop.StopDecision(None, None, ())  # and no ending recap follows


# ======================================================== origins and statuses

def test_unknown_turn_origin_skips_rows_3_and_4_with_a_notice(s):
    s.human("h0", mins(30))
    s.entry(PID, "scheduled-task", NOW)
    decision = s.stop(**owned_quiet())
    assert s.state()["presence"] == "present"  # row 3 skipped despite the 30-minute gap
    assert decision.system_message == N3


def test_unknown_turn_origin_does_not_return_an_afk_user_but_rows_5_to_9_run(s):
    s.go_afk(since=mins(60))
    s.entry(PID, "mystery", NOW)
    decision = s.stop(tasks=[], crons=[])
    assert s.state()["presence"] == "afk"  # row 4 skipped
    assert decision.held == ("E",)
    assert decision.system_message == N3


@pytest.mark.parametrize("status", ["completed", "failed", "killed", "stopped", "never-seen-before", None, 7])
def test_unobserved_background_task_status_counts_as_running(s, status):
    s.go_afk()
    s.entry(PID, "sdk", NOW)
    task = {"id": "t", "type": "shell", "status": status}
    decision = s.stop(tasks=[task], last="DONE")
    assert decision.held == ("C", "W")  # owned: no E, and the job still needs a waiter


def test_status_key_missing_still_counts(s):
    s.entry(PID, "sdk", NOW)
    assert s.stop(tasks=[{"type": "shell"}], last=WAITING_LAST).held == ("C",)


# ================================================================ output shapes

def test_decision_output_shapes():
    nudge = pstop.StopDecision("a\nb", None, ("C", "W"))
    note = pstop.StopDecision(None, "n1\nn2", ())
    both = pstop.StopDecision("a", "n", ("C",))
    none = pstop.StopDecision(None, None, ())
    assert nudge.to_output() == {"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": "a\nb"}}
    assert note.to_output() == {"systemMessage": "n1\nn2"}
    assert list(both.to_output()) == ["systemMessage", "hookSpecificOutput"]
    assert none.to_output() is None
    assert json.dumps(both.to_output()).isascii()


def test_budget_constant():
    assert pstop.NUDGE_BUDGET == 3


# ================================================================ loud failures

@pytest.mark.parametrize("field,value,kind", [
    ("prompt_id", "has space", "bad-prompt-id"),
    ("prompt_id", "", "bad-prompt-id"),
    ("session_id", "../escape", "bad-session-id"),
])
def test_bad_ids_raise_state_errors(s, field, value, kind):
    payload = s.payload()
    payload[field] = value
    with pytest.raises(ps.StateError) as caught:
        pstop.evaluate(payload, s.cfg, s.env, NOW)
    assert caught.value.kind == kind


def test_malformed_state_file_raises_and_is_not_repaired(s):
    path = ps.state_path(SID, s.env)
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="ascii")
    s.entry(PID, "sdk", NOW)
    with pytest.raises(ps.StateError) as caught:
        s.stop()
    assert caught.value.kind == "malformed"
    assert path.read_text(encoding="ascii") == "{broken"


def test_an_exception_inside_the_update_leaves_no_partial_write(s, monkeypatch):
    quiet_setup(s, 12)

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(ps, "bump_nudge", boom)
    with pytest.raises(RuntimeError):
        s.stop(tasks=[], crons=[])
    assert not ps.state_path(SID, s.env).exists()  # rows 3-4 and 8 are one atomic update


# ============================================ K3: the prompt hook's departure and row 4

# The real a1 capture: the transcript entry (17:16:10.089Z) precedes the UserPromptSubmit
# hook (17:16:10.265) by 176 ms, in 8 of 8 captures. afk_since is truncated to the second,
# so the entry looks NEWER than afk_since and row 4 used to count the departure prompt itself
# as a return.
A1_ENTRY = datetime(2026, 10, 17, 17, 16, 10, 89000, tzinfo=timezone.utc)
A1_HOOK = datetime(2026, 10, 17, 17, 16, 10, 265000, tzinfo=timezone.utc)


def hook(s, pid, prompt, now):
    """The real prompt hook for this session; returns its PromptOutput."""
    payload = {"session_id": SID, "prompt_id": pid, "prompt": prompt}
    return pprompt.evaluate(payload, s.cfg, s.env, now)


def test_departure_prompt_is_not_a_return(s):
    """The row-4 guard, from the real a1 timings. With the guard reverted: present + R, not afk + E."""
    hook(s, PID, "going afk for lunch", A1_HOOK)
    state = s.state()
    assert state["afk_since"] == "2026-10-17T17:16:10Z"
    assert A1_ENTRY > ps.parse_timestamp(state["afk_since"])     # the timing that fooled row 4
    s.human(PID, A1_ENTRY)
    decision = s.stop(now=A1_HOOK + timedelta(seconds=5), tasks=[], crons=[])
    state = s.state()
    assert (state["presence"], state["afk_prompt"], state["pending_return"]) == ("afk", PID, None)
    assert decision.held == ("E",)
    assert decision.nudge == line_E()


def test_a_later_prompt_still_returns_the_user_after_a_hook_departure(s):
    """Paired with the guard test: the guard is the one prompt, not a blanket skip."""
    hook(s, PID, "going afk for lunch", A1_HOOK)
    s.human(PID, A1_ENTRY)
    s.stop(now=A1_HOOK + timedelta(seconds=5), tasks=[], crons=[])
    later = A1_HOOK + timedelta(minutes=20)
    s.human("prompt-2", later)
    decision = s.stop(pid="prompt-2", now=later + timedelta(seconds=5), **owned_quiet())
    state = s.state()
    assert (state["presence"], state["pending_return"]) == ("present", "prompt-2")
    assert state["afk_prompt"] is None                     # ends with the afk period
    assert decision.held == ("R",)
    assert decision.nudge == line_R("prompt-2")


def test_a_repeated_departure_while_afk_is_not_a_return(s):
    """Ruling 19: the hook keeps the user afk and Stop does not count that prompt as a return."""
    s.go_afk(since=mins(30))
    s.human("prompt-2", NOW)
    out = hook(s, "prompt-2", "afk again", NOW)
    assert "kept the user afk" in out.additional_context
    decision = s.stop(pid="prompt-2", tasks=[], crons=[])
    state = s.state()
    assert (state["presence"], state["afk_prompt"]) == ("afk", "prompt-2")
    assert state["afk_since"] == ps.format_timestamp(mins(30))      # the recap window still starts at the first departure
    assert (decision.held, state["pending_return"]) == (("E",), None)


def test_a_returning_user_kept_afk_by_the_hook_and_undone_by_claude_is_present(s):
    """Cross-check fix 1 end to end: kept afk, then `state set present`, then Stop."""
    s.go_afk(since=mins(30))
    s.human("prompt-2", NOW)
    hook(s, "prompt-2", "what does afk do?", NOW)
    ps.state_set(SID, "present", "user", None, None, s.env, NOW)
    decision = s.stop(pid="prompt-2", **owned_quiet())
    state = s.state()
    assert (state["presence"], state["afk_prompt"], state["pending_return"]) == ("present", None, None)
    assert decision.held == ()


def test_the_guard_compares_the_payload_prompt_id_to_afk_prompt(s):
    """Fix 3: Entry.prompt_id is built from the same pid, so the comparison is on the payload's."""
    s.go_afk(since=mins(30))
    s.set_state(afk_prompt=PID)
    s.human(PID, mins(1))
    assert s.stop(**owned_quiet()).held == ()
    assert s.state()["presence"] == "afk"
    s.human("prompt-2", mins(0))
    assert s.stop(pid="prompt-2", **owned_quiet()).held == ("R",)
    assert s.state()["presence"] == "present"


# --- a return when the prompt's entry is missing (cross-check fix 3) ---

N1_TEXT = "skipped quiet-afk and return detection: the transcript has no user entry for this prompt yet."


def line_N1(pid):
    return L("afk-on-quiet,recap-on-return", pid, N1_TEXT)


def p2_clause(pid):
    return f"invoke `presence-kit:recap return {pid}` before answering."


def test_same_pid_entry_missing_after_a_hook_departure_keeps_afk(s):
    hook(s, PID, "going afk", A1_HOOK)
    decision = s.stop(now=A1_HOOK + timedelta(seconds=5), tasks=[], crons=[])   # no transcript at all
    state = s.state()
    assert (state["presence"], state["afk_prompt"]) == ("afk", PID)
    assert decision.system_message == line_N1(PID)
    assert decision.held == ("E",)


@pytest.mark.parametrize("recap_on_return", [True, False])
def test_later_pid_entry_missing_leaves_the_user_afk_until_something_returns_them(make, recap_on_return):
    s = make(behaviors={"recap-on-return": recap_on_return})
    hook(s, PID, "going afk", A1_HOOK)
    s.human(PID, A1_ENTRY)
    later = A1_HOOK + timedelta(minutes=20)
    out = hook(s, "prompt-2", "back", later)
    # the hook asks for the return recap only with recap-on-return on
    assert (p2_clause("prompt-2") in out.additional_context) is recap_on_return
    decision = s.stop(pid="prompt-2", now=later + timedelta(seconds=5), tasks=[], crons=[])   # prompt-2 has no entry
    state = s.state()
    assert state["presence"] == "afk"                      # row 4 cannot see the entry
    assert decision.system_message == line_N1("prompt-2")
    assert state["pending_return"] is None


def test_a_p2_driven_return_recap_makes_the_user_present_though_the_entry_is_missing(s):
    hook(s, PID, "going afk", A1_HOOK)
    later = A1_HOOK + timedelta(minutes=20)
    hook(s, "prompt-2", "back", later)
    s.recap_done("return", "prompt-2")                      # Claude followed P2
    decision = s.stop(pid="prompt-2", now=later + timedelta(seconds=5), **owned_quiet())
    state = s.state()
    assert (state["presence"], state["pending_return"], state["afk_prompt"]) == ("present", None, None)
    assert state["recaps"]["prompt-2"] == "return"
    assert decision.held == () and decision.system_message == line_N1("prompt-2")


def test_a_later_usable_human_entry_returns_the_user_after_a_missing_one(s):
    hook(s, PID, "going afk", A1_HOOK)
    s.human(PID, A1_ENTRY)
    t2 = A1_HOOK + timedelta(minutes=10)
    s.stop(pid="prompt-2", now=t2, tasks=[], crons=[])      # prompt-2's entry is missing
    assert s.state()["presence"] == "afk"
    t3 = A1_HOOK + timedelta(minutes=20)
    s.human("prompt-3", t3)
    decision = s.stop(pid="prompt-3", now=t3 + timedelta(seconds=5), **owned_quiet())
    assert s.state()["presence"] == "present"
    assert decision.held == ("R",) and decision.nudge == line_R("prompt-3")
