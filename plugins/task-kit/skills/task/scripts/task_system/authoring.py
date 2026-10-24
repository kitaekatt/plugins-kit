"""Pure task authoring: input rules and plan.md text edits.

Nothing here touches the disk or a lock. The machine API owns the
serialization and calls these functions from inside a :class:`~.mutation.TaskMutator`
edit, so every refusal below is raised BEFORE a byte is written.

An item is added in two places that must agree: an entry in plan.md's
``task_items`` block and, when details are given, a ``### <id> -- <title>`` section
in the ``##`` region that holds the block (the section is what a run is given as its
instructions). :func:`add_item_to_plan` makes both edits and then proves the result
in memory -- one block, schema-valid, exactly the old items plus the new one, the new
section found, every other item's section unchanged -- or refuses with nothing written.
"""

from __future__ import annotations

import re

import yaml
from skills_kit_lib import schema_engine
from skills_kit_lib.document_walker import YAML_BLOCK_RE, iter_yaml_blocks

from . import schemas
from .task_items import ITEM_ID_RE
from .types import HAND_OFF
from . import mutation, plan_sections

ITEM_ID_MAX = 48
PRIORITIES = ("P1", "P2", "P3")


class AuthoringRefused(Exception):
    """An input rule refused the request; ``code`` is stable (the API answers 422)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def check_priority(priority: object) -> str | None:
    if priority is None:
        return None
    if not isinstance(priority, str) or not re.match(HAND_OFF.priority_pattern, priority):
        raise AuthoringRefused("bad-priority", f"priority must be one of {', '.join(PRIORITIES)} (or empty)")
    return priority


def check_title(title: object) -> str:
    if not isinstance(title, str):
        raise AuthoringRefused("bad-title", "title must be text")
    flat = title.strip()
    if not flat:
        raise AuthoringRefused("bad-title", "title must not be empty")
    if "\n" in flat or "\r" in flat:
        raise AuthoringRefused("bad-title", "title must be one line")
    return flat


def check_details(details: object) -> str:
    """Details are plan.md body text: no heading that would end the item's section, no open code fence."""
    if details is None:
        return ""
    if not isinstance(details, str):
        raise AuthoringRefused("bad-details", "details must be text")
    text = details.replace("\r\n", "\n").strip()
    if not text:
        return ""
    shallow = plan_sections.headings_outside_fences(text, 3)
    if shallow:
        raise AuthoringRefused(
            "bad-details",
            f"details may not contain a heading of level 1 to 3 ({shallow[0][1]!r}): it would end this item's section; "
            "use #### or deeper",
        )
    if plan_sections.has_unclosed_fence(text):
        raise AuthoringRefused("bad-details", "details end inside an unclosed code fence")
    return text


def derive_item_id(title: str, taken: set[str]) -> str:
    """A kebab-case id from the title, unique among ``taken`` (item ids and section-heading first tokens)."""
    base = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:ITEM_ID_MAX].strip("-")
    if not base or not ITEM_ID_RE.fullmatch(base):
        raise AuthoringRefused("bad-title", "the title derives no item id (it needs letters or digits)")
    candidate, n = base, 1
    while candidate in taken:
        n += 1
        suffix = f"-{n}"
        candidate = base[: ITEM_ID_MAX - len(suffix)].rstrip("-") + suffix
    return candidate


def _taken_ids(plan: str) -> set[str]:
    taken = {str(i.get("id")) for i in (mutation.raw_items(plan) or [])}
    lines = plan.splitlines()
    taken.update(token for _i, level, token, _t in plan_sections._headings(lines) if 2 <= level <= 4)
    return taken


def _task_items_blocks(plan: str) -> list[dict]:
    found = []
    for text in iter_yaml_blocks(plan):
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError:
            continue
        if isinstance(data, dict) and "task_items" in data:
            found.append(data)
    return found


def _section_statuses(plan: str, ids: list[str]) -> dict[str, str]:
    return {i: plan_sections.find_item_section(plan, i).status for i in ids}


def add_item_to_plan(plan: str, *, title: str, priority: str | None, details: str) -> tuple[str, dict]:
    """plan.md text with a new available item (and its section), plus the item mapping.

    Raises :class:`AuthoringRefused` for an input rule and :class:`~.mutation.MutationError`
    (``invalid-source``) when plan.md cannot take the edit or the edited text does not verify.
    """
    found = mutation._items_block(plan)
    if found is None:
        raise AuthoringRefused("no-items-block", "plan.md has no task_items block to add an item to")
    match, _data = found
    old_items = mutation.raw_items(plan) or []
    item_id = derive_item_id(title, _taken_ids(plan))
    item: dict = {"id": item_id, "title": title, "state": "available"}
    if priority:
        item["priority"] = priority
    new_plan = mutation.append_item(plan, item)
    if details:
        # The block was re-dumped in place; its fence did not move, so the insert point is read off the NEW text.
        new_match = mutation._items_block(new_plan)[0]  # type: ignore[index]
        offset = plan_sections.section_insert_offset(new_plan, new_match.end())
        section = f"### {item_id} -- {title}\n\n{details}\n"
        head, tail = new_plan[:offset], new_plan[offset:]
        if head and not head.endswith("\n"):
            head += "\n"
        if head and not head.endswith("\n\n"):
            head += "\n"
        new_plan = head + section + ("\n" + tail if tail else "")
    _verify(plan, new_plan, old_items, item, with_section=bool(details))
    return new_plan, item


def _verify(old_plan: str, new_plan: str, old_items: list[dict], item: dict, *, with_section: bool) -> None:
    def refuse(why: str) -> None:
        raise mutation.MutationError("invalid-source", f"the edited plan.md would not verify ({why}); nothing was written")

    blocks = _task_items_blocks(new_plan)
    if len(blocks) != 1:
        refuse(f"it has {len(blocks)} task_items blocks")
    fails, _ = schema_engine.validate(blocks[0], schemas.TASK_ITEMS_SCHEMA)
    if fails:
        refuse("; ".join(f"{path}: {msg}" for path, msg in fails[:3]))
    if mutation.raw_items(new_plan) != [*old_items, item]:
        refuse("the item list is not the old list plus the new item")
    ids = [str(i.get("id")) for i in old_items]
    if _section_statuses(old_plan, ids) != _section_statuses(new_plan, ids):
        refuse("an existing item's section changed")
    if with_section and plan_sections.find_item_section(new_plan, item["id"]).status != plan_sections.FOUND:
        refuse("the new item's section is not found")
    if not with_section and plan_sections.find_item_section(new_plan, item["id"]).status != plan_sections.MISSING:
        refuse("a section for the new item appeared that was not written")


__all__ = [
    "AuthoringRefused", "add_item_to_plan", "check_details", "check_priority", "check_title", "derive_item_id",
    "YAML_BLOCK_RE",
]
