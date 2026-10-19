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
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

import presence_state

KNOWN_ORIGINS = ("human", "task_notification", "peer", "sdk")
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
