"""Read a data corpus off disk according to the profile's ``source`` documents.

One record is one addressable thing: the file it came from, the identity it
carries, and its mapping of field values. Every diagnostic downstream is
addressed with those three, so they are captured here rather than reconstructed
later.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import Any, Iterator

import yaml

from .errors import ADVISORY, Diagnostic
from .model import Profile, SourceSpec, TypeSpec


@dataclass
class Record:
    """One record of one type, with the address it was read from."""

    type_id: str
    identity: str | None
    data: dict[str, Any]
    file: str
    source: SourceSpec

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
    """Read every source the profile declares, rooted at ``root``."""
    corpus = Corpus(root=root)
    for source in _ordered_sources(profile):
        _load_source(profile, corpus, source, root)
    return corpus


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


def _load_source(profile: Profile, corpus: Corpus, source: SourceSpec, root: Path) -> None:
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
        corpus.records.append(
            Record(type_id=source.of, identity=None, data=document, file=name, source=source)
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
                data=body,
                file=name,
                source=source,
            )
        )


def _keyed_map_records(
    profile: Profile, corpus: Corpus, source: SourceSpec, document: dict, name: str
) -> Iterator[tuple[str, Any]]:
    if source.record_keys is not None:
        wanted = [str(k) for k in source.record_keys]
        for key in wanted:
            if key not in document:
                corpus.diagnostics.append(
                    Diagnostic("declared record key is absent from the document", name, record=key)
                )
                continue
            yield key, document[key]
        return
    if source.record_keys_from is not None:
        wanted = {str(v) for v in resolve_value_set(profile, corpus, source.record_keys_from)}
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
    metadata = {str(k) for k in (source.metadata_keys or [])}
    for key in document:
        if str(key) not in metadata:
            yield str(key), document[key]


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
    if not is_identity:
        check_path_key_steps(profile, target, field_path, path, corpus)
    for record in corpus.of_type(type_id):
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
    target: TypeSpec | None,
    field_path: list[str],
    path: str,
    corpus: Corpus,
) -> None:
    """Walk ``field_path`` over ``target``'s DECLARATIONS (not data), checking
    each map key step this path crosses -- once per path, not once per record.

    A ref key is checked against the referenced type's actual identities,
    which only the corpus (not the loader) can see. A bare 'id' key has no
    declared legal set, so the step resolves the shape unchecked and is
    reported as an ADVISORY -- the same channel an 'open:' field uses. An enum
    key was already checked at load time when its set was inline; a
    'values_from:'-sourced enum key is resolved here through the same corpus
    value-set machinery used for field values.
    """
    if target is None or len(field_path) < 2:
        return
    field = target.every_possible_field().get(field_path[0])
    if field is None:
        return
    consumed = field_path[0]
    for segment in field_path[1:]:
        if field is None:
            return
        if field.kind == "map":
            key_spec = field.key
            if key_spec is not None and key_spec.kind == "id":
                corpus.diagnostics.append(
                    Diagnostic(
                        "steps into map '{0}' by key '{1}', which is a bare 'id' key "
                        "with no declared legal set -- the key is unchecked; declare "
                        "the key set to make it checkable".format(consumed, segment),
                        _profile_file_for(target),
                        field=path,
                        severity=ADVISORY,
                    )
                )
            elif key_spec is not None and key_spec.kind == "ref" and key_spec.to is not None:
                if segment not in corpus.identities(key_spec.to):
                    corpus.diagnostics.append(
                        Diagnostic(
                            "has key '{0}' at map '{1}', which names no record of type "
                            "'{2}'".format(segment, consumed, key_spec.to),
                            _profile_file_for(target),
                            field=path,
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
                    corpus.diagnostics.append(
                        Diagnostic(
                            "has key '{0}' at map '{1}', which is not a member of the "
                            "declared set ({2})".format(segment, consumed, ", ".join(legal)),
                            _profile_file_for(target),
                            field=path,
                        )
                    )
            field = field.value
        elif field.kind == "record":
            field = field.fields.get(segment)
        else:
            return
        consumed = "{0}.{1}".format(consumed, segment)


def _profile_file_for(target: TypeSpec | None) -> str:
    if target is not None and target.document is not None:
        return target.document.name
    return "<profile>"


class _Absent:
    """Sentinel distinguishing 'absent' from a legitimate ``None`` value."""


ABSENT = _Absent()
