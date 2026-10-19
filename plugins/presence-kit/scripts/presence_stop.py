"""presence-kit Stop-hook evaluation (rows 2-9; row 1 is config_error_decision).

Stdlib only; sibling imports. Contract: dev/tasks/presence-kit/contracts.md
sections 1, 5, 6, 7 and 11. `evaluate` makes ONE presence_state.update call per
stop, so rows 3-4 (which write presence) and rows 5-9 (which read it, D8) see one
consistent document. It returns a StopDecision; presence.py prints it.

The caller (presence.py) has already parsed the payload and validated `session_id`
and `prompt_id` (class `payload` errors); the checks here are a backstop and raise
StateError.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import presence_config as pc
import presence_state as ps
import presence_transcript as pt

NUDGE_BUDGET = 3

# design.md Stop row 5 / contracts.md section 11 item 2: these types are agent work
# (they show "working" by themselves); every other type is non-agent.
AGENT_TYPES = ("subagent", "workflow")
ENDING_RECAPS = ("afk-ending", "autonomous-done")
_WAITING = re.compile(r"Waiting on \S")

# Held-condition codes in nudge order, with the behavior id that owns each.
_CODE_IDS = {
    "R": pc.R_IDS,
    "C": pc.C_IDS,
    "W": pc.W_IDS,
    "E": pc.E_IDS,
}

# Row-2 causes -> (ids, text), section 7 N1-N5.
_NOTICES = {
    pt.CAUSE_MISSING: (pc.N_QUIET_RETURN_IDS, pc.N1_ENTRY_MISSING),
    pt.CAUSE_MALFORMED: (pc.N_QUIET_RETURN_IDS, pc.N2_ENTRY_MALFORMED),
    pt.CAUSE_ORIGIN_UNKNOWN: (pc.N_QUIET_RETURN_IDS, pc.N3_ORIGIN_UNKNOWN),
    "background-tasks-absent": (pc.N_CHECKS_IDS, pc.N4_BACKGROUND_ABSENT),
    "background-tasks-malformed": (pc.N_CHECKS_IDS, pc.N4_BACKGROUND_MALFORMED),
    "session-crons-absent": (pc.N_CHECKS_IDS, pc.N5_CRONS_ABSENT),
    "session-crons-malformed": (pc.N_CHECKS_IDS, pc.N5_CRONS_MALFORMED),
}


@dataclass(frozen=True)
class StopDecision:
    nudge: str | None
    system_message: str | None
    held: tuple[str, ...]

    def to_output(self) -> dict[str, Any] | None:
        """The stop stdout object (contracts.md section 11 item 1), or None for silent allow."""
        out: dict[str, Any] = {}
        if self.system_message is not None:
            out["systemMessage"] = self.system_message
        if self.nudge is not None:
            out["hookSpecificOutput"] = {
                "hookEventName": "Stop",
                "additionalContext": self.nudge,
            }
        return out or None


def config_error_decision(err: pc.ConfigError, payload: dict) -> StopDecision:
    """Row 1: report the config error, allow, change nothing."""
    pid = ps.check_prompt_id(payload.get("prompt_id"))
    text = pc.S1_CONFIG_SYSTEM.format(err_text=err.text)
    return StopDecision(None, pc.line(pc.CONFIG_IDS, pid, text), ())


# ----------------------------------------------------------- payload normalization

def _list_field(payload: dict, name: str, cause: str) -> tuple[list | None, str | None]:
    """The raw list for `name`, or (None, `{cause}-absent` / `{cause}-malformed`)."""
    if name not in payload:
        return None, f"{cause}-absent"
    value = payload[name]
    if not isinstance(value, list):
        return None, f"{cause}-malformed"
    return value, None


def _background_tasks(payload: dict) -> tuple[list[dict] | None, str | None]:
    tasks, cause = _list_field(payload, "background_tasks", "background-tasks")
    if tasks is not None and any(not isinstance(task, dict) for task in tasks):
        return None, "background-tasks-malformed"
    return tasks, cause


def _session_crons(payload: dict) -> tuple[list | None, str | None]:
    return _list_field(payload, "session_crons", "session-crons")


def _is_agent(task: dict) -> bool:
    task_type = task.get("type")
    return isinstance(task_type, str) and task_type in AGENT_TYPES


def _needs_waiter(tasks: list[dict]) -> bool:
    """C: a non-agent task is listed and no agent task is. Every listed task counts as
    running whatever its `status` says (contracts.md section 11 item 2)."""
    return bool(tasks) and not any(_is_agent(task) for task in tasks)


def _ends_with_waiting(last_message: str | None) -> bool:
    if last_message is None:
        return False
    lines = [text.strip() for text in last_message.splitlines() if text.strip()]
    return bool(lines) and _WAITING.match(lines[-1]) is not None


# ------------------------------------------------------------------------- rows

def _notice_lines(doc: dict, pid: str, causes: list[str]) -> list[str]:
    """Row 2: one line per cause not yet recorded for the session."""
    lines = []
    for cause in causes:
        if ps.add_notice(doc, cause):
            ids, text = _NOTICES[cause]
            lines.append(pc.line(ids, pid, text))
    return lines


def _quiet_afk(
    doc: dict, cfg: pc.Config, now: datetime, entry: pt.Entry | None, human: pt.Entry | None
) -> None:
    """Row 3: present, a usable non-human entry, and the newest earlier human entry
    older than afk_quiet_minutes -> afk since that human entry."""
    if not cfg.enabled("afk-on-quiet") or doc["presence"] != "present":
        return
    if entry is None or entry.turn_origin == pt.HUMAN or human is None:
        return
    if now - human.timestamp > timedelta(minutes=cfg.values["afk_quiet_minutes"]):
        ps.enter_afk(doc, ps.format_timestamp(human.timestamp))


def _user_returned(
    doc: dict, cfg: pc.Config, pid: str, entry: pt.Entry | None, state_file: str
) -> None:
    """Row 4: afk and a human entry newer than afk_since -> present, always; R is
    pending only with recap-on-return on and no `return` recap for this prompt.

    The prompt that the prompt hook itself set (or kept) afk on is never a return,
    whatever the timestamps say: the transcript entry precedes the hook's `afk_since`
    by milliseconds, and `afk_since` is truncated to the second, so the entry can
    look newer. The hook records that prompt id as `afk_prompt`."""
    if doc["presence"] != "afk" or entry is None or entry.turn_origin != pt.HUMAN:
        return
    since = ps.parse_timestamp(doc["afk_since"])
    if since is None:
        raise ps.StateError(
            "malformed", f"{state_file}: malformed state file -- presence is afk but afk_since is null"
        )
    if doc["afk_prompt"] == pid or entry.timestamp <= since:
        return
    ps.set_present(doc)
    if cfg.enabled("recap-on-return") and doc["recaps"].get(pid) != "return":
        doc["pending_return"] = pid


def _held_codes(
    doc: dict,
    cfg: pc.Config,
    pid: str,
    tasks: list[dict] | None,
    crons: list | None,
    last_message: str | None,
) -> list[str]:
    """Rows 5-7 over the document AFTER rows 3-4 (D8); codes in R, C, W, E order."""
    recaps = doc["recaps"]
    waiting_guard = cfg.enabled("waiting-guard")
    owned_known = tasks is not None and crons is not None
    owned = owned_known and bool(tasks or crons)

    r = (
        cfg.enabled("recap-on-return")
        and doc["pending_return"] == pid
        and recaps.get(pid) != "return"
    )
    c = waiting_guard and tasks is not None and _needs_waiter(tasks)
    w = waiting_guard and owned and not _ends_with_waiting(last_message)
    e = (
        cfg.enabled("ending-recap")
        and doc["presence"] == "afk"
        and owned_known
        and not owned
        and not r
        and recaps.get(pid) not in ENDING_RECAPS
    )
    return [code for code, held in (("R", r), ("C", c), ("W", w), ("E", e)) if held]


def _nudge_line(code: str, doc: dict, pid: str) -> str:
    if code == "R":
        text = pc.R_RETURN.format(pid=pid)
    elif code == "C":
        text = pc.C_WAITER
    elif code == "W":
        text = pc.W_WAITING
    elif doc["goal"] is not None:
        text = pc.E_AUTONOMOUS.format(goal=doc["goal"], pid=pid)
    else:
        text = pc.E_AFK.format(pid=pid)
    return pc.line(_CODE_IDS[code], pid, text)


def _budget(doc: dict, pid: str, held: list[str]) -> tuple[str | None, list[str]]:
    """Rows 8-9. Returns (nudge, exhaustion lines)."""
    if not held:
        doc["greeted"] = doc["recaps"].get(pid) in ENDING_RECAPS
        return None, []
    if doc["nudges"].get(pid, 0) < NUDGE_BUDGET:
        ps.bump_nudge(doc, pid)
        return "\n".join(_nudge_line(code, doc, pid) for code in held), []
    lines = []
    for code in held:
        ids = _CODE_IDS[code]
        label = pc.S6_LABELS[code]
        lines.append(pc.line(ids, pid, pc.S6_UNRESOLVED.format(label=label)))
        entry = f"{ids}: {label} (prompt {pid})"
        if entry not in doc["unresolved"]:
            doc["unresolved"].append(entry)
    doc["pending_return"] = None
    return None, lines


# --------------------------------------------------------------------- evaluate

def evaluate(payload: dict, cfg: pc.Config, env: Mapping[str, str], now: datetime) -> StopDecision:
    """Rows 2-9 for one Stop payload, in one locked state update. `now` is injected."""
    sid = ps.check_session_id(payload.get("session_id"))
    pid = ps.check_prompt_id(payload.get("prompt_id"))
    transcript = payload.get("transcript_path")
    if not isinstance(transcript, str) or not transcript:
        transcript = None
    last_message = payload.get("last_assistant_message")
    if not isinstance(last_message, str):
        last_message = None
    tasks, tasks_cause = _background_tasks(payload)
    crons, crons_cause = _session_crons(payload)

    entry, entry_cause = pt.find_prompt_entry(transcript, pid)
    human = None
    if entry is not None and entry.turn_origin != pt.HUMAN and cfg.enabled("afk-on-quiet"):
        human = pt.newest_human_before(transcript, entry)
    causes = [cause for cause in (entry_cause, tasks_cause, crons_cause) if cause]
    state_file = str(ps.state_path(sid, env))

    out: list[StopDecision] = []

    def apply(doc: dict) -> None:
        messages = _notice_lines(doc, pid, causes)
        _quiet_afk(doc, cfg, now, entry, human)
        _user_returned(doc, cfg, pid, entry, state_file)
        held = _held_codes(doc, cfg, pid, tasks, crons, last_message)
        nudge, exhaustion = _budget(doc, pid, held)
        ps.prune(doc)
        messages.extend(exhaustion)
        out.append(StopDecision(nudge, "\n".join(messages) or None, tuple(held)))

    ps.update(sid, apply, env)
    return out[0]
