"""Input readers of the task questions (summary-on-commit-plan.md, D1) and the inputs fingerprint.

A reader returns one task file's material as canonical UTF-8 text: LF newlines, trailing whitespace stripped
per line, no trailing blank lines, and cut at the reader's fixed limit (the cut text ends with a final
``[truncated]`` line and still fits the limit). An answer is current when the fingerprint of its question
definition plus these texts equals the one it stored, so every byte a reader emits is part of the contract.

Readers are code. ``task_commits`` is a script-only reader: it names git material that only a script
answerer (``answer_scripts``) can read, so a question that lists it must have no prompt and
``read_inputs`` refuses to read it into a bundle. No reader reads ``answers/``.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

import yaml

from .answers_types import InputBundle, Question
from .discovery import TaskUpdate, read_task_updates
from .task_items import read_task_items
from .types import HAND_OFF

TRUNCATION_MARKER = "[truncated]"
TASK_EXCLUDED_KEYS = frozenset(("_schema_version", "summary", "summary_fingerprint", "summary_updated"))
# The bookkeeping line `state_ops.update` writes when nothing was edited. It records no activity.
_BOOKKEEPING_RE = re.compile(r"^(?:update:\s*)?refresh \(no field edits\)$")


class QuestionInputError(ValueError):
    """A question names a reader that cannot supply its input, or a file cannot be read."""


def canonical_json(value: Any) -> str:
    """The canonical JSON every digest and fingerprint hashes."""
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def canonical_text(raw: str) -> str:
    """LF newlines, trailing whitespace stripped per line, trailing blank lines dropped."""
    lines = [line.rstrip() for line in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def cut_to_limit(text: str, limit: int) -> tuple[str, bool]:
    """``text`` unchanged when it fits ``limit`` characters, else its head plus a final marker line, in all
    at most ``limit`` characters."""
    if len(text) <= limit:
        return text, False
    room = limit - len(TRUNCATION_MARKER) - 1
    if room < 0:
        raise ValueError(f"limit {limit} cannot hold the {TRUNCATION_MARKER} line")
    return canonical_text(text[:room]) + "\n" + TRUNCATION_MARKER, True


class _LiteralDumper(yaml.SafeDumper):
    """Multi-line strings as literal blocks, so the text a model reads is the text in the file."""


def _represent_str(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_LiteralDumper.add_representer(str, _represent_str)


def _dump_yaml(data: Any) -> str:
    return yaml.dump(
        data, Dumper=_LiteralDumper, sort_keys=True, default_flow_style=False,
        allow_unicode=True, width=1_000_000,
    )


def _read_file(path: Path) -> str:
    """The file's text; an absent file is empty material (a scaffold has none yet), any other failure raises."""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeDecodeError) as exc:
        raise QuestionInputError(f"{path}: cannot read: {exc}") from exc


def activity_entries(folder: Path) -> tuple[TaskUpdate, ...]:
    """The task's dated log entries, newest first, without bookkeeping.

    ``read_task_updates`` already leaves out summary-maintenance entries; this also leaves out
    ``refresh (no field edits)`` lines, which record no activity but would change every fingerprint that
    reads the log.
    """
    return tuple(entry for entry in read_task_updates(folder) if not _BOOKKEEPING_RE.match(entry.detail))


def activity_line(entry: TaskUpdate) -> str:
    stamp = f"{entry.date} {entry.time}" if entry.time else entry.date
    return f"{stamp}: {entry.detail}" if entry.detail else f"{stamp}:"


def _read_task(folder: Path, block: Mapping[str, Any] | None) -> str:
    material = {key: value for key, value in (block or {}).items() if key not in TASK_EXCLUDED_KEYS}
    return _dump_yaml(material)


def _read_claude_md(folder: Path, block: Mapping[str, Any] | None) -> str:
    return _read_file(folder / "CLAUDE.md")


def _read_plan_md(folder: Path, block: Mapping[str, Any] | None) -> str:
    return _read_file(folder / "plan.md")


def _read_items(folder: Path, block: Mapping[str, Any] | None) -> str:
    items = []
    for item in read_task_items(folder, HAND_OFF).items:
        entry = {"id": item.id, "title": item.title, "state": item.state}
        if item.priority is not None:
            entry["priority"] = item.priority
        if item.note is not None:
            entry["note"] = item.note
        items.append(entry)
    return _dump_yaml(items)


def _read_activity(folder: Path, block: Mapping[str, Any] | None) -> str:
    return "\n".join(activity_line(entry) for entry in activity_entries(folder))


@dataclass(frozen=True)
class Reader:
    name: str
    limit: int | None  # characters; None for a script-only reader
    read: Callable[[Path, Mapping[str, Any] | None], str] | None


READERS: Mapping[str, Reader] = {
    reader.name: reader
    for reader in (
        Reader("task", 4000, _read_task),
        Reader("claude_md", 12000, _read_claude_md),
        Reader("plan_md", 12000, _read_plan_md),
        Reader("items", 6000, _read_items),
        Reader("activity", 6000, _read_activity),
        Reader("task_commits", None, None),
    )
}


def read_inputs(
    folder: Path, block: Mapping[str, Any] | None, question: Question, *, task_id: str | None = None
) -> InputBundle:
    """Read the question's declared inputs from a task folder.

    ``block`` is the parsed task.yaml ``task:`` mapping (None when it is unreadable). ``task_id`` names the
    task in the bundle; it defaults to the folder name and is not part of any fingerprint.
    """
    texts: list[tuple[str, str]] = []
    truncated: list[str] = []
    for name in question.inputs:
        reader = READERS.get(name)
        if reader is None:
            raise QuestionInputError(f"question {question.id}: unknown reader {name!r}")
        if reader.read is None or reader.limit is None:
            raise QuestionInputError(
                f"question {question.id}: reader {name!r} is script-only and cannot enter an input bundle"
            )
        text, was_cut = cut_to_limit(canonical_text(reader.read(Path(folder), block)), reader.limit)
        texts.append((name, text))
        if was_cut:
            truncated.append(name)
    return InputBundle(
        task_id=task_id if task_id is not None else Path(folder).name,
        texts=tuple(texts),
        truncated=tuple(truncated),
    )


def inputs_fingerprint(question: Question, bundle: InputBundle) -> str:
    """sha256 hex of the question digest plus the bundle's texts, in declared order."""
    names = tuple(name for name, _ in bundle.texts)
    if names != question.inputs:
        raise QuestionInputError(
            f"question {question.id}: the bundle holds {list(names)}, the question declares {list(question.inputs)}"
        )
    material = {"question": question.digest, "inputs": [[name, text] for name, text in bundle.texts]}
    return hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()
