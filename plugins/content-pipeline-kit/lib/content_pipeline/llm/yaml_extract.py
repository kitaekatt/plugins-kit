"""Robust YAML extraction from LLM responses.

Generalizes loc's ``yaml_extract`` to a domain-free parser: an LLM asked for
YAML wraps it in a fenced block, prepends prose, or decorates the language tag
inconsistently, and this module recovers the payload tolerantly. One stage,
no multi-strategy JSON ladder -- YAML is whitespace-tolerant and sidesteps the
brace-balancing failures a JSON extractor works around.

Strategy (in :func:`extract_yaml`):

1. Strip a leading/trailing fence. A ```` ```yaml ```` / ```` ```yml ```` fence
   is accepted with any trailing tokens on the language line (``yaml linenos``,
   ``yaml title=x``); a bare ```` ``` ```` fence (no language tag) is accepted
   too.
2. ``yaml.safe_load`` the fence-stripped candidate (or the whole text when no
   fence matched).
3. On a parse failure, if an ``anchor`` substring (e.g. a known top-level key
   like ``"items:"``) is supplied and present, retry from the first occurrence
   of that anchor -- this rescues a response with preamble prose ahead of bare
   YAML. Both attempts failing raises :class:`YamlExtractionError` (iron
   contract: no silent acceptance).

:func:`extract_mapping` layers a shape check on top: the result must be a
mapping, optionally carrying ``required_keys``. This module is stdlib +
``pyyaml`` only and imports nothing from the rest of ``content_pipeline``.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Optional

import yaml

# ```yaml / ```yml with any trailing tokens on the language line.
_FENCE_LANG_RE = re.compile(r"```(?:ya?ml)[^\n]*\n(.+?)```", re.DOTALL | re.IGNORECASE)
# A bare ``` fence with no language tag.
_FENCE_BARE_RE = re.compile(r"```\s*\n(.+?)```", re.DOTALL)


class YamlExtractionError(Exception):
    """Raised when a response cannot be parsed into YAML.

    Carries the offending ``raw`` text (truncated in the message) and a
    machine-readable ``reason`` so a caller can surface a diagnostic without
    re-parsing the prose.
    """

    def __init__(self, raw: str, reason: str) -> None:
        self.raw = raw
        self.reason = reason
        super().__init__(f"{reason}: {raw[:200]}")


def strip_fence(text: str) -> str:
    """Return the fenced body if ``text`` is fenced, else ``text`` unchanged.

    A language-tagged fence wins over a bare fence; when neither matches the
    text is returned as-is (unfenced bare YAML is valid input).
    """
    match = _FENCE_LANG_RE.search(text)
    if match is None:
        match = _FENCE_BARE_RE.search(text)
    return match.group(1) if match else text


def extract_yaml(text: str, *, anchor: Optional[str] = None) -> Any:
    """Parse ``text`` into a Python object, tolerating fences and preamble.

    ``anchor`` is an optional substring to restart parsing from when the first
    attempt fails -- pass a known top-level key (``"items:"``) so preamble
    prose ahead of the YAML does not defeat the parse. Raises
    :class:`YamlExtractionError` when the content cannot be parsed.
    """
    if text is None:
        raise YamlExtractionError("", "input text is None")

    candidate = strip_fence(text)
    try:
        return yaml.safe_load(candidate)
    except yaml.YAMLError as exc:
        if anchor:
            idx = text.find(anchor)
            if idx >= 0:
                try:
                    return yaml.safe_load(text[idx:])
                except yaml.YAMLError:
                    raise YamlExtractionError(
                        text, f"yaml parse failed: {exc}"
                    ) from exc
        raise YamlExtractionError(text, f"yaml parse failed: {exc}") from exc


def extract_mapping(
    text: str,
    *,
    required_keys: Iterable[str] = (),
    anchor: Optional[str] = None,
) -> dict:
    """Parse ``text`` and assert it is a mapping carrying ``required_keys``.

    Raises :class:`YamlExtractionError` when the payload is not a mapping or a
    required key is absent. ``anchor`` defaults to the first required key's
    ``"<key>:"`` form when none is given, so preamble recovery works
    out-of-the-box for the common case.
    """
    keys = tuple(required_keys)
    if anchor is None and keys:
        anchor = f"{keys[0]}:"
    data = extract_yaml(text, anchor=anchor)
    if not isinstance(data, dict):
        raise YamlExtractionError(text, "parsed YAML is not a mapping")
    for key in keys:
        if key not in data:
            raise YamlExtractionError(text, f"missing required key {key!r}")
    return data


__all__ = [
    "YamlExtractionError",
    "strip_fence",
    "extract_yaml",
    "extract_mapping",
]
