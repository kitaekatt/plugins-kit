"""A strict reader for the EDN the pinned metric tools write.

It accepts exactly the subset those tools emit -- maps with keyword keys,
vectors, strings (escapes ``\\\\`` and ``\\"`` only), integers, decimals and
``nil`` -- and raises EdnError on anything else. ``read_dry`` and
``read_crap`` then check the document against the one shape each tool writes,
so a tool change that alters the shape fails loudly instead of being half read.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


class EdnError(ValueError):
    pass


_TOKEN = re.compile(
    r"""
    (?P<ws>[\s,]+)
  | (?P<open>[{\[])
  | (?P<close>[}\]])
  | (?P<string>"(?:[^"\\]|\\.)*")
  | (?P<keyword>:[a-z][a-z0-9-]*(?=[\s,{}\[\]]|$))
  | (?P<number>-?\d+(?:\.\d+)?(?=[\s,{}\[\]]|$))
  | (?P<nil>nil(?=[\s,{}\[\]]|$))
    """,
    re.VERBOSE,
)
_KEYWORD = object()


def _tokens(text: str):
    pos = 0
    while pos < len(text):
        match = _TOKEN.match(text, pos)
        if match is None:
            raise EdnError(f"unsupported EDN at offset {pos}: {text[pos:pos + 30]!r}")
        pos = match.end()
        kind = match.lastgroup
        if kind == "ws":
            continue
        yield kind, match.group(), match.start()


def _unescape(raw: str, offset: int) -> str:
    body = raw[1:-1]
    out = []
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "\\":
            nxt = body[i + 1]
            if nxt not in '\\"':
                raise EdnError(f"unsupported string escape \\{nxt} at offset {offset + i + 1}")
            out.append(nxt)
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def loads(text: str) -> Any:
    """Parse one EDN form from ``text``."""
    stream = list(_tokens(text))
    if not stream:
        raise EdnError("empty EDN document")
    value, index = _form(stream, 0)
    if value is _KEYWORD:
        raise EdnError("a keyword is only accepted as a map key")
    if index != len(stream):
        raise EdnError(f"trailing content at offset {stream[index][2]}")
    return value


def _form(stream, index):
    if index >= len(stream):
        raise EdnError("unexpected end of EDN")
    kind, text, offset = stream[index]
    if kind == "open" and text == "{":
        return _map(stream, index + 1, offset)
    if kind == "open":
        return _vector(stream, index + 1, offset)
    if kind == "string":
        return _unescape(text, offset), index + 1
    if kind == "number":
        return (float(text) if "." in text else int(text)), index + 1
    if kind == "nil":
        return None, index + 1
    if kind == "keyword":
        return _KEYWORD, index + 1
    raise EdnError(f"unexpected {text!r} at offset {offset}")


def _map(stream, index, start):
    result: dict[str, Any] = {}
    while True:
        if index >= len(stream):
            raise EdnError(f"unclosed map opened at offset {start}")
        kind, text, offset = stream[index]
        if kind == "close":
            if text != "}":
                raise EdnError(f"map opened at offset {start} closed by {text!r}")
            return result, index + 1
        if kind != "keyword":
            raise EdnError(f"map key at offset {offset} is not a keyword: {text!r}")
        key = text[1:]
        if key in result:
            raise EdnError(f"duplicate map key :{key} at offset {offset}")
        value, index = _form(stream, index + 1)
        if value is _KEYWORD:
            raise EdnError(f"keyword value for :{key} at offset {offset}")
        result[key] = value


def _vector(stream, index, start):
    result: list[Any] = []
    while True:
        if index >= len(stream):
            raise EdnError(f"unclosed vector opened at offset {start}")
        kind, text, offset = stream[index]
        if kind == "close":
            if text != "]":
                raise EdnError(f"vector opened at offset {start} closed by {text!r}")
            return result, index + 1
        value, index = _form(stream, index)
        if value is _KEYWORD:
            raise EdnError(f"keyword element in vector at offset {offset}")
        result.append(value)


# --- shapes -----------------------------------------------------------------


def _expect_keys(where: str, value: Any, required: set[str], optional: set[str] = frozenset()):
    if not isinstance(value, dict):
        raise EdnError(f"{where}: expected a map, got {type(value).__name__}")
    keys = set(value)
    missing = required - keys
    extra = keys - required - optional
    if missing or extra:
        raise EdnError(f"{where}: missing keys {sorted(missing)}, unexpected keys {sorted(extra)}")


def _expect(where: str, value: Any, *types: type) -> None:
    if not isinstance(value, types) or isinstance(value, bool):
        names = "/".join("nil" if t is type(None) else t.__name__ for t in types)
        raise EdnError(f"{where}: expected {names}, got {value!r}")


def _span(where: str, value: Any) -> dict[str, Any]:
    _expect_keys(where, value, {"file", "start-line", "end-line"})
    _expect(f"{where} :file", value["file"], str)
    _expect(f"{where} :start-line", value["start-line"], int)
    _expect(f"{where} :end-line", value["end-line"], int)
    return {"path": value["file"], "start_line": value["start-line"], "end_line": value["end-line"]}


def read_dry(text: str) -> list[dict[str, Any]]:
    """Candidates from dryer's ``.metrics/dry.edn``; spans as {path, start_line, end_line}."""
    doc = loads(text)
    _expect_keys("dry.edn", doc, {"candidates"})
    _expect("dry.edn :candidates", doc["candidates"], list)
    out = []
    for i, cand in enumerate(doc["candidates"]):
        where = f"dry.edn candidate {i}"
        _expect_keys(where, cand, {"score", "language", "left", "right", "left-nodes", "right-nodes"},
                     {"others"})
        _expect(f"{where} :score", cand["score"], float)
        _expect(f"{where} :language", cand["language"], str)
        _expect(f"{where} :left-nodes", cand["left-nodes"], int)
        _expect(f"{where} :right-nodes", cand["right-nodes"], int)
        others = cand.get("others", [])
        _expect(f"{where} :others", others, list)
        if "others" in cand and not others:
            raise EdnError(f"{where}: :others is present but empty")
        spans = [_span(f"{where} :left", cand["left"]), _span(f"{where} :right", cand["right"])]
        spans += [_span(f"{where} :others {j}", s) for j, s in enumerate(others)]
        out.append({
            "score": cand["score"],
            "language": cand["language"],
            "left_nodes": cand["left-nodes"],
            "right_nodes": cand["right-nodes"],
            "spans": spans,
        })
    return out


def read_crap(text: str) -> list[dict[str, Any]]:
    """Entries from crapper's ``.metrics/crap.edn``."""
    doc = loads(text)
    _expect_keys("crap.edn", doc, {"entries"})
    _expect("crap.edn :entries", doc["entries"], list)
    out = []
    for i, entry in enumerate(doc["entries"]):
        where = f"crap.edn entry {i}"
        _expect_keys(where, entry, {"name", "namespace", "complexity", "coverage", "crap"})
        _expect(f"{where} :name", entry["name"], str)
        _expect(f"{where} :namespace", entry["namespace"], str)
        _expect(f"{where} :complexity", entry["complexity"], int)
        _expect(f"{where} :coverage", entry["coverage"], float, type(None))
        _expect(f"{where} :crap", entry["crap"], float, type(None))
        out.append(dict(entry))
    return out


def read_file(path: Path, reader) -> list[dict[str, Any]]:
    try:
        return reader(path.read_text(encoding="utf-8"))
    except EdnError as exc:
        raise EdnError(f"{path}: {exc}") from exc
