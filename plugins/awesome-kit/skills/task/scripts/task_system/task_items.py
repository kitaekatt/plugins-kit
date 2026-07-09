"""task_items -- the plan.md-embedded item enumeration (the ``items`` verb
substrate; design/task-items-design.md).

An **item** (long form "task item"; accepted synonym "work item") is the
enumerable unit of next work WITHIN a task: the ``task_items:`` typed unit
embedded as a fenced YAML block in the task folder's plan.md is the single
home for a task's open work. Item state lives there and only there --
CLAUDE.md's Immediate Priorities references items by id and never restates
state. Completion is REMOVAL from the block (plan.md's Accomplished line +
log.md keep the record), so the block enumerates open work only.

Contract implemented here (design sections 4-5, 9):

- **One block per task, in plan.md.** All ``*.md`` under the folder are
  scanned (rglob, same fenced-block extraction as discovery.py); more than
  one ``task_items`` block, or a single block outside plan.md, is an error.
- **Schema** (schemas.TASK_ITEMS_SCHEMA): ``{ items: list[item] }``; each
  item ``id``/``title``/``state`` required, ``priority``/``note`` optional.
  ``items`` may be empty (fresh task).
- **Post-walker checks** (same layering as task.yaml's, see schemas.py):
  ``state`` within the type's ``item_state_vocabulary``; ``priority`` against
  the type's priority pattern (one priority vocabulary system-wide); ``id``
  kebab-case (``^[a-z0-9][a-z0-9-]*$``) and unique within the block.
- **Absence is not an error.** ``block_found`` is False; validate.py turns
  that into the pre-contract warning (gates ``work``, prompting the one-time
  forward conversion of a pre-contract folder).
- **Lenient projection.** Items are returned whenever the block's structural
  walk passes, even alongside vocabulary/id findings -- the verbs render what
  exists; validate gates. A structurally failing block yields no items.

The stale-reference check (design section 9): CLAUDE.md's Immediate
Priorities section references items by backticked id; a backticked
kebab-with-hyphen token there that matches no item id is a warning. The
one-hyphen-minimum requirement keeps ordinary backticked words (``active``,
``none``) and paths/commands (dots, slashes, spaces fail the pattern) out of
the check.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from skills_kit_lib import schema_engine
from skills_kit_lib.document_walker import iter_yaml_blocks

from .schemas import TASK_ITEMS_SCHEMA
from .types import TaskType

ITEM_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
PLAN_DOC_NAME = "plan.md"

_TASK_ITEMS_KEY_RE = re.compile(r"^task_items\s*:", re.MULTILINE)
# Backticked kebab token WITH at least one hyphen (module docstring).
_PRIORITY_REF_RE = re.compile(r"`([a-z0-9][a-z0-9-]*-[a-z0-9-]*[a-z0-9])`")
_PRIORITIES_HEADING = "## Immediate Priorities"


@dataclass(frozen=True)
class ItemRecord:
    """One parsed item, projected for the ``items`` verb and the ``status``
    substrate."""

    id: str
    title: str
    state: str
    priority: str | None = None
    note: str | None = None


@dataclass
class ItemsResult:
    """Outcome of reading a folder's ``task_items`` unit."""

    items: list[ItemRecord] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    block_found: bool = False


def _blocks_in_doc(doc: Path) -> list[dict]:
    """Parsed ``task_items``-rooted YAML blocks in one document. An
    unparseable fenced block that names the root key is surfaced as a
    sentinel ``None`` entry (the caller reports it against the doc)."""
    try:
        text = doc.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    found: list[dict] = []
    for block_text in iter_yaml_blocks(text):
        try:
            data = yaml.safe_load(block_text)
        except yaml.YAMLError:
            if _TASK_ITEMS_KEY_RE.search(block_text):
                found.append(None)  # type: ignore[arg-type]
            continue
        if isinstance(data, dict) and "task_items" in data:
            found.append(data)
    return found


def read_task_items(folder: Path, ttype: TaskType) -> ItemsResult:
    """Read + check the folder's ``task_items`` unit per the module-docstring
    contract. Never raises for bad content; findings land in ``errors``."""
    result = ItemsResult()
    hits: list[tuple[Path, dict | None]] = []
    for doc in sorted(folder.rglob("*.md")):
        for data in _blocks_in_doc(doc):
            hits.append((doc, data))

    if not hits:
        return result
    result.block_found = True

    rel = lambda d: d.relative_to(folder).as_posix()  # noqa: E731
    if len(hits) > 1:
        docs = ", ".join(sorted({rel(doc) for doc, _ in hits}))
        result.errors.append(
            f"multiple task_items blocks ({len(hits)}) in: {docs} -- the "
            "task_items unit lives in plan.md, exactly once"
        )
        return result

    doc, data = hits[0]
    if data is None:
        result.errors.append(
            f"unparseable YAML block containing task_items in {rel(doc)}"
        )
        return result
    if rel(doc) != PLAN_DOC_NAME:
        result.errors.append(
            f"task_items block found in {rel(doc)} -- the task_items unit "
            f"lives in {PLAN_DOC_NAME}"
        )
        return result

    fails, _ = schema_engine.validate(data, TASK_ITEMS_SCHEMA)
    if fails:
        for path, msg in fails:
            result.errors.append(f"task_items schema violation: {path}: {msg}")
        return result

    seen: set[str] = set()
    for entry in data["task_items"]["items"]:
        item_id = entry["id"]  # schema-guaranteed present strings
        state = entry["state"]
        priority = entry.get("priority")
        note = entry.get("note")
        if not ITEM_ID_RE.match(item_id):
            result.errors.append(
                f"task_items: id {item_id!r} is not kebab-case "
                f"({ITEM_ID_RE.pattern})"
            )
        if item_id in seen:
            result.errors.append(f"task_items: duplicate id {item_id!r}")
        seen.add(item_id)
        if state not in ttype.item_state_vocabulary:
            result.errors.append(
                f"task_items: {item_id}: state {state!r} not in "
                f"{list(ttype.item_state_vocabulary)}"
            )
        if isinstance(priority, str) and not re.match(
            ttype.priority_pattern, priority
        ):
            result.errors.append(
                f"task_items: {item_id}: priority {priority!r} does not "
                f"match {ttype.priority_pattern!r}"
            )
        result.items.append(
            ItemRecord(
                id=item_id,
                title=entry["title"],
                state=state,
                priority=priority if isinstance(priority, str) else None,
                note=note if isinstance(note, str) else None,
            )
        )
    return result


def sort_items(items: list[ItemRecord]) -> list[ItemRecord]:
    """The ``items`` verb ordering (design section 8): priority first
    (P1 < P2 < ...; no priority sorts last), block order as the tiebreak
    (sorted() is stable)."""
    return sorted(
        items, key=lambda it: (it.priority is None, it.priority or "")
    )


def stale_priority_refs(claude_md_text: str, item_ids: set[str]) -> list[str]:
    """Backticked kebab-with-hyphen tokens in CLAUDE.md's Immediate
    Priorities section that match no item id (design section 9's
    stale-reference check). Empty when the section is absent."""
    lines = claude_md_text.splitlines()
    section: list[str] = []
    in_section = False
    for line in lines:
        if line.strip() == _PRIORITIES_HEADING:
            in_section = True
            continue
        if in_section and line.startswith("## "):
            break
        if in_section:
            section.append(line)
    tokens = _PRIORITY_REF_RE.findall("\n".join(section))
    stale = [t for t in dict.fromkeys(tokens) if t not in item_ids]
    return stale
