"""presence-kit Stop-hook evaluation (rows 2-9; row 1 is config_error_decision).

Stdlib only; sibling imports. Contract: dev/tasks/presence-kit/contracts.md
sections 1, 5, 6, 7 and 11. Owned work (C, W, E) is every listed task that has not
finished, except an Artifact live-update monitor, plus every session cron. `evaluate` makes ONE presence_state.update call per
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

# These types are agent work (they show "working" by themselves); every other type
# is non-agent and needs a waiter agent.
AGENT_TYPES = ("subagent", "workflow")
ENDING_RECAPS = ("afk-ending", "autonomous-done")
# The Stop payload lists every task in the harness registry, finished ones included,
# until the harness notifies and evicts them. A task in one of these statuses is not
# running, so it is not owned work. Any other status (pending, running, paused, missing,
# unrecognized) counts: a false positive shows in the nudge, a silent miss would not.
TERMINAL_STATUSES = ("completed", "failed", "killed")
# The harness lists a monitor task for every Artifact live subscription, which the
# agent did not arm and cannot usefully stop. Observed Stop payload (21 Oct 2026):
# {"id": "si7wbhhk1", "type": "monitor", "status": "running", "description":
# "live updates for artifact https://claude.ai/artifact/<id> (auto-armed on publish)"}.
# The text after the URL names how the subscription was armed ("auto-armed on
# publish", "attached via /artifacts", "watch requested", "resume requested",
# "re-armed on session resume", "presence while editing via MCP", "watch requested;
# auto-replies ..."), so every such monitor is matched by its prefix.
ARTIFACT_MONITOR_TYPE = "monitor"
ARTIFACT_MONITOR_PREFIX = "live updates for artifact "
DESCRIPTION_LIMIT = 80
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


def _is_artifact_monitor(task: dict) -> bool:
    """An Artifact live-update subscription the harness armed, not the agent."""
    description = task.get("description")
    return (
        task.get("type") == ARTIFACT_MONITOR_TYPE
        and isinstance(description, str)
        and description.startswith(ARTIFACT_MONITOR_PREFIX)
    )


def _is_finished(task: dict) -> bool:
    status = task.get("status")
    return isinstance(status, str) and status in TERMINAL_STATUSES


def _owned_tasks(tasks: list[dict] | None) -> list[dict] | None:
    """The listed tasks that are owned running work: finished tasks and Artifact
    live-update monitors are dropped; None stays None."""
    if tasks is None:
        return None
    return [task for task in tasks if not _is_finished(task) and not _is_artifact_monitor(task)]


def _needs_waiter(tasks: list[dict]) -> bool:
    """C: an owned non-agent task is listed and no owned agent task is. `tasks` is
    already filtered by `_owned_tasks`, so a finished agent covers nothing."""
    return bool(tasks) and not any(_is_agent(task) for task in tasks)


def _ascii(value: Any, fallback: str) -> str:
    """`value` as one-line ASCII text; `fallback` when it is not a non-empty string or number."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return fallback
    text = " ".join(str(value).split()).encode("ascii", "replace").decode("ascii")
    return text or fallback


def describe_task(task: dict) -> str:
    """`<id> (<type>, <status>): <description>`, description cut to DESCRIPTION_LIMIT."""
    description = _ascii(task.get("description"), "no description")
    if len(description) > DESCRIPTION_LIMIT:
        description = description[: DESCRIPTION_LIMIT - 3] + "..."
    return (
        f"{_ascii(task.get('id'), '?')} "
        f"({_ascii(task.get('type'), '?')}, {_ascii(task.get('status'), 'no status')}): "
        f"{description}"
    )


def counted_work(tasks: list[dict] | None, crons: list | None) -> str:
    """The owned work a C or W nudge counted: one entry per task, then the cron count."""
    parts = [describe_task(task) for task in tasks or []]
    if crons:
        parts.append(f"{len(crons)} session cron{'s' if len(crons) != 1 else ''}")
    return "; ".join(parts) or "none"


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


def _record_cron_prompts(doc: dict, cfg: pc.Config, crons: list | None) -> None:
    """Record the live session crons' prompts for the prompt hook's exclusion.

    A cron or /loop firing reaches the prompt hook as its bare prompt (a dynamic
    wakeup as `/loop ...`) with no origin field, so the hook matches the prompt
    against this list. The list is replaced each Stop, so a deleted cron drops
    out. An absent or malformed `session_crons` says nothing about the crons
    (row 2 reports it), so the list is left as it was.
    """
    if crons is None or not cfg.enabled("afk-on-departure"):
        return
    ps.set_cron_prompts(
        doc,
        [
            cron["prompt"]
            for cron in crons
            if isinstance(cron, dict) and isinstance(cron.get("prompt"), str)
        ],
    )


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
    """Rows 5-7 over the document AFTER rows 3-4 (D8); codes in R, C, W, E order.

    Finished tasks and Artifact live-update monitors are dropped first: they are not
    owned work for C, W or E."""
    tasks = _owned_tasks(tasks)
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


def _nudge_line(code: str, doc: dict, pid: str, counted: dict[str, str]) -> str:
    if code == "R":
        text = pc.R_RETURN.format(pid=pid)
    elif code == "C":
        text = pc.C_WAITER.format(counted=counted["C"])
    elif code == "W":
        text = pc.W_WAITING.format(counted=counted["W"])
    elif doc["goal"] is not None:
        text = pc.E_AUTONOMOUS.format(goal=doc["goal"], pid=pid)
    else:
        text = pc.E_AFK.format(pid=pid)
    return pc.line(_CODE_IDS[code], pid, text)


def _budget(
    doc: dict, pid: str, held: list[str], counted: dict[str, str]
) -> tuple[str | None, list[str]]:
    """Rows 8-9. `counted` maps C and W to the owned work each counted.
    Returns (nudge, exhaustion lines)."""
    if not held:
        doc["greeted"] = doc["recaps"].get(pid) in ENDING_RECAPS
        return None, []
    if doc["nudges"].get(pid, 0) < NUDGE_BUDGET:
        ps.bump_nudge(doc, pid)
        return "\n".join(_nudge_line(code, doc, pid, counted) for code in held), []
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
        _record_cron_prompts(doc, cfg, crons)
        _quiet_afk(doc, cfg, now, entry, human)
        _user_returned(doc, cfg, pid, entry, state_file)
        held = _held_codes(doc, cfg, pid, tasks, crons, last_message)
        owned = _owned_tasks(tasks)
        counted = {"C": counted_work(owned, None), "W": counted_work(owned, crons)}
        nudge, exhaustion = _budget(doc, pid, held, counted)
        ps.prune(doc)
        messages.extend(exhaustion)
        out.append(StopDecision(nudge, "\n".join(messages) or None, tuple(held)))

    ps.update(sid, apply, env)
    return out[0]
