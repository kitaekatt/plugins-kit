"""presence-kit transcript reader: the prompt's user entry and the newest human entry.

Stdlib only; imports only presence_state (for timestamp parsing). Contract:
dev/tasks/presence-kit/contracts.md sections 1, 6 and 11 item 3.

The Stop hook reads the session transcript (JSON Lines) to learn who started the
current prompt. The prompt's entry is the FIRST line whose `promptId` equals the
payload's `prompt_id`; it is `type: "user"` and carries `turnOrigin`. Later lines
with the same id (tool results, hook feedback) carry none. Lines are numbered by
their position in the file (0-based, blank lines included), so `Entry.index`
orders entries by file order.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

import presence_state

# Every value but `human` is non-human. `scheduled` is a /loop or cron firing
# (observed on Claude Code 2.1.304: `promptSource: "system"`, `isMeta: true`).
KNOWN_ORIGINS = ("human", "task_notification", "peer", "sdk", "scheduled")
HUMAN = "human"

# Row-2 causes (contracts.md section 6).
CAUSE_MISSING = "entry-missing"
CAUSE_MALFORMED = "entry-malformed"
CAUSE_ORIGIN_UNKNOWN = "entry-origin-unknown"


class Entry(NamedTuple):
    prompt_id: str
    turn_origin: str
    timestamp: datetime
    index: int


def _records(path: Path) -> list[tuple[int, dict]]:
    """(index, object) for every line that parses to a JSON object.

    A line that does not parse cannot be trusted to carry a promptId, so it is
    not an entry; a prompt whose entry is lost this way reports `entry-missing`.
    A missing file raises FileNotFoundError; other read errors propagate.
    """
    records: list[tuple[int, dict]] = []
    for index, raw in enumerate(path.read_bytes().splitlines()):
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if isinstance(obj, dict):
            records.append((index, obj))
    return records


def find_prompt_entry(path: str | None, pid: str) -> tuple[Entry | None, str | None]:
    """The prompt's entry, or (None, cause).

    The entry is non-None only when it is usable: found, `type: "user"`, a parsable
    timestamp and a string `turnOrigin` in KNOWN_ORIGINS. Causes: `entry-missing`
    (no path, no file, or no line with that promptId), `entry-malformed`,
    `entry-origin-unknown`.
    """
    if not path:
        return None, CAUSE_MISSING
    try:
        records = _records(Path(path))
    except FileNotFoundError:
        return None, CAUSE_MISSING
    for index, obj in records:
        if obj.get("promptId") != pid:
            continue
        # The first line with the id decides; later lines are never consulted.
        timestamp = presence_state.parse_timestamp(obj.get("timestamp"))
        origin = obj.get("turnOrigin")
        if obj.get("type") != "user" or timestamp is None or not isinstance(origin, str):
            return None, CAUSE_MALFORMED
        if origin not in KNOWN_ORIGINS:
            return None, CAUSE_ORIGIN_UNKNOWN
        return Entry(pid, origin, timestamp, index), None
    return None, CAUSE_MISSING


def newest_human_before(path: str, entry: Entry) -> Entry | None:
    """The last line before `entry.index` that is a human prompt entry, or None.

    Human = `type: "user"`, `turnOrigin` exactly "human", a parsable timestamp and
    no `isMeta: true`. No content test: only prompt entries carry `turnOrigin`.
    """
    for index, obj in reversed(_records(Path(path))):
        if index >= entry.index:
            continue
        if obj.get("type") != "user" or obj.get("turnOrigin") != HUMAN:
            continue
        if obj.get("isMeta") is True:
            continue
        timestamp = presence_state.parse_timestamp(obj.get("timestamp"))
        if timestamp is None:
            continue
        prompt_id: Any = obj.get("promptId")
        return Entry(prompt_id if isinstance(prompt_id, str) else "", HUMAN, timestamp, index)
    return None


# How Claude Code announces a background task to the session that started it, in
# the tool result (measured, Claude Code 2.1.x): a run_in_background Bash call, a
# foreground Bash call moved to the background after 180 s, and a Monitor.
_STARTED = re.compile(
    rb"(?:background with ID: |moved to the background \(ID: |Monitor started \(task )([A-Za-z0-9]+)"
)


def _announced(path: Path) -> set[str]:
    """The task ids the file at `path` announces as started. A missing file announces none."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return set()
    return {m.decode("ascii") for m in _STARTED.findall(data)}


def subagent_owned_ids(path: str | None, ids: set[str]) -> set[str]:
    """The ids in `ids` that a subagent started and the main session did not.

    The main transcript holds only the main session's own records; each subagent's
    records live in `<transcript stem>/subagents/agent-*.jsonl`. A task announced
    in a subagent file and nowhere in the main transcript is the subagent's. Any
    other id (announced by the main session, or by neither file) is not returned:
    when ownership cannot be shown, the caller counts the task.
    """
    if not path or not ids:
        return set()
    main = Path(path)
    folder = main.with_suffix("") / "subagents"
    if not folder.is_dir():
        return set()
    theirs: set[str] = set()
    for agent in sorted(folder.glob("agent-*.jsonl")):
        theirs |= _announced(agent) & ids
    if not theirs:
        return set()
    return theirs - _announced(main)


_MONITOR_STARTED = re.compile(r"Monitor started \(task ([A-Za-z0-9]+)")


def _result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            part["text"] for part in content if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return ""


def armed_monitors(path: str, ids: set[str]) -> dict[str, set[str]]:
    """Which Monitors the main transcript armed for each id in `ids`.

    A Monitor tool_use whose `command` names a task id as a whole token (its
    `tasks/<id>.output` path, or `watch.py --task <id>`) is joined by tool_use_id to
    the result "Monitor started (task <monitor id>, ...". Returns {task id: {monitor
    ids}}; an id nothing armed is absent. A missing file raises FileNotFoundError.
    """
    if not ids:
        return {}
    patterns = {i: re.compile(r"(?<![A-Za-z0-9])" + re.escape(i) + r"(?![A-Za-z0-9])") for i in ids}
    watching: dict[str, set[str]] = {}  # tool_use_id -> task ids its command names
    started: dict[str, str] = {}  # tool_use_id -> monitor id
    for _, obj in _records(Path(path)):
        message = obj.get("message")
        blocks = message.get("content") if isinstance(message, dict) else None
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") == "Monitor":
                command = (block.get("input") or {}).get("command")
                if isinstance(command, str) and isinstance(block.get("id"), str):
                    named = {i for i, rx in patterns.items() if rx.search(command)}
                    if named:
                        watching[block["id"]] = named
            elif block.get("type") == "tool_result" and isinstance(block.get("tool_use_id"), str):
                found = _MONITOR_STARTED.search(_result_text(block))
                if found:
                    started[block["tool_use_id"]] = found.group(1)
    armed: dict[str, set[str]] = {}
    for tool_use_id, named in watching.items():
        monitor = started.get(tool_use_id)
        if monitor is not None:
            for task_id in named:
                armed.setdefault(task_id, set()).add(monitor)
    return armed


# ------------------------------------------------------------ job age and coverage

# The waiter agent type, as the Agent tool names it (plugin-qualified) and bare.
WAITER_TYPES = ("presence-kit:waiter", "waiter")
# Where the main session learns a task id: the tool results above, and an Agent launch
# ("Async agent launched successfully ... agentId: <id>").
_ANNOUNCED_ID = re.compile(
    r"(?:background with ID: |moved to the background \(ID: |Monitor started \(task |agentId: )([A-Za-z0-9]+)"
)
_AGENT_LAUNCHED = re.compile(r"agentId: ([A-Za-z0-9]+)")
_AGENT_TOOLS = ("Agent", "Task")


def _blocks(obj: dict) -> list[dict]:
    message = obj.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _whole_token(task_id: str) -> re.Pattern:
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(task_id) + r"(?![A-Za-z0-9])")


def job_starts(path: str | None, ids: set[str]) -> dict[str, datetime | None]:
    """When the main session started (or last resumed) each task in `ids`.

    A task starts at the transcript record whose tool result announces it (a
    run_in_background Bash call, a call moved to the background, a Monitor, an Agent
    launch), and an agent restarts at the newest SendMessage tool call addressed to its
    id. The newest of those records wins. None means unknown: no record names the id,
    or no such record has a parsable timestamp, or the transcript cannot be read (a
    compacted transcript loses the announcement). The caller treats unknown as old.
    """
    known: dict[str, datetime | None] = {task_id: None for task_id in ids}
    if not ids or not path:
        return known
    try:
        records = _records(Path(path))
    except FileNotFoundError:
        return known
    for _, obj in records:
        stamp = presence_state.parse_timestamp(obj.get("timestamp"))
        if stamp is None:
            continue
        named: set[str] = set()
        for block in _blocks(obj):
            kind = block.get("type")
            if kind == "tool_result":
                named |= {m for m in _ANNOUNCED_ID.findall(_result_text(block)) if m in ids}
            elif kind == "tool_use" and block.get("name") == "SendMessage":
                target = (block.get("input") or {}).get("to")
                if isinstance(target, str) and target in ids:
                    named.add(target)
        for task_id in named:
            current = known[task_id]
            if current is None or stamp > current:
                known[task_id] = stamp
    return known


def waiter_agents(path: str | None, ids: set[str]) -> dict[str, set[str]]:
    """Which waiter agents the main transcript launched for each job id in `ids`.

    An Agent tool call whose `subagent_type` is a waiter type and whose prompt names a
    job id as a whole token (its `tasks/<id>.output` path, or the bare id) is joined by
    tool_use_id to the launch result's `agentId`. Returns {job id: {agent ids}}; a job
    no waiter was launched for is absent. A missing file raises FileNotFoundError.
    """
    if not ids or not path:
        return {}
    patterns = {i: _whole_token(i) for i in ids}
    naming: dict[str, set[str]] = {}  # tool_use_id -> job ids its prompt names
    launched: dict[str, str] = {}  # tool_use_id -> agent id
    for _, obj in _records(Path(path)):
        for block in _blocks(obj):
            if block.get("type") == "tool_use" and block.get("name") in _AGENT_TOOLS:
                spec = block.get("input") or {}
                prompt = spec.get("prompt")
                if spec.get("subagent_type") in WAITER_TYPES and isinstance(prompt, str) \
                        and isinstance(block.get("id"), str):
                    named = {i for i, rx in patterns.items() if rx.search(prompt)}
                    if named:
                        naming[block["id"]] = named
            elif block.get("type") == "tool_result" and isinstance(block.get("tool_use_id"), str):
                found = _AGENT_LAUNCHED.search(_result_text(block))
                if found:
                    launched[block["tool_use_id"]] = found.group(1)
    covering: dict[str, set[str]] = {}
    for tool_use_id, named in naming.items():
        agent = launched.get(tool_use_id)
        if agent is not None:
            for job_id in named:
                covering.setdefault(job_id, set()).add(agent)
    return covering


def compacted_since(path: str | None, since: datetime) -> bool:
    """True when the transcript holds a context-compaction boundary record
    (`type: "system"`, `subtype: "compact_boundary"`) stamped after `since`. A missing
    or unreadable transcript says nothing, so it is False."""
    if not path:
        return False
    try:
        records = _records(Path(path))
    except FileNotFoundError:
        return False
    for _, obj in records:
        if obj.get("type") == "system" and obj.get("subtype") == "compact_boundary":
            stamp = presence_state.parse_timestamp(obj.get("timestamp"))
            if stamp is not None and stamp > since:
                return True
    return False


def work_since(path: str | None, since: datetime) -> bool:
    """True when an assistant tool call is stamped after `since`, not counting a
    `recap-done` call (the recap's own bookkeeping). A missing transcript is False."""
    if not path:
        return False
    try:
        records = _records(Path(path))
    except FileNotFoundError:
        return False
    for _, obj in records:
        stamp = presence_state.parse_timestamp(obj.get("timestamp"))
        if stamp is None or stamp <= since:
            continue
        for block in _blocks(obj):
            if block.get("type") != "tool_use":
                continue
            command = (block.get("input") or {}).get("command")
            if isinstance(command, str) and "recap-done" in command:
                continue
            return True
    return False
