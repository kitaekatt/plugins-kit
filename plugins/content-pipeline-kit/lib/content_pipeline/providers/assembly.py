"""Single-owner prompt-block assembly, slot syntax, and label indirection.

The one place that composes registered provider outputs plus a template into an
assembled prompt block. Being the single owner structurally prevents drift
between build sites -- two call sites that both need "the glossary block" get
it from the same path rather than each formatting it slightly differently.

Three responsibilities, each generalized from a source technique and kept
domain-free:

- **Ordered named blocks with conditional inclusion** (:class:`Block`,
  :func:`assemble_blocks`) -- generalizes loc's ``prompt_assembly``, where a
  conditional override-schema block is spliced in only when relevant. A block
  is emitted only when it is included AND its body is non-blank, so an absent
  block leaves no stray whitespace.
- **Configurable slot-syntax tokenizer** (:class:`SlotSyntax`) -- the single
  owner of a ``${name}`` template dialect, generalized from loc's
  ``slot_syntax`` with the delimiters made configurable. All slot knowledge
  (the compiled regex, construction, parsing, rendering) lives here so a
  delimiter change is one place.
- **Label indirection** (:func:`assign_labels`, :func:`invert_labels`,
  :func:`relabel`) -- shows an agent opaque ``item_N`` labels instead of real
  keys. Opaque labels defeat an LLM's tendency to collapse sibling items that
  share a visible
  key-suffix pattern; the mapping round-trips the agent's label-keyed response
  back to real keys. A GENERIC technique, so it lives here, not in a caller.

Stdlib-only (``re``); imports nothing from the rest of ``content_pipeline``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Mapping, Sequence

# ---------------------------------------------------------------------------
# Ordered named blocks
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Block:
    """One named prompt block.

    - ``name`` -- a label for the block (diagnostics / ordering; not emitted).
    - ``body`` -- the block text.
    - ``include`` -- when False the block is skipped entirely (a conditional
      block whose precondition did not hold).
    """

    name: str
    body: str
    include: bool = True


def assemble_blocks(blocks: Sequence[Block], *, separator: str = "\n\n") -> str:
    """Join ``blocks`` in order, dropping excluded or blank-bodied ones.

    A block contributes only when ``include`` is True and ``body.strip()`` is
    non-empty, so a template that conditionally omits sections produces clean
    output with no doubled separators. Order is the caller's order (this is the
    single owner of block composition, so the order lives at one site).
    """
    parts: List[str] = [b.body for b in blocks if b.include and b.body.strip()]
    return separator.join(parts)


# ---------------------------------------------------------------------------
# Slot syntax
# ---------------------------------------------------------------------------

DEFAULT_SLOT_OPEN = "${"
DEFAULT_SLOT_CLOSE = "}"


class SlotSyntax:
    """The single owner of a ``${name}`` slot dialect (configurable delimiters).

    Slots are ``<open><name><close>`` where ``name`` is any run of characters
    that are not part of a delimiter. The default ``${ ... }`` is YAML-safe at
    every position (a leading ``[`` would force quoting) and matches Python's
    ``string.Template`` convention. A caller wanting a different dialect
    constructs ``SlotSyntax("<<", ">>")`` -- the compiled regex and every
    operation derive from the delimiters passed here, so nothing else hardcodes
    them.
    """

    def __init__(
        self,
        open_delim: str = DEFAULT_SLOT_OPEN,
        close_delim: str = DEFAULT_SLOT_CLOSE,
    ) -> None:
        if not open_delim or not close_delim:
            raise ValueError("slot delimiters must be non-empty")
        self.open_delim = open_delim
        self.close_delim = close_delim
        # Inner match excludes the delimiter characters so adjacent slots and
        # multi-word names both parse. Group 1 captures the slot name.
        forbidden = re.escape(open_delim + close_delim)
        self._pattern = re.compile(
            re.escape(open_delim) + r"([^" + forbidden + r"]+)" + re.escape(close_delim)
        )

    def slot(self, name: str) -> str:
        """Return the slot literal for ``name`` (``slot("glossary")`` -> ``${glossary}``)."""
        return f"{self.open_delim}{name}{self.close_delim}"

    def parse_slots(self, text: str) -> tuple:
        """Return the slot names referenced in ``text``, left-to-right.

        Duplicates are preserved (``${x} ${x}`` yields both).
        """
        return tuple(m.group(1) for m in self._pattern.finditer(text))

    def has_any_slot(self, text: str) -> bool:
        """Return True iff ``text`` contains at least one slot."""
        return self._pattern.search(text) is not None

    def render(self, template: str, lookup: Callable[[str], str]) -> str:
        """Substitute every slot in ``template`` via ``lookup(name) -> str``.

        The lookup owns the missing-name policy (raise, or return a default);
        this method just performs the substitution.
        """
        return self._pattern.sub(lambda m: lookup(m.group(1)), template)

    def render_map(self, template: str, values: Mapping[str, str], *, strict: bool = True) -> str:
        """Substitute slots from a ``{name: value}`` mapping.

        With ``strict`` (default) an unknown slot raises :class:`KeyError`;
        otherwise it is left untouched in the output.
        """

        def _lookup(name: str) -> str:
            if name in values:
                return values[name]
            if strict:
                raise KeyError(f"no value for slot {name!r}")
            return self.slot(name)

        return self.render(template, _lookup)


# ---------------------------------------------------------------------------
# Label indirection
# ---------------------------------------------------------------------------


def assign_labels(
    keys: Iterable,
    *,
    prefix: str = "item_",
    start: int = 1,
) -> Dict:
    """Assign opaque sequential labels to ``keys`` in iteration order.

    Returns ``{key: label}`` where labels are ``prefix + N`` (``item_1``,
    ``item_2``, ...). Showing an agent these opaque labels instead of the real
    keys defeats its tendency to collapse sibling items that share a visible
    key-suffix pattern; the returned map round-trips the agent's response back
    to real keys via :func:`relabel`. Iteration order is preserved, so the
    caller controls label assignment by ordering ``keys``.
    """
    return {key: f"{prefix}{i}" for i, key in enumerate(keys, start=start)}


def invert_labels(label_by_key: Mapping) -> Dict:
    """Invert a ``{key: label}`` map to ``{label: key}``.

    Raises :class:`ValueError` on a duplicate label (labels must be a bijection
    for the response round-trip to be unambiguous).
    """
    out: Dict = {}
    for key, label in label_by_key.items():
        if label in out:
            raise ValueError(f"duplicate label {label!r} in label map")
        out[label] = key
    return out


def relabel(
    response_by_label: Mapping,
    label_by_key: Mapping,
    *,
    strict: bool = True,
) -> Dict:
    """Translate a label-keyed response back to real keys.

    ``response_by_label`` is what the agent returned (keyed by opaque label);
    the result is keyed by the real keys from ``label_by_key``. With ``strict``
    (default) a response label absent from the map raises :class:`KeyError`
    (the agent invented a label); otherwise the unknown entry is dropped.
    """
    key_by_label = invert_labels(label_by_key)
    out: Dict = {}
    for label, value in response_by_label.items():
        if label in key_by_label:
            out[key_by_label[label]] = value
        elif strict:
            raise KeyError(f"response label {label!r} has no assigned key")
    return out


__all__ = [
    "Block",
    "assemble_blocks",
    "DEFAULT_SLOT_OPEN",
    "DEFAULT_SLOT_CLOSE",
    "SlotSyntax",
    "assign_labels",
    "invert_labels",
    "relabel",
]
