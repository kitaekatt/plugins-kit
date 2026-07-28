"""Formatting helpers for user-facing bootstrap message text.

This module is the PRESENTATION half of the split described in
``records.py``. It shortens, numbers, and truncates freely, because the
complete text of everything it renders is retained independently in
``bootstrap_events.jsonl``. Every rule here is therefore a readability
judgement -- none of them is protecting information from being lost.

The rules in full, with rationale:
``skills/bootstrap/references/engine-internals.md`` ("Collated message text").
"""

from typing import Iterable, List, Optional

#: Longest an item may be when it is going to be collated onto one line with
#: other items. Deliberately short: a collated line carries N of these plus a
#: header, and past roughly this width the line stops being scannable and
#: becomes a paragraph the user skips.
ITEM_MAX = 40


#: Separators an entry uses between its subject and its explanation, most
#: specific first. "<tool>: FAILED - install attempted but not found in PATH"
#: shortens to "<tool>: FAILED" by cutting at the SEPARATOR, which is a
#: different act from cutting at a character count: the result is a complete
#: clause that still names the thing.
_HEAD_SEPARATORS = (" - ", " (", " -- ")


def derive_short(text: str, limit: int = ITEM_MAX):
    """A shorter WHOLE form of ``text`` within ``limit``, or None.

    Never truncates. It only drops a trailing explanation at a separator the
    entry already contains, so whatever comes back is a phrase the author
    wrote. When no such phrase fits, this returns None rather than inventing
    one -- the caller then has an over-long item, which is a signal to author a
    ``display=`` label at the source, not something to paper over here.
    """
    text = str(text).strip()
    if len(text) <= limit:
        return text
    for sep in _HEAD_SEPARATORS:
        head = text.split(sep, 1)[0].strip()
        if head and len(head) <= limit:
            return head
    return None


def numbered(items: Iterable[str], sep: str = "; ",
             limit: int = ITEM_MAX) -> str:
    """Collate items into one line as ``(1) x; (2) y``, each within ``limit``.

    Bootstrap routinely flattens several unrelated issues into a single line
    (a display section's actions, the elevation queue's task labels, the ASK
    item list). Joined by a bare separator those read as one run-on sentence,
    and items whose own text contains commas or semicolons -- most of them --
    make the boundaries genuinely unrecoverable. Numbering restores them:

        (1) install uv; (2) create the p4-kit venv

    A single item is returned unnumbered: "(1) x" on its own adds ceremony
    without disambiguating anything -- but it is still width-limited, since a
    lone 400-character item is exactly as unreadable as five.

    Each item is rendered as: its AUTHORED short label if it has one, else a
    whole shorter form derived at a separator, else the full text. Nothing is
    ever cut mid-word -- an item that still exceeds ``limit`` means no short
    label was authored for it, and the fix belongs at the call site.

    Pass ``limit=None`` for the rare surface that is genuinely unconstrained.
    """
    from .records import short_form

    listed: List[str] = []
    for item in items:
        if item is None:
            continue
        text = str(item).strip()
        if not text:
            continue
        if limit is not None and len(text) > limit:
            text = (short_form(item) or derive_short(text, limit) or text).strip()
        listed.append(text)
    if len(listed) <= 1:
        return listed[0] if listed else ""
    return sep.join(f"({n}) {item}" for n, item in enumerate(listed, 1))


def item_label(*candidates: Optional[str], limit: int = ITEM_MAX) -> str:
    """First candidate that is non-empty AND fits ``limit``; else the last, cut.

    Callers pass candidates longest-and-friendliest first and
    terse-and-guaranteed-short last (typically an author-declared ``label``,
    then a ``description`` that MAY be prose, then the entry's ``name`` slug).
    Preferring a whole shorter candidate over a trimmed longer one is a
    READABILITY choice, not a safety one: "parsec-host" tells the reader what
    the item is, where "Parsec headless host: PER-COMPUTER install (ser..."
    spends its budget on the least distinguishing part of the sentence.

    When no candidate fits, a whole shorter form is derived at a separator; if
    even that fails the last candidate is returned intact and over-length. It is
    never cut mid-word: a label that stops partway through an identifier is
    unrecognisable, and an over-long item is a missing ``label`` at the source
    -- fix it there.
    """
    usable = [str(c).strip() for c in candidates if c and str(c).strip()]
    if not usable:
        return ""
    for candidate in usable:
        if len(candidate) <= limit:
            return candidate
    return derive_short(usable[-1], limit) or usable[-1]
