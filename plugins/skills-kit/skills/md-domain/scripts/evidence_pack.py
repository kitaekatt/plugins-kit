#!/usr/bin/env python3
"""evidence_pack.py -- build deterministic repository evidence for one Markdown
audit subject.

Given a repository root and a repository-relative Markdown path, this module
computes an "evidence pack": facts about the file and its repository, laid out
in six sections -- IDENTITY, MEASUREMENTS, REFERENCES, ANCESTORS, CLAIM
EVIDENCE, MECHANICAL -- so an auditing model does not have to discover them.
The pack is a set of FACTS, never findings; a row with no rule violation is not
a finding.

Usage:
    python evidence_pack.py <repo-root> <repo-relative-path>
    python evidence_pack.py <repo-root> <path> --no-compact --max-chars 12000

Public surface:
    build_pack(repo_root, rel_path, *, compact=True, max_chars=24000) -> str
    build_structured(repo_root, rel_path) -> dict

Makes no model calls. Stdlib-only, plus an in-process import of
`skills_kit_lib.audit` (this plugin's own library) for the MECHANICAL section's
contract verdicts; the section degrades to a single "unavailable" row when that
import fails.
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import unquote, urlsplit

# skills_kit_lib lives at the plugin root; make it importable regardless of
# which interpreter or venv launched this script. The import itself is
# deferred to _audit_verdicts so the rest of the pack stays stdlib-only.
_PLUGIN_ROOT = Path(__file__).resolve().parents[3]
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))


CODE_EXTENSIONS = {
    ".c", ".cc", ".cpp", ".cs", ".gd", ".go", ".h", ".hpp", ".java",
    ".js", ".mjs", ".cjs", ".py", ".rb", ".rs", ".sh", ".ts", ".tsx",
}
DATA_EXTENSIONS = {".csv", ".godot", ".json", ".toml", ".tres", ".xml", ".yaml", ".yml"}
KNOWN_EXTENSIONS = CODE_EXTENSIONS | DATA_EXTENSIONS | {
    ".bat", ".cfg", ".html", ".ini", ".md", ".plan", ".ps1", ".svg", ".txt",
}
CLAIM_VERBS = re.compile(
    r"\b(?:lives|is\s+at|is\s+defined|defined|exports?|returns?|reads?|writes?|calls?|"
    r"defaults?|runs?|uses?)\b",
    re.IGNORECASE,
)
DEIXIS = re.compile(r"\b(?:recent|recently|new|currently|now|upcoming|soon)\b|\bjust\s+shipped\b", re.I)
PATH_LINE = re.compile(r"(?<![\w./-])((?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+):([0-9]+)\b")
MARKDOWN_LINK = re.compile(r"!?\[([^\]]*)\]\(([^)]+)\)")
BACKTICK = re.compile(r"`([^`\n]+)`")
MODAL = re.compile(r"\b(?:must|never|always|only|do\s+not)\b", re.I)


def artifact_of(path: str | Path) -> str:
    """Classify a Markdown artifact using both its basename and path."""
    pure = Path(path)
    parts = pure.parts
    if pure.name == "CLAUDE.md":
        return "claude-md"
    if pure.name == "SKILL.md":
        return "skill"
    for index, part in enumerate(parts):
        if part == "skills" and index + 2 < len(parts) and parts[index + 2] == "references":
            return "skill-reference"
    if pure.name == "CLAUDE-potential-defects.md":
        return "project-doc"
    return "project-doc"


def _ascii(text: str) -> str:
    pieces: list[str] = []
    for char in text:
        value = ord(char)
        if value < 128:
            pieces.append(char)
        elif value <= 0xFFFF:
            pieces.append(f"\\u{value:04x}")
        else:
            value -= 0x10000
            pieces.append(f"\\u{0xD800 + (value >> 10):04x}\\u{0xDC00 + (value & 0x3FF):04x}")
    return "".join(pieces)


def ascii_text(text: str) -> str:
    """Public alias for the pack's ASCII escaping.

    emit_audit_jobs inlines the standards and subject documents into an
    ASCII-only job document and must escape them exactly as the pack escapes its
    own rows, so the document and the evidence about it read alike.
    """
    return _ascii(text)


def _display(text: str, limit: int = 180) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[: limit - 3] + "..."
    return _ascii(text).replace('"', '\\"')


def _criteria(artifact: str, family: str) -> str:
    if family == "identity":
        if artifact == "claude-md":
            return "A-1, CD-1, cd_anchor_modality_classify"
        if artifact == "skill":
            return "required_frontmatter, yaml_contract_block"
        if artifact == "skill-reference":
            return "PD-1, placement_not_in_skill_dir"
        return "PD-1, placement_not_in_skill_dir"
    if family == "measurements":
        if artifact == "claude-md":
            return "R-3, crp_size_signal, hygiene_thresholds"
        if artifact == "skill":
            return "crp_placement, hygiene_thresholds"
        return "PD-7, hygiene_thresholds"
    if family == "references":
        if artifact == "claude-md":
            return "A-1, CD-2, CD-3, cd_fidelity_anchor_resolves, cd_fidelity_line_anchor, hygiene_thresholds"
        if artifact == "skill":
            return "adp_back_reference, references_reachable_from_skill_md, hygiene_thresholds"
        return "PD-H1, hygiene_thresholds, mechanical_convention_hygiene"
    if family == "ancestors":
        if artifact == "claude-md":
            return "C-1, H-11, ccp_cross_file_duplication"
        if artifact == "skill":
            return "M_ancestor_convention_violation"
        return "PD-11, ancestor_convention_conformance"
    if family == "claims":
        return "CD-4, cd_fidelity_claim_holds"
    if artifact == "claude-md":
        return "H-11, hygiene_thresholds"
    if artifact == "skill":
        return "required_frontmatter, description_quality, yaml_contract_block, mixed_type_signal, hygiene_thresholds"
    return "PD-H1, PD-11, hygiene_thresholds, mechanical_convention_hygiene"


def _headings(text: str) -> list[tuple[int, int, str]]:
    result = []
    fenced = False
    for number, line in enumerate(text.splitlines(), 1):
        if re.match(r"^\s*(```|~~~)", line):
            fenced = not fenced
            continue
        if fenced:
            continue
        match = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if match:
            result.append((number, len(match.group(1)), match.group(2).strip()))
    return result


def _slug_counts(text: str) -> set[str]:
    slugs: set[str] = set()
    counts: Counter[str] = Counter()
    for _, _, heading in _headings(text):
        slug = heading.strip().lower()
        slug = re.sub(r"[^\w\- ]", "", slug, flags=re.UNICODE)
        slug = re.sub(r"\s+", "-", slug)
        count = counts[slug]
        counts[slug] += 1
        slugs.add(slug if count == 0 else f"{slug}-{count}")
    return slugs


def _resolve_path(repo: Path, subject: Path, token: str, *, markdown: bool = False) -> tuple[str, Path | None, str | None]:
    raw = token.strip().strip("<>\"'")
    if markdown:
        raw = raw.split(maxsplit=1)[0]
    parsed = urlsplit(raw)
    if parsed.scheme or raw.startswith("//"):
        return "external", None, parsed.fragment or None
    path_text = unquote(parsed.path).rstrip(".,;)")
    fragment = unquote(parsed.fragment) or None
    if not path_text:
        return "exists", subject, fragment
    candidate_text = path_text.split(":", 1)[0] if re.search(r":\d+$", path_text) else path_text
    candidate = Path(candidate_text)
    if candidate.is_absolute():
        resolved = candidate
    else:
        local = (subject.parent / candidate).resolve()
        root_relative = (repo / candidate).resolve()
        resolved = local if local.exists() or not root_relative.exists() else root_relative
    try:
        if resolved.is_dir():
            return "directory", resolved, fragment
        if resolved.is_file():
            return "exists", resolved, fragment
    except OSError:
        pass
    return "missing", resolved, fragment


def _rel(repo: Path, path: Path | None) -> str:
    if path is None:
        return "-"
    try:
        return path.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        return _ascii(str(path))


def _text_file(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _direct_files(directory: Path) -> list[Path]:
    try:
        return sorted(
            (entry for entry in directory.iterdir() if entry.is_file() and entry.suffix.lower() in CODE_EXTENSIONS),
            key=lambda entry: entry.name,
        )
    except OSError:
        return []


def _ancestors(repo: Path, subject: Path) -> list[Path]:
    result = []
    directory = subject.parent
    while True:
        candidate = directory / "CLAUDE.md"
        if candidate != subject and candidate.is_file():
            result.append(candidate)
        if directory == repo or repo not in directory.parents:
            break
        directory = directory.parent
    return result


def _sentence_rows(text: str, minimum: int = 0) -> list[tuple[int, str]]:
    rows: list[tuple[int, str]] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        clean = re.sub(r"^\s*(?:[-*+] |\d+[.)] )", "", line).strip()
        for sentence in re.split(r"(?<=[.!?])\s+", clean):
            sentence = sentence.strip()
            if len(re.sub(r"\s+", " ", sentence)) >= minimum:
                rows.append((line_number, sentence))
    return rows


def _identity(repo: Path, subject: Path, artifact: str, text: str) -> list[str]:
    criterion = _criteria(artifact, "identity")
    rel = subject.relative_to(repo).as_posix()
    rows = [f"[{criterion}] path={rel} artifact={artifact}"]
    if artifact == "skill-reference":
        rows.append(f"[{criterion}] domain=out-of-domain (current audit schema has no skill-reference verdict)")
    if artifact == "claude-md":
        role = "root" if subject.parent == repo else "child"
        counts: Counter[str] = Counter()
        try:
            for entry in subject.parent.iterdir():
                if entry.is_file() and entry != subject and entry.suffix.lower() in CODE_EXTENSIONS | DATA_EXTENSIONS:
                    counts[entry.suffix.lower() or "<none>"] += 1
        except OSError:
            pass
        count_text = ", ".join(f"{key}={counts[key]}" for key in sorted(counts)) or "none"
        has_contract = bool(re.search(r"^\s*claude_md\s*:", text, re.M))
        has_skill = (subject.parent / "SKILL.md").is_file()
        dimension = "code-directory" if role == "child" and counts and not has_contract and not has_skill else "classic"
        rows.append(f"[{criterion}] role={role} direct-code/data-files-by-extension: {count_text}")
        rows.append(f"[{criterion}] dimension={dimension}; CD dimension applies={'yes' if dimension == 'code-directory' else 'no'}")
    return rows


def _measurements(text: str, artifact: str) -> list[str]:
    criterion = _criteria(artifact, "measurements")
    raw_lines = text.splitlines()
    effective_lines = len(raw_lines)
    while effective_lines and not raw_lines[effective_lines - 1].strip():
        effective_lines -= 1
    encoded = text.encode("utf-8")
    rows = [
        f"[{criterion}] lines={effective_lines} bytes={len(encoded)} approx_tokens={len(encoded) // 4}",
    ]
    headings = _headings(text)
    for number, level, heading in headings:
        rows.append(f"[{criterion}] heading line={number} level={level} text=\"{_display(heading)}\"")
    boundaries = [(number, heading) for number, _, heading in headings]
    largest = (0, "<body>", effective_lines)
    if boundaries:
        for index, (start, heading) in enumerate(boundaries):
            end = boundaries[index + 1][0] - 1 if index + 1 < len(boundaries) else effective_lines
            size = max(0, end - start + 1)
            if size > largest[2] or largest[1] == "<body>":
                largest = (start, heading, size)
    share = (largest[2] / effective_lines * 100) if effective_lines else 0
    fences = sum(1 for line in raw_lines if re.match(r"^\s*(?:```|~~~)", line)) // 2
    table_lines = [line for line in raw_lines if re.match(r"^\s*\|.*\|\s*$", line)]
    table_separators = sum(1 for line in table_lines if re.match(r"^\s*\|?(?:\s*:?-+:?\s*\|)+", line))
    rows.append(f"[{criterion}] largest-section line={largest[0]} text=\"{_display(largest[1])}\" lines={largest[2]} share={share:.1f}%")
    rows.append(f"[{criterion}] fenced-blocks={fences} tables={table_separators}")
    return rows


def _references(repo: Path, subject: Path, text: str, artifact: str) -> tuple[list[str], list[tuple[int, Path, int]]]:
    criterion = _criteria(artifact, "references")
    rows: list[str] = []
    bad_lines: list[tuple[int, Path, int]] = []
    lines = text.splitlines()

    def add(line_number: int, source_kind: str, token: str, markdown: bool = False) -> None:
        status, resolved, fragment = _resolve_path(repo, subject, token, markdown=markdown)
        kind = "external" if status == "external" else "directory" if status == "directory" else "file"
        anchor = ""
        if fragment and status == "exists" and resolved:
            target_text = _text_file(resolved)
            if target_text is None:
                anchor = " anchor=unreadable"
            else:
                anchor_ok = fragment.lower() in _slug_counts(target_text)
                anchor = f" anchor=#{_display(fragment)}:{'resolves' if anchor_ok else 'missing'}"
        rows.append(
            f"[{criterion}] line={line_number} source={source_kind} token=\"{_display(token)}\" "
            f"status={status} kind={kind} target={_rel(repo, resolved)}{anchor}"
        )

    for line_number, line in enumerate(lines, 1):
        for match in MARKDOWN_LINK.finditer(line):
            add(line_number, "markdown-link", match.group(2), markdown=True)
        for match in BACKTICK.finditer(line):
            token = match.group(1).strip()
            suffix = Path(token.split(":", 1)[0]).suffix.lower()
            if "/" in token or suffix in KNOWN_EXTENSIONS:
                add(line_number, "backtick-path", token)
        for match in PATH_LINE.finditer(line):
            token = f"{match.group(1)}:{match.group(2)}"
            add(line_number, "path-line", token)
            status, resolved, _ = _resolve_path(repo, subject, match.group(1))
            cited = int(match.group(2))
            if status == "exists" and resolved:
                target_lines = (_text_file(resolved) or "").splitlines()
                if 1 <= cited <= len(target_lines):
                    actual = _display(target_lines[cited - 1], 120)
                    rows.append(f"[{criterion}] line={line_number} path-line={_rel(repo, resolved)}:{cited} actual=\"{actual}\"")
                else:
                    bad_lines.append((line_number, resolved, cited))
    if not rows:
        rows.append(f"[{criterion}] none")
    return rows, bad_lines


def _ancestor_rows(repo: Path, subject: Path, text: str, artifact: str) -> list[str]:
    criterion = _criteria(artifact, "ancestors")
    ancestors = _ancestors(repo, subject)
    if not ancestors:
        return [f"[{criterion}] chain=none"]
    rows: list[str] = []
    subject_units = _sentence_rows(text, 60)
    conventions_remaining = 12
    for ancestor in ancestors:
        ancestor_text = _text_file(ancestor) or ""
        ancestor_rel = ancestor.relative_to(repo).as_posix()
        rows.append(f"[{criterion}] ancestor={ancestor_rel} lines={len(ancestor_text.splitlines())}")
        ancestor_units = _sentence_rows(ancestor_text, 60)
        exact: dict[str, tuple[int, str]] = {}
        normalized_ancestors: list[tuple[int, str, str]] = []
        word_index: dict[str, set[int]] = {}
        for line_number, unit in ancestor_units:
            normalized_unit = re.sub(r"\s+", " ", unit).strip().casefold()
            exact.setdefault(normalized_unit, (line_number, unit))
            unit_index = len(normalized_ancestors)
            normalized_ancestors.append((line_number, unit, normalized_unit))
            for word in set(re.findall(r"[a-z0-9_]{5,}", normalized_unit)):
                word_index.setdefault(word, set()).add(unit_index)
        emitted: set[tuple[int, int]] = set()
        duplicate_rows: list[str] = []
        for subject_line, subject_unit in subject_units:
            normalized = re.sub(r"\s+", " ", subject_unit).strip().casefold()
            match = exact.get(normalized)
            score = 1.0 if match else 0.0
            if not match:
                words = sorted(set(re.findall(r"[a-z0-9_]{5,}", normalized)), key=lambda word: (-len(word), word))
                candidate_indexes: set[int] = set()
                for word in words[:3]:
                    candidate_indexes.update(word_index.get(word, ()))
                for candidate_index in sorted(candidate_indexes):
                    ancestor_line, ancestor_unit, other = normalized_ancestors[candidate_index]
                    ratio = len(normalized) / max(1, len(other))
                    if ratio < 0.70 or ratio > 1.43:
                        continue
                    candidate_score = difflib.SequenceMatcher(None, normalized, other, autojunk=False).ratio()
                    if candidate_score >= 0.85 and candidate_score > score:
                        match = (ancestor_line, ancestor_unit)
                        score = candidate_score
            if match and (subject_line, match[0]) not in emitted:
                emitted.add((subject_line, match[0]))
                duplicate_rows.append(
                    f"[{criterion}] DUPLICATE CANDIDATE similarity={score:.2f} "
                    f"subject:{subject_line}=\"{_display(subject_unit)}\" "
                    f"ancestor={ancestor_rel}:{match[0]}=\"{_display(match[1])}\""
                )
        rows.append(f"[{criterion}] DUPLICATE CANDIDATES ancestor={ancestor_rel} count={len(duplicate_rows)}")
        rows.extend(duplicate_rows)
        convention_rows: list[str] = []
        for line_number, line in enumerate(ancestor_text.splitlines(), 1):
            bold_parts = re.findall(r"\*\*(.+?)\*\*", line)
            if bold_parts and MODAL.search(line) and conventions_remaining:
                convention_rows.append(f"[{criterion}] CONVENTION {ancestor_rel}:{line_number}=\"{_display(line)}\"")
                conventions_remaining -= 1
                if not conventions_remaining:
                    break
        rows.append(f"[{criterion}] CONVENTIONS ancestor={ancestor_rel} count={len(convention_rows)} cap-remaining={conventions_remaining}")
        rows.extend(convention_rows)
    return rows


def _paragraph_sentences(text: str) -> list[tuple[int, str]]:
    result: list[tuple[int, str]] = []
    paragraph: list[str] = []
    start = 1

    def flush() -> None:
        nonlocal paragraph
        joined = " ".join(part.strip() for part in paragraph)
        for sentence in re.split(r"(?<=[.!?])\s+", joined):
            if sentence.strip():
                result.append((start, sentence.strip()))
        paragraph = []

    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip() or re.match(r"^\s*(?:```|~~~|#)", line):
            flush()
            start = number + 1
        else:
            if not paragraph:
                start = number
            paragraph.append(line)
    flush()
    return result


def _symbol_forms(token: str) -> list[str]:
    clean = token.strip().strip(".,:;()[]{}")
    callable_match = re.match(r"^([A-Za-z_][A-Za-z0-9_.:]*)\s*\(", token.strip())
    if callable_match:
        clean = callable_match.group(1)
    forms = [clean]
    if "::" in clean:
        forms.append(clean.rsplit("::", 1)[-1])
    if "." in clean and Path(clean).suffix.lower() not in KNOWN_EXTENSIONS:
        forms.append(clean.rsplit(".", 1)[-1])
    return [form for form in dict.fromkeys(forms) if re.match(r"^[A-Za-z_][A-Za-z0-9_.:]*$", form)]


def _claim_rows(repo: Path, subject: Path, text: str, artifact: str) -> list[str]:
    criterion = _criteria(artifact, "claims")
    rows: list[str] = []
    direct_files = _direct_files(subject.parent)
    for line_number, sentence in _paragraph_sentences(text):
        tokens = [match.group(1).strip() for match in BACKTICK.finditer(sentence)]
        if not tokens or not CLAIM_VERBS.search(sentence):
            continue
        named_files: list[Path] = []
        path_states: list[str] = []
        identifiers: list[str] = []
        for token in tokens:
            forms = _symbol_forms(token)
            suffix = Path(token.split(":", 1)[0]).suffix.lower()
            callable_token = bool(re.match(r"^[A-Za-z_][A-Za-z0-9_.:]*\s*\(", token))
            if ("/" in token or suffix in KNOWN_EXTENSIONS) and not callable_token:
                status, resolved, _ = _resolve_path(repo, subject, token)
                path_states.append(f"{_display(token)}={status}:{_rel(repo, resolved)}")
                if status == "exists" and resolved:
                    named_files.append(resolved)
            else:
                identifiers.extend(forms)
        if not path_states and not identifiers:
            continue
        search_files = sorted(set(named_files or direct_files), key=lambda path: str(path))
        matches: list[str] = []
        for candidate in search_files:
            candidate_text = _text_file(candidate)
            if candidate_text is None:
                continue
            for candidate_line, candidate_value in enumerate(candidate_text.splitlines(), 1):
                if any(re.search(rf"(?<![A-Za-z0-9_]){re.escape(symbol)}(?![A-Za-z0-9_])", candidate_value) for symbol in identifiers):
                    matches.append(f"{_rel(repo, candidate)}:{candidate_line}=\"{_display(candidate_value, 120)}\"")
                    if len(matches) == 2:
                        break
            if len(matches) == 2:
                break
        evidence = "; ".join(matches)
        if not evidence and path_states and not identifiers:
            evidence = "PATH STATUS RECORDED" if any("=exists:" in state or "=directory:" in state or "=external:" in state for state in path_states) else "NOT FOUND"
        if not evidence:
            evidence = "NOT FOUND"
        path_note = "; ".join(path_states) if path_states else "no named file; searched direct code files"
        rows.append(
            f"[{criterion}] CLAIM line={line_number} text=\"{_display(sentence)}\" "
            f"targets={path_note} evidence={evidence}"
        )
    if not rows:
        rows.append(f"[{criterion}] no matching claim sentences")
    return rows


def _audit_verdicts(subject: Path) -> list[str]:
    """Run this plugin's own contract audit in-process and keep its verdict lines."""
    try:
        from skills_kit_lib.audit import audit as _audit
        from skills_kit_lib.audit import render_text as _render_text
    except ImportError as error:
        return [f"mechanical-contract-check unavailable: {type(error).__name__}"]
    try:
        report = _audit(subject)
        output = report["error"] if "error" in report else _render_text(report)
    except Exception as error:  # noqa: BLE001 - a broken audit must not sink the pack
        return [f"mechanical-contract-check unavailable: {type(error).__name__}"]
    verdicts = [line.strip() for line in output.splitlines()
                if re.search(r"\[(?:pass|fail|info|warn|n/a)\]", line, re.I)]
    if not verdicts:
        verdicts = ["mechanical-contract-check produced no verdict lines"]
    return verdicts[:20]


def _mechanical(repo: Path, subject: Path, text: str, artifact: str, bad_lines: list[tuple[int, Path, int]]) -> list[str]:
    criterion = _criteria(artifact, "mechanical")
    rows: list[str] = []
    non_ascii = []
    for line_number, line in enumerate(text.splitlines(), 1):
        for char_number, char in enumerate(line, 1):
            if ord(char) > 127:
                non_ascii.append((line_number, char_number, char))
    for line_number, char_number, char in non_ascii[:20]:
        rows.append(f"[{criterion}] non-ascii line={line_number} char={char_number} codepoint=U+{ord(char):04X} value={_ascii(char)}")
    if len(non_ascii) > 20:
        rows.append(f"[{criterion}] non-ascii additional={len(non_ascii) - 20} (cap=20)")
    absolute = re.compile(r"(?<![\w])(?:/home/[^\s`)>\]]+|~/[^\s`)>\]]+|[A-Za-z]:\\[^\s`)>\]]+)")
    for line_number, line in enumerate(text.splitlines(), 1):
        for match in absolute.finditer(line):
            rows.append(f"[{criterion}] machine-path line={line_number} value=\"{_display(match.group(0))}\"")
        hits = sorted({match.group(0).lower() for match in DEIXIS.finditer(line)})
        if hits:
            rows.append(f"[{criterion}] temporal-deixis line={line_number} words={','.join(hits)}")
    for source_line, target, cited in bad_lines:
        target_count = len((_text_file(target) or "").splitlines())
        rows.append(f"[{criterion}] cited-line-out-of-range source-line={source_line} target={_rel(repo, target)}:{cited} target-lines={target_count}")
    trailing = sum(1 for line in text.splitlines() if re.search(r"[ \t]+$", line))
    rows.append(f"[{criterion}] trailing-whitespace-lines={trailing}")
    if artifact in {"skill", "claude-md"}:
        for verdict in _audit_verdicts(subject):
            rows.append(f"[{criterion}] audit.py {_display(verdict, 240)}")
    if not rows:
        rows.append(f"[{criterion}] none")
    return rows


def build_structured(repo_root: str | Path, rel_path: str | Path) -> dict[str, list[str] | str]:
    """Return the full, untruncated structured evidence form."""
    repo = Path(repo_root).resolve()
    rel = Path(rel_path)
    if rel.is_absolute():
        try:
            rel = rel.resolve().relative_to(repo)
        except ValueError as error:
            raise ValueError(f"subject is outside repository: {rel}") from error
    subject = (repo / rel).resolve()
    try:
        subject.relative_to(repo)
    except ValueError as error:
        raise ValueError(f"subject is outside repository: {rel}") from error
    if not subject.is_file():
        raise FileNotFoundError(subject)
    text = subject.read_text(encoding="utf-8")
    artifact = artifact_of(rel)
    reference_rows, bad_lines = _references(repo, subject, text, artifact)
    return {
        "IDENTITY": _identity(repo, subject, artifact, text),
        "MEASUREMENTS": _measurements(text, artifact),
        "REFERENCES": reference_rows,
        "ANCESTORS": _ancestor_rows(repo, subject, text, artifact),
        "CLAIM EVIDENCE": _claim_rows(repo, subject, text, artifact),
        "MECHANICAL": _mechanical(repo, subject, text, artifact, bad_lines),
        "closing": "Evidence rows are facts, not findings. A row with no rule violation is not a finding. Cite rule ids exactly as spelled in the STANDARDS.",
    }



SECTION_ORDER = ("IDENTITY", "MEASUREMENTS", "REFERENCES", "ANCESTORS", "CLAIM EVIDENCE", "MECHANICAL")
TRUNCATION_ORDER = ("MEASUREMENTS", "REFERENCES", "MECHANICAL", "ANCESTORS", "CLAIM EVIDENCE", "IDENTITY")


_ROW_CRITERION = re.compile(r"^\[([^\]]+)\]\s*")
_REFERENCE_STATUS = re.compile(r"^\[([^\]]+)\]\s+line=\d+\s+source=([^ ]+)\s+.*?status=(exists|directory)\s+kind=")


def _compact_references(rows: list[str]) -> list[str]:
    """Collapse resolved reference rows while retaining evidence-bearing rows."""
    counts: dict[str, int] = {}
    external_counts: dict[str, int] = {}
    first_seen: list[str] = []
    retained: list[str] = []
    for row in rows:
        match = _REFERENCE_STATUS.match(row)
        # path:line rows carry a cited target line and must remain individually visible.
        collapsible = match and match.group(2) != "path-line" and "anchor=#" not in row
        if collapsible:
            source_kind = match.group(2)
            if source_kind not in counts:
                first_seen.append(source_kind)
                counts[source_kind] = 0
            counts[source_kind] += 1
        elif match is None:
            external = re.match(r"^\[[^\]]+\]\s+line=\d+\s+source=([^ ]+)\s+.*?status=external\s+", row)
            if external and external.group(1) != "path-line":
                source_kind = external.group(1)
                external_counts[source_kind] = external_counts.get(source_kind, 0) + 1
            else:
                retained.append(row)
        else:
            retained.append(row)
    summaries = [f"resolved: {counts[k]} {k} -> all exist" for k in first_seen]
    summaries.extend(f"external: {external_counts[k]} {k} -> outside repository" for k in external_counts)
    return summaries + retained


def _drop_identical_token(row: str) -> str:
    """Remove a reference token echo when its value is exactly the target path."""
    match = re.search(r' token="([^"\\]*(?:\\.[^"\\]*)*)"', row)
    target = re.search(r" target=([^ ]+)", row)
    if not match or not target or match.group(1) != target.group(1):
        return row
    return row[:match.start()] + row[match.end():]


def _render(
    data: dict[str, list[str] | str],
    truncated: set[str],
    *,
    compact: bool = False,
) -> str:
    blocks = []
    for section in SECTION_ORDER:
        rows = list(data[section])
        criteria = next((match.group(1) for row in rows if (match := _ROW_CRITERION.match(row))), "")
        if compact and section == "REFERENCES":
            rows = _compact_references(rows)
        if section in truncated:
            rows.append(f"[truncated: {section} least-relevant rows omitted]")
        if compact:
            compact_rows = []
            for row in rows:
                if criteria and row.startswith(f"[{criteria}]"):
                    row = row[len(criteria) + 2:].lstrip()
                row = _drop_identical_token(row)
                compact_rows.append(row)
            header = f"[serves: {criteria}]" if criteria else "[serves: none]"
            blocks.append(section + "\n" + header + "\n" + "\n".join(compact_rows))
        else:
            blocks.append(section + "\n" + "\n".join(rows))
    blocks.append(str(data["closing"]))
    return _ascii("\n\n".join(blocks))



def build_pack(
    repo_root: str | Path,
    rel_path: str | Path,
    *,
    compact: bool = True,
    max_chars: int = 24000,
) -> str:
    """Build an ASCII-only evidence pack, deterministically capped at max_chars."""
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    data = build_structured(repo_root, rel_path)
    working = {key: (list(value) if isinstance(value, list) else value) for key, value in data.items()}
    truncated: set[str] = set()
    rendered = _render(working, truncated, compact=compact)
    for section in TRUNCATION_ORDER:
        while len(rendered) > max_chars and working[section]:
            working[section].pop()
            truncated.add(section)
            rendered = _render(working, truncated, compact=compact)
        if len(rendered) <= max_chars:
            return rendered
    if len(rendered) <= max_chars:
        return rendered
    marker = "\n[truncated: pack hard cap reached]"
    if max_chars <= len(marker):
        return marker[:max_chars]
    return rendered[: max_chars - len(marker)] + marker


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a deterministic evidence pack for one Markdown audit subject.",
    )
    parser.add_argument("repo_root", help="repository root directory")
    parser.add_argument("rel_path", help="repository-relative Markdown path")
    parser.add_argument("--max-chars", type=int, default=24000)
    parser.add_argument(
        "--compact",
        dest="compact",
        action="store_true",
        default=True,
        help="use the compact evidence format (the default)",
    )
    parser.add_argument(
        "--no-compact",
        dest="compact",
        action="store_false",
        help="emit every row with its full criterion prefix",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    print(build_pack(args.repo_root, args.rel_path,
                     compact=args.compact, max_chars=args.max_chars))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
