"""Deterministic structural repair of a model's almost-JSON answer.

Models drop, swap or add a bracket at the deepest nesting levels far more often
than they get content wrong. A schema fixes the container type at nearly every
path, so a scan that follows the schema knows which token cannot continue the
open container and which single-token edits could make it fit.

What this module may change, and nothing else:

- strip a code fence, or prose standing outside the root value;
- insert or delete one of ``[ ] { } ,``;
- turn the ``,`` written where a property name's ``:`` belongs into ``:``;
- restore the closing quote of a property name that was written after its
  colon (``{"text:"x"`` for ``{"text":"x"``).

No string, number, literal or key is rewritten. Only a failing scan point
gets candidate edits, so a document that already parses is never touched.
Every combination of at most ``max_edits`` edits is followed and the answer is
repaired only when EXACTLY ONE resulting document conforms to the schema. Two
different documents mean the structure is a guess, and the answer is declined
as ``ambiguous``. A text that ends inside the document is never closed: a cut
off answer is lost content, not a slip.

A repaired text is a candidate, not a verdict: the caller still validates it
in full (``finalize_contract`` does).

Limits: the scan follows ``type`` (object/array/scalars), ``properties``,
``required``, ``additionalProperties``, ``items``, ``minItems``, ``maxItems``,
``anyOf``/``oneOf`` and local ``$ref``. A value the schema leaves entirely
open (``{}``) can hold scalars only; a container there is refused and the
answer is reported unrecoverable.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Optional, Set, Tuple

from .json_schema import conforms, resolve_local_ref

#: Most single-token edits one repair may apply (wrapper strips included).
MAX_REPAIR_EDITS = 24
#: Most candidate documents one repair may scan before it gives up.
MAX_REPAIR_WALKS = 1500
_MAX_CLOSER_RUN = 8
_MAX_REF_DEPTH = 16
#: A misquoted key stands at most this many tokens behind the failing point:
#: ``{"text:", which`` fails one token after the key (the comma).
_MISQUOTED_KEY_LOOKBACK = 2

STATUS_VALID = "valid"
STATUS_REPAIRED = "repaired"
STATUS_AMBIGUOUS = "ambiguous"
STATUS_UNRECOVERABLE = "unrecoverable"

_JSON_TOKEN_RE = re.compile(
    r'[ \t\r\n]*(?:([{}\[\],:])|("(?:[^"\\\x00-\x1f]|\\.)*")'
    r"|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|true|false|null))"
)
_FENCE_RE = re.compile(r"```[A-Za-z0-9_+-]*[ \t]*\r?\n(.*?)\r?\n?[ \t]*```", re.DOTALL)
# token record: (whitespace before it, text, offset in the raw answer, inserted)
_Token = Tuple[str, str, int, bool]


@dataclass(frozen=True)
class RepairEdit:
    """One structural change made to a raw answer.

    ``op`` is ``insert`` or ``delete`` with ``token`` one of ``[ ] { } ,``;
    ``strip`` with ``token`` ``leading``, ``trailing`` or ``fence`` for text
    outside the root value; ``replace`` with ``token`` ``:`` for the comma
    written after a property name; or ``insert`` with ``token`` ``"`` for the
    closing quote of a misquoted property name. ``offset`` is a position in
    the raw answer.
    """

    op: str
    token: str
    offset: int

    def to_json(self) -> dict:
        return {"op": self.op, "token": self.token, "offset": self.offset}


@dataclass(frozen=True)
class JsonRepair:
    """The outcome of :func:`repair_json_structure` for one raw answer.

    ``status`` is ``valid`` (the answer already parses; ``text`` is the
    answer), ``repaired`` (``text`` parses and has the schema's shape;
    ``edits`` say how), or ``ambiguous`` / ``unrecoverable`` (``text`` is the
    answer, ``reason`` says why).
    """

    status: str
    text: Any
    edits: Tuple[RepairEdit, ...] = ()
    reason: str = ""


class _Guide:
    """The schema as the scan reads it: alternatives, properties, string keys."""

    def __init__(self, schema: Mapping) -> None:
        self.root = schema
        self.string_keys = self._string_keys(schema)

    def alternatives(self, schema: Mapping, depth: int = 0) -> List[Mapping]:
        if depth > _MAX_REF_DEPTH:
            return []
        if "$ref" in schema:
            return self.alternatives(resolve_local_ref(self.root, schema["$ref"]), depth + 1)
        for key in ("oneOf", "anyOf"):
            if key in schema:
                return [
                    alt
                    for option in schema[key]
                    for alt in self.alternatives(option, depth + 1)
                ]
        return [schema]

    def property_alternatives(self, alt: Mapping, key: str) -> Optional[List[Mapping]]:
        """The alternatives for ``key``'s value, or None when ``key`` is not allowed."""
        properties = alt.get("properties", {})
        if key in properties:
            return self.alternatives(properties[key])
        extra = alt.get("additionalProperties", True)
        if extra is False:
            return None
        if isinstance(extra, Mapping):
            return self.alternatives(extra)
        return [{}]

    def scalar_fits(self, alt: Mapping, value: Any) -> bool:
        if alt.get("type") in ("object", "array"):
            return False
        return conforms(self.root, alt, value)

    def _string_keys(self, schema: Any) -> FrozenSet[str]:
        found: Set[str] = set()

        def visit(node: Any) -> None:
            if isinstance(node, Mapping):
                properties = node.get("properties")
                if isinstance(properties, Mapping):
                    for name, sub in properties.items():
                        if isinstance(sub, Mapping) and self._is_string(sub):
                            found.add(name)
                for sub in node.values():
                    visit(sub)
            elif isinstance(node, list):
                for sub in node:
                    visit(sub)

        visit(schema)
        return frozenset(found)

    def _is_string(self, schema: Mapping) -> bool:
        try:
            alts = self.alternatives(schema)
        except ValueError:
            return False
        return bool(alts) and all(alt.get("type") == "string" for alt in alts)


def _misquoted_key(guide: _Guide, tokens: Sequence[_Token]) -> Optional[int]:
    """Index of a ``"KEY:"`` token whose closing quote moved past its colon.

    Only a string-valued schema key in property-name position (after ``{`` or
    ``,``) qualifies, and only within ``_MISQUOTED_KEY_LOOKBACK`` tokens of the
    point where the token stream failed.
    """
    for index in range(len(tokens) - 1, max(-1, len(tokens) - 1 - _MISQUOTED_KEY_LOOKBACK), -1):
        body = tokens[index][1]
        if (
            body.startswith('"')
            and body.endswith(':"')
            and body[1:-2] in guide.string_keys
            and index > 0
            and tokens[index - 1][1] in ("{", ",")
            and not tokens[index][3]
        ):
            return index
    return None


def _tokenize(
    guide: _Guide, text: str, base: int, edits: Optional[List[RepairEdit]] = None
) -> Optional[Tuple[List[_Token], str]]:
    tokens: List[_Token] = []
    pos = 0
    repaired: Set[int] = set()
    while True:
        match = _JSON_TOKEN_RE.match(text, pos)
        if match is None:
            rest = text[pos:]
            if not rest.strip(" \t\r\n"):
                return tokens, rest
            index = None if edits is None else _misquoted_key(guide, tokens)
            if index is None or index in repaired:
                return None
            # `"KEY:"` + value: the key is `"KEY"`, the colon is the raw one,
            # and the value string opens at the raw closing quote.
            prefix, body, offset, _ = tokens[index]
            colon = offset + len(body) - 2
            del tokens[index:]
            tokens.append((prefix, body[:-2] + '"', offset, False))
            tokens.append(("", ":", colon, False))
            edits.append(RepairEdit("insert", '"', colon))
            repaired.add(index)
            pos = colon + 1 - base
            continue
        body = match.group(match.lastindex)
        start = match.end() - len(body)
        tokens.append((text[pos:start], body, base + start, False))
        pos = match.end()


def _walk(guide: _Guide, tokens: Sequence[_Token]) -> Optional[Tuple[int, str, str]]:
    """Find the first token the schema cannot accept.

    Returns None for a complete acceptable document, else the token index
    (the token count when the text ends early), the closer of the innermost
    open container ("" when none is open) and the opener the schema wanted
    where a value of another type stands ("" otherwise). Where the schema's
    alternatives cannot be told apart yet the scan accepts any of them, so it
    never refuses a prefix of a valid document.
    """
    # frame: [closer, alternatives, state, keys seen or item count, value
    # alternatives, current key]. States: "first" (a member or the closer),
    # "member" (a member is required), "colon", "value", "after".
    frames: List[List[Any]] = []
    root = guide.alternatives(guide.root)
    complete = False
    for index, token in enumerate(tokens):
        text = token[1]
        if complete:
            return index, "", ""
        top = frames[-1] if frames else None
        top_closer = top[0] if top else ""
        state = "value" if top is None else top[2]
        in_array = top is not None and top[0] == "]"
        if in_array and state in ("first", "member"):
            if text == "]" and state == "first":
                state = "after"
            else:
                state = "value"
        if state == "value":
            options = root if top is None else top[4]
            objects = [alt for alt in options if alt.get("type") == "object"]
            arrays = [alt for alt in options if alt.get("type") == "array"]
            wanted = "[" if arrays and not objects else "{" if objects and not arrays else ""
            if text == "{":
                if not objects:
                    return index, top_closer, wanted
                following = tokens[index + 1][1] if index + 1 < len(tokens) else ""
                if following.startswith('"') and not any(
                    guide.property_alternatives(alt, following[1:-1]) is not None
                    for alt in objects
                ):
                    # Its first key belongs to no object allowed here: the
                    # object itself is misplaced, so refuse it, not the key.
                    return index, top_closer, ""
                frames.append(["}", objects, "first", set(), [], None])
                continue
            if text == "[":
                if not arrays:
                    return index, top_closer, wanted
                items: List[Mapping] = []
                for alt in arrays:
                    for item in guide.alternatives(alt.get("items", {})):
                        if not any(item is seen for seen in items):
                            items.append(item)
                frames.append(["]", arrays, "first", 0, items, None])
                continue
            if text in "}],:":
                return index, top_closer, ""
            try:
                value = json.loads(text)
            except ValueError:
                return index, top_closer, ""
            if not any(guide.scalar_fits(alt, value) for alt in options):
                return index, top_closer, wanted
            if top is not None and not in_array:
                top[1] = [
                    alt
                    for alt in top[1]
                    if any(
                        guide.scalar_fits(option, value)
                        for option in guide.property_alternatives(alt, top[5]) or []
                    )
                ]
        elif state == "after":
            if text == ",":
                top[2] = "member"
                continue
            if text != top_closer:
                return index, top_closer, ""
            if in_array:
                fitting = [
                    alt
                    for alt in top[1]
                    if alt.get("minItems", 0) <= top[3] <= alt.get("maxItems", top[3])
                ]
            else:
                fitting = [
                    alt for alt in top[1] if set(alt.get("required", ())) <= top[3]
                ]
            if not fitting:
                return index, top_closer, ""
            frames.pop()
        elif state == "colon":
            if text != ":":
                return index, top_closer, ":" if text == "," else ""
            top[2] = "value"
            continue
        else:  # an object wants a key, or its closer when it is still empty
            if text == "}" and state == "first":
                if not any(not alt.get("required") for alt in top[1]):
                    return index, top_closer, ""
                frames.pop()
            else:
                if not text.startswith('"'):
                    return index, top_closer, ""
                try:
                    key = json.loads(text)
                except ValueError:
                    return index, top_closer, ""
                having = [
                    alt for alt in top[1] if guide.property_alternatives(alt, key) is not None
                ]
                if key in top[3] or not having:
                    return index, top_closer, ""
                top[1] = having
                top[3].add(key)
                top[5] = key
                top[4] = []
                for alt in having:
                    for option in guide.property_alternatives(alt, key) or []:
                        if not any(option is seen for seen in top[4]):
                            top[4].append(option)
                top[2] = "colon"
                continue
        # A value just ended, or a container just closed.
        if not frames:
            complete = True
            continue
        parent = frames[-1]
        if parent[0] == "]":
            parent[3] += 1
        parent[2] = "after"
    if complete:
        return None
    return len(tokens), frames[-1][0] if frames else "", ""


def _candidates(
    tokens: Sequence[_Token], index: int, closer: str, wanted: str
) -> List[Tuple[RepairEdit, List[_Token]]]:
    """Every single structural edit worth trying at one refused token."""
    out: List[Tuple[RepairEdit, List[_Token]]] = []
    if index >= len(tokens):
        # The text ends inside the document. Closing it here would pass off a
        # cut-off answer as a complete one, so nothing is appended.
        return out
    here = tokens[index][1]
    before = tokens[index - 1][1] if index else ""
    if wanted == ":":
        # A comma where the colon after a property name belongs.
        prefix, _text, offset, _ = tokens[index]
        out.append(
            (
                RepairEdit("replace", ":", offset),
                [*tokens[:index], (prefix, ":", offset, True), *tokens[index + 1 :]],
            )
        )
        return out

    def insert(at: int, text: str) -> None:
        offset = tokens[at][2]
        out.append(
            (
                RepairEdit("insert", text, offset),
                [*tokens[:at], ("", text, offset, True), *tokens[at:]],
            )
        )

    def delete(at: int) -> None:
        prefix, text, offset, _ = tokens[at]
        rest = list(tokens[at + 1 :])
        if rest:
            rest[0] = (prefix + rest[0][0], *rest[0][1:])
        out.append((RepairEdit("delete", text, offset), [*tokens[:at], *rest]))

    if closer:
        insert(index, closer)
        if before == ",":
            insert(index - 1, closer)
    if here in "}]":
        delete(index)
    if wanted and here != wanted:
        if here in "{[":
            delete(index)
        if wanted == "[" or (
            wanted == "{"
            and here.startswith('"')
            and index + 1 < len(tokens)
            and tokens[index + 1][1] == ":"
        ):
            # `{` only before a key where the schema wants an object: the
            # object can start nowhere else. `[` goes where the schema wants an
            # array; its end is the first token the array cannot hold, so a
            # missing `[` has one reading.
            insert(index, wanted)
    if before == "," and here in "}]":
        delete(index - 1)
    if here not in ",:}]" and before and before not in ",:{[":
        insert(index, ",")
    at = index - (2 if before == "," else 1)
    for _ in range(_MAX_CLOSER_RUN):
        if at < 0 or tokens[at][1] not in ("}", "]") or tokens[at][3]:
            break
        delete(at)
        at -= 1
    return out


def _strip_wrapper(guide: _Guide, text: str) -> Tuple[int, int, List[RepairEdit]]:
    """Bounds of the answer without a fence or text outside the root value."""
    edits: List[RepairEdit] = []
    openers = ""
    for alt in guide.alternatives(guide.root):
        if alt.get("type") == "object" and "{" not in openers:
            openers += "{"
        elif alt.get("type") == "array" and "[" not in openers:
            openers += "["
    if not openers:
        return 0, len(text), edits
    fence = _FENCE_RE.search(text)
    if fence is not None and fence.group(1).lstrip()[:1] in tuple(openers):
        edits.append(RepairEdit("strip", "fence", fence.start()))
        return fence.start(1), fence.end(1), edits
    starts = [i for i in (text.find(c) for c in openers) if i >= 0]
    if not starts:
        return 0, len(text), edits
    start = min(starts)
    if text[:start].strip():
        edits.append(RepairEdit("strip", "leading", 0))
    else:
        start = 0
    closers = "}" if openers == "{" else "]" if openers == "[" else "}]"
    stop = max(text.rfind(c) for c in closers) + 1
    tail = text[stop:]
    if tail.strip() and not any(char in tail for char in '"[{'):
        edits.append(RepairEdit("strip", "trailing", stop))
    else:
        stop = len(text)
    return start, stop, edits


def repair_json_structure(
    schema: Mapping,
    text: Any,
    *,
    max_edits: int = MAX_REPAIR_EDITS,
    max_walks: int = MAX_REPAIR_WALKS,
) -> JsonRepair:
    """Make an answer that is not JSON parse, by structural edits alone.

    ``schema`` must already have passed ``check_schema``. A valid answer
    comes back untouched (``valid``). Otherwise every combination of at most
    ``max_edits`` single-token edits at the points where the scan is refused
    is followed; the answer is repaired only when exactly one resulting
    document conforms to the schema (``repaired``). Two different documents
    are ``ambiguous``; anything else is ``unrecoverable``.
    """
    if not isinstance(text, str):
        return JsonRepair(STATUS_UNRECOVERABLE, text, reason="not text")
    try:
        json.loads(text)
        return JsonRepair(STATUS_VALID, text)
    except (ValueError, RecursionError):
        pass
    guide = _Guide(schema)
    start, stop, wrapper = _strip_wrapper(guide, text)
    scanned = _tokenize(guide, text[start:stop], start, wrapper)
    if scanned is None:
        return JsonRepair(STATUS_UNRECOVERABLE, text, reason="not a JSON token stream")
    first, tail = scanned
    budget = max_edits - len(wrapper)
    if budget < 0:
        return JsonRepair(STATUS_UNRECOVERABLE, text, reason="edit bound spent")
    walks = 0
    seen = {"".join(token[1] for token in first)}
    level: List[Any] = [(first, tuple(wrapper), _walk(guide, first))]
    found: Dict[str, Tuple[str, Tuple[RepairEdit, ...]]] = {}
    for depth in range(budget + 1):
        following: List[Any] = []
        for tokens, edits, refusal in level:
            if refusal is None:
                body = "".join(token[0] + token[1] for token in tokens) + tail
                try:
                    value = json.loads(body)
                except (ValueError, RecursionError):
                    continue
                if conforms(schema, schema, value):
                    found.setdefault("".join(token[1] for token in tokens), (body, edits))
                continue
            if depth == budget:
                continue
            index, closer, wanted = refusal
            offset = tokens[index][2] if index < len(tokens) else stop
            for edit, changed in _candidates(tokens, index, closer, wanted):
                key = "".join(token[1] for token in changed)
                if key in seen:
                    continue
                seen.add(key)
                walks += 1
                if walks > max_walks:
                    return JsonRepair(
                        STATUS_UNRECOVERABLE, text, reason="repair search budget spent"
                    )
                result = _walk(guide, changed)
                if result is not None:
                    at = result[0]
                    if at < len(changed) and changed[at][3]:
                        continue  # the edit itself is refused
                    if (changed[at][2] if at < len(changed) else stop) < offset:
                        continue  # the scan got no further than before
                following.append((changed, (*edits, edit), result))
        level = following
        if not level:
            break
    if not found:
        return JsonRepair(
            STATUS_UNRECOVERABLE,
            text,
            reason="no structural edit yields a document of the schema's shape",
        )
    if len(found) > 1:
        return JsonRepair(
            STATUS_AMBIGUOUS,
            text,
            reason="%d different structures fit the schema" % len(found),
        )
    body, edits = next(iter(found.values()))
    return JsonRepair(STATUS_REPAIRED, body, edits)


__all__ = [
    "MAX_REPAIR_EDITS",
    "MAX_REPAIR_WALKS",
    "STATUS_VALID",
    "STATUS_REPAIRED",
    "STATUS_AMBIGUOUS",
    "STATUS_UNRECOVERABLE",
    "RepairEdit",
    "JsonRepair",
    "repair_json_structure",
]
