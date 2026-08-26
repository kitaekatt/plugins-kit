"""Read a data corpus off disk according to the profile's ``source`` documents.

One record is one addressable thing: the file it came from, the identity it
carries, and its data value. Every diagnostic downstream is
addressed with those three, so they are captured here rather than reconstructed
later.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
import fnmatch
from pathlib import Path
from typing import Any, Iterator

import yaml

from .errors import ADVISORY, Diagnostic
from .model import PathWalk, Profile, SourceSpec, TypeSpec


@dataclass
class Record:
    """One record of one type, with the address it was read from."""

    type_id: str
    identity: str | None
    ordinal: int | None
    data: Any
    file: str
    source: SourceSpec
    excluded_keys: frozenset[str] = dataclass_field(default_factory=frozenset)

    @property
    def label(self) -> str:
        """What a diagnostic calls this record."""
        return self.identity if self.identity is not None else "<the document>"


@dataclass
class Corpus:
    """Every record read, indexed by type."""

    root: Path
    records: list[Record] = dataclass_field(default_factory=list)
    diagnostics: list[Diagnostic] = dataclass_field(default_factory=list)
    _path_key_steps_checked: bool = dataclass_field(
        default=False,
        init=False,
        repr=False,
    )

    def of_type(self, type_id: str) -> list[Record]:
        return [r for r in self.records if r.type_id == type_id]

    def identities(self, type_id: str) -> list[str]:
        return [r.identity for r in self.of_type(type_id) if r.identity is not None]

    def find(self, type_id: str, identity: Any) -> Record | None:
        for record in self.of_type(type_id):
            if record.identity == identity:
                return record
        return None


def load_corpus(profile: Profile, root: Path) -> Corpus:
    """Read every source, then check corpus-backed obligations of all paths."""
    corpus = Corpus(root=root)
    single_claims = _precompute_single_claims(profile)
    for _claimed, diagnostics in single_claims.values():
        corpus.diagnostics.extend(diagnostics)
    for source in _ordered_sources(profile):
        _load_source(profile, corpus, source, root, single_claims)
    check_path_key_steps(profile, corpus)
    return corpus


def claiming_source(profile: Profile, source: SourceSpec, key: str) -> SourceSpec | None:
    """The other source on ``source``'s path that claims top-level key
    ``key``, if any.

    Only a ``rows`` source with ``key:`` claims a SPECIFIC key (see
    ``_precompute_single_claims``) -- every other coexisting layout claims
    the whole document, which refuses coexistence outright rather than
    reaching this record with a per-key exclusion. Used to build an
    actionable message when an address steps into a key a ``single`` record
    excluded, so the reader is pointed at the record that actually owns it.
    """
    for other in profile.sources:
        if other is source or other.path != source.path:
            continue
        if other.layout == "rows" and other.key == key:
            return other
    return None


def _shares_this_path(source: SourceSpec, path: str) -> bool:
    """Whether ``source`` occupies file ``path`` -- literally for every
    other layout, or by GLOB EXPANSION for ``file_per_record``, whose own
    ``path:`` is a pattern rather than a literal file. A ``file_per_record``
    source that never literally spells ``path`` can still claim it whole by
    matching it: ``content/*.yaml`` reads ``content/manifest.yaml`` as one
    of its own records exactly as surely as a pattern that names it
    verbatim, so treating only an exact string match as "sharing" the path
    missed that case entirely -- the file loaded twice, silently, under two
    type ids, with no coexistence refusal at all.
    """
    if source.layout != "file_per_record":
        return source.path == path
    return _file_per_record_matches(source.path, path)


def _file_per_record_matches(pattern: str, path: str) -> bool:
    """Whether the glob ``pattern`` (as passed to ``Path.glob``) would match
    literal file ``path``, without touching the filesystem.

    Matches ``pathlib.Path.glob`` semantics for the patterns this dialect
    actually uses: each ``/``-separated segment matches independently, so a
    bare ``*`` does not cross a directory boundary -- which is why this is
    not a single ``fnmatch`` over the whole string (that would let
    ``content/*.yaml`` wrongly match ``content/sub/manifest.yaml``). A
    recursive ``**`` segment is rare in this corpus and not worth
    replicating exactly; it falls back to a conservative single-``*``
    translation rather than silently failing to match.
    """
    pattern_segments = pattern.split("/")
    path_segments = path.split("/")
    if "**" in pattern_segments:
        return fnmatch.fnmatch(path, pattern.replace("**/", "").replace("**", "*"))
    if len(pattern_segments) != len(path_segments):
        return False
    return all(
        fnmatch.fnmatch(path_segment, pattern_segment)
        for path_segment, pattern_segment in zip(path_segments, pattern_segments)
    )


def _precompute_single_claims(
    profile: Profile,
) -> dict[str, tuple[set[str], list[Diagnostic]]]:
    """For each file path a 'single' source names, the top-level keys another
    source on that same path claims, plus coexistence diagnostics.

    ``single`` means "the whole document is one record" only for the keys no
    OTHER source addresses -- a ``rows`` source with ``key:`` on the same
    path unambiguously owns that one key, its own layout says so, so
    ``single`` must not also read it as a field of its own record.

    Coexistence works only when the other source's claim is a well-defined
    PROPER SUBSET of the document. Four things instead claim the WHOLE
    document, and none of them can share a file with ``single`` at all: a
    ``rows`` source with no ``key:`` (it IS the document's sequence), a
    ``keyed_map`` source (every top-level key is either one of its records or
    its metadata -- there is no third region left for ``single``), a
    ``file_per_record`` source whose glob names this exact file (it reads the
    whole matched file as one record, the same whole-document claim a
    key-less ``rows`` source makes), and a second ``single`` source (by
    definition, the whole document again).

    Two ``rows`` sources both naming the same ``key:`` is the same "one key,
    two owners" problem restated one level down, and is refused the same
    way -- but ONLY on a path a ``single`` source also occupies, because that
    is the only case this function inspects; two ``rows`` sources sharing a
    key on a path with no ``single`` source at all is a real, separate
    defect (each would silently load the same rows twice, under two type
    ids) that this ruling does not cover.

    Computed once, before any source loads, so a path shared by several
    sources -- including two ``single`` sources, which would otherwise each
    discover and report the same conflict -- is diagnosed exactly once.
    """
    single_paths = sorted({s.path for s in profile.sources if s.layout == "single"})
    result: dict[str, tuple[set[str], list[Diagnostic]]] = {}
    for path in single_paths:
        siblings = [s for s in profile.sources if _shares_this_path(s, path)]
        singles = [s for s in siblings if s.layout == "single"]
        claimed: dict[str, SourceSpec] = {}
        diagnostics: list[Diagnostic] = []
        for other in siblings:
            if other.layout == "single":
                continue
            if other.layout == "rows" and other.key is not None:
                key = other.key
                first = claimed.get(key)
                if first is not None:
                    diagnostics.append(
                        Diagnostic(
                            "key '{0}' of this file is claimed by two 'rows' "
                            "sources -- one for type '{1}' and one for type "
                            "'{2}'; one top-level key cannot have two "
                            "owners".format(key, first.of, other.of),
                            path,
                            record=key,
                        )
                    )
                    continue
                claimed[key] = other
                continue
            # A 'rows' source with no 'key:', a 'keyed_map' source, or a
            # 'file_per_record' source whose glob names this exact file --
            # each claims the whole document, so none can share a file with
            # 'single' at all.
            if other.layout == "rows":
                reason = "no 'key:', so it IS the document's sequence -- the whole file"
            elif other.layout == "keyed_map":
                reason = (
                    "every top-level key is either one of its records or "
                    "its metadata, leaving no third region for 'single'"
                )
            else:
                reason = (
                    "its 'path:' glob matches this exact file, and it reads "
                    "every matched file as one whole record"
                )
            diagnostics.append(
                Diagnostic(
                    "a 'single' source for type '{0}' shares this file with a "
                    "'{1}' source for type '{2}': {3}; a 'single' source can "
                    "only coexist with a 'rows' source that names a specific "
                    "'key:'".format(
                        ", ".join(sorted(s.of for s in singles)),
                        other.layout,
                        other.of,
                        reason,
                    ),
                    path,
                )
            )
        if len(singles) > 1:
            names = ", ".join(sorted(s.of for s in singles))
            diagnostics.append(
                Diagnostic(
                    "two 'single' sources both claim this whole file: types "
                    "{0}; a 'single' source IS the whole document, so a "
                    "second one on the same path has nothing left to "
                    "be".format(names),
                    path,
                )
            )
        result[path] = (set(claimed), diagnostics)
    return result


def _ordered_sources(profile: Profile) -> list[SourceSpec]:
    """Sources whose record keys come from another type are read last.

    ``record_keys_from:`` needs that other type's ids to exist already.
    """
    plain = [s for s in profile.sources if s.record_keys_from is None]
    deferred = [s for s in profile.sources if s.record_keys_from is not None]
    return plain + deferred


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name


def _read(path: Path, root: Path, diagnostics: list[Diagnostic]) -> Any:
    name = _relative(path, root)
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        diagnostics.append(Diagnostic("cannot be read: {0}".format(exc.strerror), name))
    except yaml.YAMLError as exc:
        diagnostics.append(Diagnostic("is not valid YAML: {0}".format(exc), name))
    return None


def _matching_files(root: Path, pattern: str) -> list[Path]:
    return sorted(p for p in root.glob(pattern) if p.is_file())


def _load_source(
    profile: Profile,
    corpus: Corpus,
    source: SourceSpec,
    root: Path,
    single_claims: dict[str, tuple[set[str], list[Diagnostic]]],
) -> None:
    type_spec = profile.types[source.of]
    identified_by = type_spec.identified_by

    if source.layout == "file_per_record":
        files = _matching_files(root, source.path)
        if not files:
            corpus.diagnostics.append(
                Diagnostic(
                    "no file matches the declared path for type '{0}'".format(source.of),
                    source.path,
                )
            )
        for path in files:
            _load_file_per_record(corpus, source, path, root, identified_by)
        return

    path = root / source.path
    if not path.is_file():
        corpus.diagnostics.append(
            Diagnostic(
                "declared source for type '{0}' does not exist".format(source.of), source.path
            )
        )
        return
    name = _relative(path, root)
    document = _read(path, root, corpus.diagnostics)
    if document is None:
        return

    if source.layout == "rows":
        _load_rows(corpus, source, document, name, identified_by)
    elif source.layout == "keyed_map":
        _load_keyed_map(profile, corpus, source, document, name)
    elif source.layout == "single":
        if not isinstance(document, dict):
            corpus.diagnostics.append(
                Diagnostic("a 'single' source must be a mapping of that record's fields", name)
            )
            return
        claimed_keys, _diagnostics = single_claims.get(source.path, (set(), []))
        record_data = {
            key: value for key, value in document.items() if str(key) not in claimed_keys
        }
        corpus.records.append(
            Record(
                type_id=source.of,
                identity=None,
                ordinal=None,
                data=record_data,
                file=name,
                source=source,
                excluded_keys=frozenset(claimed_keys),
            )
        )


def _load_rows(
    corpus: Corpus,
    source: SourceSpec,
    document: Any,
    name: str,
    identified_by: str | None,
) -> None:
    if source.key is not None:
        if not isinstance(document, dict):
            corpus.diagnostics.append(
                Diagnostic(
                    "a 'rows' source with 'key: {0}' must be a mapping".format(source.key), name
                )
            )
            return
        if source.key not in document:
            corpus.diagnostics.append(
                Diagnostic("has no containing key '{0}'".format(source.key), name)
            )
            return
        rows = document[source.key]
    else:
        rows = document
    if not isinstance(rows, list):
        corpus.diagnostics.append(
            Diagnostic("a 'rows' source must hold a list of records", name)
        )
        return
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            corpus.diagnostics.append(
                Diagnostic("row #{0} is not a mapping".format(index), name)
            )
            continue
        identity = row.get(identified_by) if identified_by else None
        corpus.records.append(
            Record(
                type_id=source.of,
                identity=None if identity is None else str(identity),
                ordinal=index,
                data=row,
                file=name,
                source=source,
            )
        )


def _load_file_per_record(
    corpus: Corpus, source: SourceSpec, path: Path, root: Path, identified_by: str | None
) -> None:
    name = _relative(path, root)
    document = _read(path, root, corpus.diagnostics)
    if document is None:
        return
    if not isinstance(document, dict):
        corpus.diagnostics.append(
            Diagnostic("a 'file_per_record' source must be a mapping", name)
        )
        return
    identity = document.get(identified_by) if identified_by else None
    identity_text = None if identity is None else str(identity)
    if identity_text is not None and identity_text != path.stem:
        corpus.diagnostics.append(
            Diagnostic(
                "the filename says '{0}' but the record's identity says '{1}'; the "
                "filename is a second expression of the identity, not a second source "
                "of it".format(path.stem, identity_text),
                name,
                record=identity_text,
                field=identified_by,
            )
        )
    corpus.records.append(
        Record(
            type_id=source.of,
            identity=identity_text,
            ordinal=None,
            data=document,
            file=name,
            source=source,
        )
    )


def _load_keyed_map(
    profile: Profile, corpus: Corpus, source: SourceSpec, document: Any, name: str
) -> None:
    if not isinstance(document, dict):
        corpus.diagnostics.append(
            Diagnostic("a 'keyed_map' source must be a mapping", name)
        )
        return
    target = profile.types[source.of]
    if target.value is not None:
        record_items = list(
            _keyed_map_records(profile, corpus, source, document, name)
        )
        present_record_keys = {key for key, _ in record_items}
        for key, body in record_items:
            corpus.records.append(
                Record(
                    type_id=source.of,
                    identity=str(key),
                    ordinal=None,
                    data=body,
                    file=name,
                    source=source,
                )
            )
        metadata = {
            str(key): value
            for key, value in document.items()
            if str(key) in target.fields and str(key) not in present_record_keys
        }
        corpus.records.append(
            Record(
                type_id=source.of,
                identity=None,
                ordinal=None,
                data=metadata,
                file=name,
                source=source,
            )
        )
        return
    for key, body in _keyed_map_records(profile, corpus, source, document, name):
        if not isinstance(body, dict):
            corpus.diagnostics.append(
                Diagnostic("is not a mapping of that record's fields", name, record=str(key))
            )
            continue
        corpus.records.append(
            Record(
                type_id=source.of,
                identity=str(key),
                ordinal=None,
                data=body,
                file=name,
                source=source,
            )
        )


def _keyed_map_records(
    profile: Profile,
    corpus: Corpus,
    source: SourceSpec,
    document: dict[Any, Any],
    name: str,
) -> Iterator[tuple[str, Any]]:
    target = profile.types[source.of]
    if target.value is None:
        metadata = {str(key) for key in (source.metadata_keys or [])}
        metadata_label = "declared metadata keys"
    else:
        metadata = set(target.fields)
        metadata_label = "fields of value-shaped type '{0}'".format(target.id)

    if source.record_keys is not None:
        wanted = [str(key) for key in source.record_keys]
        wanted_set = set(wanted)
        record_keys_label = "declared record keys"
        _report_unknown_keyed_map_keys(
            corpus,
            document,
            name,
            wanted_set,
            metadata,
            record_keys_label,
            metadata_label,
        )
        if target.value is not None:
            _report_value_shaped_metadata_collisions(
                corpus, name, target, wanted_set, metadata, record_keys_label
            )
        for key in wanted:
            if key not in document:
                corpus.diagnostics.append(
                    Diagnostic("declared record key is absent from the document", name, record=key)
                )
                continue
            yield key, document[key]
        return
    if source.record_keys_from is not None:
        wanted = {
            str(value)
            for value in resolve_value_set(
                profile, corpus, source.record_keys_from
            )
        }
        record_keys_label = "declared record keys from '{0}'".format(
            source.record_keys_from
        )
        _report_unknown_keyed_map_keys(
            corpus,
            document,
            name,
            wanted,
            metadata,
            record_keys_label,
            metadata_label,
        )
        if target.value is not None:
            _report_value_shaped_metadata_collisions(
                corpus, name, target, wanted, metadata, record_keys_label
            )
        for key in document:
            if str(key) in wanted:
                yield str(key), document[key]
        for key in sorted(wanted):
            if key not in {str(k) for k in document}:
                corpus.diagnostics.append(
                    Diagnostic(
                        "'record_keys_from: {0}' names this record, but the document has "
                        "no such key".format(source.record_keys_from),
                        name,
                        record=key,
                    )
                )
        return
    for key in document:
        if str(key) not in metadata:
            yield str(key), document[key]


def _report_value_shaped_metadata_collisions(
    corpus: Corpus,
    name: str,
    target: TypeSpec,
    record_keys: set[str],
    metadata_keys: set[str],
    record_keys_label: str,
) -> None:
    for key in sorted(record_keys & metadata_keys):
        corpus.diagnostics.append(
            Diagnostic(
                "metadata field '{0}' of value-shaped type '{1}' is also in "
                "{2}; one top-level key cannot be both a record and "
                "metadata".format(key, target.id, record_keys_label),
                name,
                record=key,
            )
        )


def _report_unknown_keyed_map_keys(
    corpus: Corpus,
    document: dict[Any, Any],
    name: str,
    record_keys: set[str],
    metadata_keys: set[str],
    record_keys_label: str,
    metadata_keys_label: str,
) -> None:
    """Refuse top-level keys outside both declared ``keyed_map`` sets."""
    unknown = sorted(
        {str(key) for key in document} - record_keys - metadata_keys
    )
    record_keys_text = ", ".join(sorted(record_keys))
    metadata_keys_text = ", ".join(sorted(metadata_keys))
    for key in unknown:
        corpus.diagnostics.append(
            Diagnostic(
                "unknown keyed_map key '{0}'; {1}: [{2}]; {3}: [{4}]".format(
                    key,
                    record_keys_label,
                    record_keys_text,
                    metadata_keys_label,
                    metadata_keys_text,
                ),
                name,
                record=key,
            )
        )


def resolve_value_set(profile: Profile, corpus: Corpus, path: str) -> list[Any]:
    """The legal-value set a ``<type>.<segment>[.<segment>]*`` path names, in
    corpus order.

    Two forms, and which one applies is decided by the data rather than by a
    second declaration: a field holding a list of scalars contributes each of
    its members, and any other field contributes its own value. An id set is
    the second form applied to the type's ``identified_by`` field, so both
    ``values_from:`` shapes the spec names go through here.

    Order is preserved (and duplicates are kept) because ``ordered:`` makes
    position load-bearing for exactly the list-of-scalars form, and because a
    ``unique`` constraint cannot see a duplicate a set has already discarded.
    """
    out: list[Any] = []
    type_id, *field_path = path.split(".")
    target = profile.types.get(type_id)
    is_identity = target is not None and field_path == [target.identified_by]
    for record in corpus.of_type(type_id):
        if (
            target is not None
            and target.value is not None
            and record.identity is not None
        ):
            continue
        if is_identity and field_path[0] not in record.data and record.identity is not None:
            # A keyed_map carries the identity as the document key, not as a
            # field of the record body.
            out.append(record.identity)
            continue
        value = resolve_path_value(target, record.data, field_path)
        if value is ABSENT:
            continue
        if isinstance(value, list):
            out.extend(value)
        else:
            out.append(value)
    return out


def resolve_path_value(type_spec: TypeSpec | None, data: Any, field_path: list[str]) -> Any:
    """Walk ``field_path`` through one record's ``data``, guided by
    ``type_spec``'s declared shape so a map step (keyed by a literal path
    segment) is told apart from a record step (a named field). Returns
    ``ABSENT`` where the walk cannot reach a value.
    """
    if type_spec is None or not field_path or not isinstance(data, dict):
        return ABSENT
    field_spec = type_spec.every_possible_field().get(field_path[0])
    if field_spec is None:
        return ABSENT
    value = data.get(field_path[0], ABSENT)
    for segment in field_path[1:]:
        if not isinstance(value, dict):
            return ABSENT
        if field_spec.kind == "map":
            field_spec = field_spec.value
        elif field_spec.kind == "record":
            field_spec = field_spec.fields.get(segment)
        else:
            return ABSENT
        if field_spec is None:
            return ABSENT
        value = value.get(segment, ABSENT)
    return value


def check_path_key_steps(
    profile: Profile,
    corpus: Corpus,
) -> None:
    """Check every map-key step recorded by the loader -- once per declared
    path use, never once per record that happens to hold a related field.

    A ref key is checked against the referenced type's actual identities,
    which only the corpus (not the loader) can see. A bare 'id' key has no
    declared legal set, so the step resolves the shape unchecked and is
    reported as an ADVISORY -- the same channel an 'open:' field uses. An enum
    key was already checked at load time when its set was inline; a
    'values_from:'-sourced enum key is resolved here through the same corpus
    value-set machinery used for field values.
    """
    if corpus._path_key_steps_checked:
        return

    for walked in profile.path_walks:
        target = profile.types.get(walked.type_id)
        for step in walked.key_steps:
            segment = step.segment
            key_spec = step.key_spec
            if key_spec is not None and key_spec.kind == "id":
                corpus.diagnostics.append(
                    Diagnostic(
                        "steps into map '{0}' by key '{1}', which is a bare 'id' key "
                        "with no declared legal set -- the key is unchecked; declare "
                        "the key set to make it checkable".format(step.map_path, segment),
                        _profile_file_for(walked, target),
                        field=walked.path,
                        severity=ADVISORY,
                    )
                )
            elif key_spec is not None and key_spec.kind == "ref" and key_spec.to is not None:
                if segment not in corpus.identities(key_spec.to):
                    corpus.diagnostics.append(
                        Diagnostic(
                            "has key '{0}' at map '{1}', which names no record of type "
                            "'{2}'".format(segment, step.map_path, key_spec.to),
                            _profile_file_for(walked, target),
                            field=walked.path,
                        )
                    )
            elif (
                key_spec is not None
                and key_spec.kind == "enum"
                and key_spec.values_from is not None
            ):
                legal = [
                    str(value)
                    for value in resolve_value_set(profile, corpus, key_spec.values_from)
                ]
                if segment not in legal:
                    if not legal:
                        message = (
                            "has key '{0}' at map '{1}', but the declared set is empty "
                            "and admits no map key"
                        ).format(segment, step.map_path)
                    else:
                        message = (
                            "has key '{0}' at map '{1}', which is not a member of the "
                            "declared set ({2})"
                        ).format(segment, step.map_path, ", ".join(legal))
                    corpus.diagnostics.append(
                        Diagnostic(
                            message,
                            _profile_file_for(walked, target),
                            field=walked.path,
                        )
                    )
    corpus._path_key_steps_checked = True


def _profile_file_for(walked: PathWalk, target: TypeSpec | None) -> str:
    if walked.document is not None:
        return walked.document.name
    if target is not None and target.document is not None:
        return target.document.name
    return "<profile>"


class _Absent:
    """Sentinel distinguishing 'absent' from a legitimate ``None`` value."""


ABSENT = _Absent()
