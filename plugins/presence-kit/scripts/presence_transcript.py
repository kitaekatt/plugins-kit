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
