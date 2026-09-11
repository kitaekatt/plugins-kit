"""Check literal local Markdown targets in the frozen review snapshot."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import PurePosixPath
from typing import Mapping
from urllib.parse import unquote_to_bytes

from markdown_it import MarkdownIt
from markdown_it.token import Token

from bootstrap_lib.code_review.mechanical import MechanicalFinding, MechanicalSnapshot

from . import (
    CheckOutcome,
    FrozenRepositoryView,
    MAX_SOURCE_BYTES,
    RepositoryRequest,
    SourceRequests,
)


_CHECK_ID = "local_link_targets"
_PHRASE = "local file and Markdown link targets"
_MARKDOWN_SUFFIXES = (".md", ".markdown")
_TEMPLATE_MARKERS = ("{{", "}}", "{%", "%}", "${")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


@dataclass(frozen=True)
class _Candidate:
    line: int
    occurrence: int
    target: str
    fragment: str


class _ExplicitAnchorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.values: set[str] = set()

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        del tag
        for name, value in attrs:
            if name.lower() in {"id", "name"} and value is not None:
                self.values.add(value)

    handle_startendtag = handle_starttag


def _decode_component(value: str) -> str:
    return unquote_to_bytes(value).decode("utf-8", errors="strict")


def _is_ignored_destination(destination: str) -> bool:
    lowered = destination.lower()
    return (
        not destination
        or lowered.startswith(("http://", "https://", "mailto:"))
        or destination.startswith("//")
        or any(marker in destination for marker in _TEMPLATE_MARKERS)
    )


def _normalize_destination(source: str, destination: str) -> tuple[str, str] | None:
    """Decode and normalize one parser-produced destination.

    ``None`` means that the destination is deliberately outside this check.
    Invalid local-looking paths raise ``ValueError`` so coverage is not claimed.
    """
    if _is_ignored_destination(destination):
        return None
    path_part, separator, fragment_part = destination.partition("#")
    path_part, query_separator, query_part = path_part.partition("?")
    try:
        path_part = _decode_component(path_part)
        query = _decode_component(query_part) if query_separator else ""
        fragment = _decode_component(fragment_part) if separator else ""
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"invalid percent-encoded link destination: {destination!r}") from exc
    if any(
        "\x00" in component or "\\" in component
        for component in (path_part, query, fragment)
    ):
        raise ValueError(f"unsupported local link destination: {destination!r}")
    if _SCHEME.match(path_part):
        raise ValueError(f"unsupported URL scheme in link destination: {destination!r}")
    if path_part.startswith("/"):
        raise ValueError(f"absolute link destination is unsupported: {destination!r}")
    if path_part.endswith("/"):
        return None

    if path_part:
        parts = list(PurePosixPath(source).parent.parts)
        for part in path_part.split("/"):
            if part in {"", "."}:
                continue
            if part == "..":
                if not parts:
                    raise ValueError(f"link destination escapes repository root: {destination!r}")
                parts.pop()
            else:
                parts.append(part)
        target = "/".join(parts)
    else:
        target = source
    if not target:
        raise ValueError(f"empty repository target in link destination: {destination!r}")
    if fragment and not target.lower().endswith(_MARKDOWN_SUFFIXES):
        return None
    return target, fragment


def _hrefs(children: list[Token] | None) -> list[str]:
    result: list[str] = []
    for child in children or []:
        if child.type == "link_open":
            result.append(child.attrGet("href") or "")
        elif child.type == "image":
            result.append(child.attrGet("src") or "")
    return result


def _reference_labels_in_use(tokens: list[Token]) -> set[str]:
    """Return normalized identities of full, collapsed, and shortcut uses."""
    used: set[str] = set()
    for token in tokens:
        if token.type != "inline":
            continue
        for child in token.children or []:
            label = child.meta.get("label")
            if child.type in {"link_open", "image"} and isinstance(label, str):
                used.add(label)
    return used


def _parse_candidates(
    snapshot: MechanicalSnapshot,
) -> tuple[tuple[_Candidate, ...], str | None]:
    if snapshot.post_image_text is None or snapshot.added_lines is None:
        return (), "Markdown post-image or added-line map unavailable"
    if len(snapshot.post_image_text.encode("utf-8")) > MAX_SOURCE_BYTES:
        return (), f"Markdown source exceeds {MAX_SOURCE_BYTES} byte limit"
    md = MarkdownIt("commonmark", {"store_labels": True})
    env: dict[str, object] = {}
    try:
        tokens = md.parse(snapshot.post_image_text, env)
    except Exception as exc:
        return (), f"Markdown parse failed: {exc}"
    added = {line for line, _ in snapshot.added_lines}
    candidates: list[_Candidate] = []
    occurrence = 0

    for token in tokens:
        if token.type != "inline" or not _hrefs(token.children):
            continue
        assert token.map is not None
        local_destinations = [
            href
            for href in _hrefs(token.children)
            if not _is_ignored_destination(href)
        ]
        if local_destinations and token.map[1] - token.map[0] != 1:
            return (), "local link in a multi-line Markdown inline block is unsupported"
        line = token.map[0] + 1
        if line not in added:
            continue
        for destination in _hrefs(token.children):
            try:
                normalized = _normalize_destination(snapshot.file, destination)
            except ValueError as exc:
                return (), str(exc)
            if normalized is None:
                continue
            target, fragment = normalized
            candidates.append(_Candidate(line, occurrence, target, fragment))
            occurrence += 1

    references = env.get("references", {})
    if isinstance(references, dict):
        used_labels = _reference_labels_in_use(tokens)
        for label, record in references.items():
            if not isinstance(record, dict):
                continue
            destination = record.get("href")
            line_map = record.get("map")
            if not isinstance(destination, str) or not isinstance(line_map, (list, tuple)):
                continue
            if label not in used_labels:
                continue
            if not _is_ignored_destination(destination) and line_map[1] - line_map[0] != 1:
                return (), "local multi-line Markdown reference definition is unsupported"
            line = line_map[0] + 1
            if line not in added:
                continue
            try:
                normalized = _normalize_destination(snapshot.file, destination)
            except ValueError as exc:
                return (), str(exc)
            if normalized is None:
                continue
            target, fragment = normalized
            candidates.append(_Candidate(line, occurrence, target, fragment))
            occurrence += 1

    candidates.sort(key=lambda item: (item.line, item.occurrence, item.target, item.fragment))
    return tuple(candidates), None


def _inline_text(children: list[Token] | None) -> str:
    pieces: list[str] = []
    for child in children or []:
        if child.type in {"text", "code_inline"}:
            pieces.append(child.content)
        elif child.type == "image":
            pieces.append(_inline_text(child.children))
        elif child.type in {"softbreak", "hardbreak"}:
            pieces.append(" ")
    return "".join(pieces)


def _heading_text(token: Token) -> str:
    return _inline_text(token.children)


def _slug(text: str) -> str:
    lowered = text.lower().strip()
    without_punctuation = "".join(
        character
        for character in lowered
        if character in "-_" or not unicodedata.category(character).startswith("P")
    )
    return re.sub(r"\s+", "-", without_punctuation)


def _markdown_anchors(data: bytes) -> set[str]:
    text = data.decode("utf-8", errors="strict")
    tokens = MarkdownIt("commonmark").parse(text)
    parser = _ExplicitAnchorParser()
    for token in tokens:
        if token.type in {"html_block", "html_inline"}:
            parser.feed(token.content)
        for child in token.children or []:
            if child.type == "html_inline":
                parser.feed(child.content)
    anchors = set(parser.values)
    generated: set[str] = set()
    for index, token in enumerate(tokens[:-1]):
        if token.type != "heading_open" or tokens[index + 1].type != "inline":
            continue
        bare = _slug(_heading_text(tokens[index + 1]))
        if not bare:
            continue
        slug = bare
        suffix = 1
        while slug in generated:
            slug = f"{bare}-{suffix}"
            suffix += 1
        generated.add(slug)
    return anchors | generated


class LocalLinkTargetsCheck:
    check_id = _CHECK_ID
    phrase = _PHRASE

    def collect(
        self, sources: Mapping[str, MechanicalSnapshot]
    ) -> Mapping[str, SourceRequests]:
        result: dict[str, SourceRequests] = {}
        for source, snapshot in sources.items():
            if (
                not source.lower().endswith(_MARKDOWN_SUFFIXES)
                or snapshot.post_image_text is None
                or snapshot.added_lines is None
            ):
                continue
            candidates, diagnostic = _parse_candidates(snapshot)
            requests: dict[str, bool] = {}
            for candidate in candidates:
                requests[candidate.target] = requests.get(candidate.target, False) or bool(
                    candidate.fragment
                )
            result[source] = SourceRequests(
                tuple(
                    RepositoryRequest(target, need_content)
                    for target, need_content in requests.items()
                ),
                diagnostic,
            )
        return result

    def evaluate(
        self,
        sources: Mapping[str, MechanicalSnapshot],
        view: FrozenRepositoryView,
        requests: Mapping[str, SourceRequests],
    ) -> Mapping[str, CheckOutcome]:
        outcomes: dict[str, CheckOutcome] = {}
        for source, source_requests in requests.items():
            if source_requests.diagnostic is not None:
                outcomes[source] = CheckOutcome(False, diagnostic=source_requests.diagnostic)
                continue
            candidates, diagnostic = _parse_candidates(sources[source])
            if diagnostic is not None:
                outcomes[source] = CheckOutcome(False, diagnostic=diagnostic)
                continue
            targets = tuple(dict.fromkeys(candidate.target for candidate in candidates))
            stats = view.stat_many(targets)
            content_targets = tuple(
                dict.fromkeys(
                    candidate.target
                    for candidate in candidates
                    if candidate.fragment and stats[candidate.target].kind == "file"
                )
            )
            reads = view.read_many(content_targets)
            failures = [
                result.diagnostic or f"target {target!r} is unavailable"
                for target, result in stats.items()
                if result.kind not in {"file", "directory", "missing"}
            ]
            failures.extend(
                result.diagnostic or f"target content {target!r} is unavailable"
                for target, result in reads.items()
                if result.kind != "file" or result.data is None
            )
            if failures:
                outcomes[source] = CheckOutcome(False, diagnostic="; ".join(failures))
                continue

            anchors: dict[str, set[str]] = {}
            try:
                for target, result in reads.items():
                    assert result.data is not None
                    anchors[target] = _markdown_anchors(result.data)
            except Exception as exc:
                outcomes[source] = CheckOutcome(
                    False, diagnostic=f"Markdown target parse failed: {exc}"
                )
                continue

            findings: list[MechanicalFinding] = []
            seen: set[tuple[int, str, str, str]] = set()
            for candidate in candidates:
                stat = stats[candidate.target]
                if stat.kind == "directory":
                    continue
                if stat.kind == "missing":
                    detail = (
                        f"{candidate.target!r}: target does not exist in review snapshot"
                    )
                elif candidate.fragment and candidate.fragment not in anchors[candidate.target]:
                    detail = (
                        f"{candidate.target!r}#{candidate.fragment}: anchor does not exist "
                        "in target in review snapshot"
                    )
                else:
                    continue
                identity = (candidate.line, candidate.target, candidate.fragment, detail)
                if identity in seen:
                    continue
                seen.add(identity)
                findings.append(
                    {"check": _CHECK_ID, "line": candidate.line, "detail": detail}
                )
            outcomes[source] = CheckOutcome(True, tuple(findings))
        return outcomes


CHECK = LocalLinkTargetsCheck()


__all__ = ["CHECK", "LocalLinkTargetsCheck"]
