'''Parse and evaluate project-independent corpus addresses.'''

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field as dataclass_field
import re
from typing import Any

import yaml

from yaml_data_editor_kit.schema.corpus import (
    ABSENT,
    Corpus,
    Record,
    resolve_value_set,
)
from yaml_data_editor_kit.schema.model import FieldSpec, Profile, SourceSpec, TypeSpec

from .errors import EvaluationError, SelectorError

_MAX_LISTED_VALUES = 12
_GLOB_CHARACTERS = frozenset('*?[]')


@dataclass(frozen=True)
class _Sentinel:
    '''One structural token that cannot collide with a profile name.'''

    text: str

    def __str__(self) -> str:
        return self.text


STAR = _Sentinel('*')
DOC = _Sentinel('@doc')


@dataclass(frozen=True)
class Predicate:
    '''A record filter parsed without evaluating its declared field yet.'''

    field: str
    op: str
    values: tuple[str, ...]


@dataclass(frozen=True)
class Selector:
    '''A normalized selector with its original spelling retained for errors.'''

    text: str = dataclass_field(compare=False)
    type_seg: str | _Sentinel
    record_seg: str | int | _Sentinel | Predicate | None = None
    field_path: tuple[str | _Sentinel, ...] = ()

    @property
    def is_anchor(self) -> bool:
        '''Whether the selector contains no set-denoting syntax.'''
        return (
            self.type_seg is not STAR
            and self.record_seg is not STAR
            and not isinstance(self.record_seg, Predicate)
            and STAR not in self.field_path
        )

    def canonical(self) -> str:
        '''Render the normalized persistent spelling.'''
        parts = [_render_segment(self.type_seg)]
        if self.record_seg is not None:
            parts.append(_render_record_segment(self.record_seg))
        if self.field_path:
            parts.append('.'.join(_render_segment(item) for item in self.field_path))
        return '/'.join(parts)


RowKey = tuple[str, int]


@dataclass(frozen=True)
class Point:
    '''One concrete point in a loaded corpus.'''

    type_id: str
    record: str | RowKey | _Sentinel
    field_path: tuple[str, ...] = ()


@dataclass(frozen=True)
class Evaluation:
    '''The concrete points denoted by one selector in one corpus.'''

    selector: Selector
    points: frozenset[Point]
    matched_records: int


def parse_selector(text: str) -> Selector:
    '''Parse and normalize one selector, or raise ``SelectorError``.'''
    if not isinstance(text, str):
        raise SelectorError('selector {!r}: expected text'.format(text))
    if not text:
        raise SelectorError('selector {!r}: the selector is empty'.format(text))
    if text != text.strip():
        raise SelectorError(
            'selector {!r}: whitespace outside a predicate is not legal'.format(text)
        )

    segments = text.split('/')
    if len(segments) > 3:
        raise SelectorError(
            'selector {!r}: has {} segments; selectors have at most three'.format(
                text, len(segments)
            )
        )
    if any(segment == '' for segment in segments):
        raise SelectorError(
            'selector {!r}: contains an empty slash-separated segment'.format(text)
        )

    type_seg = _parse_type_segment(text, segments[0])
    record_seg: str | int | _Sentinel | Predicate | None = None
    if len(segments) >= 2:
        record_seg = _parse_record_segment(text, segments[1])

    field_path: tuple[str | _Sentinel, ...] = ()
    if len(segments) == 3:
        field_path = _parse_field_path(text, segments[2])
        if field_path and field_path[-1] is STAR:
            field_path = field_path[:-1]

    if record_seg is STAR and not field_path:
        record_seg = None
    return Selector(
        text=text,
        type_seg=type_seg,
        record_seg=record_seg,
        field_path=field_path,
    )


def parse_anchor(text: str) -> Selector:
    '''Parse a selector and enforce the anchor grammar's singular subset.'''
    selector = parse_selector(text)
    raw_segments = text.split('/')
    raw_field_steps = raw_segments[2].split('.') if len(raw_segments) == 3 else []
    offending = next(
        (
            segment
            for segment in [*raw_segments[:2], *raw_field_steps]
            if segment == '*' or segment.startswith('[')
        ),
        None,
    )
    if offending is not None or not selector.is_anchor:
        raise SelectorError(
            'selector {!r}: segment {!r} is not legal in an anchor; an anchor is '
            'singular, and wildcards and predicates denote sets'.format(
                text, offending or '<set expression>'
            )
        )
    return selector


def _parse_type_segment(text: str, segment: str) -> str | _Sentinel:
    if segment == '*':
        return STAR
    _reject_glob(text, segment, 'type')
    _reject_whitespace(text, segment)
    return segment


def _parse_record_segment(
    text: str, segment: str
) -> str | int | _Sentinel | Predicate:
    if segment == '*':
        return STAR
    if segment == '@doc':
        return DOC
    if segment.startswith('#'):
        index = segment[1:]
        if not index.isdecimal():
            raise SelectorError(
                'selector {!r}: segment {!r} must be # followed by a non-negative '
                'integer'.format(text, segment)
            )
        return int(index)
    if segment.startswith('['):
        return _parse_predicate(text, segment)
    _reject_glob(text, segment, 'identity')
    _reject_whitespace(text, segment)
    return segment


def _parse_field_path(text: str, segment: str) -> tuple[str | _Sentinel, ...]:
    raw_steps = segment.split('.')
    if any(step == '' for step in raw_steps):
        raise SelectorError(
            'selector {!r}: field segment {!r} contains an empty path element'.format(
                text, segment
            )
        )
    steps: list[str | _Sentinel] = []
    for step in raw_steps:
        if step == '*':
            steps.append(STAR)
            continue
        _reject_glob(text, step, 'field path')
        _reject_whitespace(text, step)
        steps.append(step)
    return tuple(steps)


def _parse_predicate(text: str, segment: str) -> Predicate:
    if not segment.endswith(']'):
        raise SelectorError(
            'selector {!r}: predicate segment {!r} is missing its closing ]'.format(
                text, segment
            )
        )
    if re.search(r'<=|>=|<|>', segment):
        raise SelectorError(
            'selector {!r}: predicate segment {!r} uses a numeric comparison; '
            'numeric comparison predicates are refused because all numbers are '
            'tunable'.format(text, segment)
        )
    matched = re.fullmatch(
        r'\[\s*([^\s./*?=!<>\[\],]+)\s*(!=|=|\bhas\b|\bin\b)\s*(.*?)\s*\]',
        segment,
    )
    if matched is None:
        raise SelectorError(
            'selector {!r}: predicate segment {!r} is malformed or uses an unknown '
            'operator; operators: [=, !=, has, in]'.format(text, segment)
        )
    field_name, op, raw_values = matched.groups()
    if not raw_values:
        raise SelectorError(
            'selector {!r}: predicate segment {!r} has an empty value'.format(
                text, segment
            )
        )
    if op == 'in':
        values = tuple(value.strip() for value in raw_values.split(','))
        if any(value == '' for value in values):
            raise SelectorError(
                'selector {!r}: predicate segment {!r} has an empty value in its '
                'value list'.format(text, segment)
            )
    else:
        values = (raw_values.strip(),)
        if ',' in raw_values:
            raise SelectorError(
                'selector {!r}: predicate segment {!r} uses a value list with '
                'operator {!r}; only in takes a value list'.format(text, segment, op)
            )
    return Predicate(field=field_name, op=op, values=values)


def _reject_glob(text: str, segment: str, role: str) -> None:
    if any(character in _GLOB_CHARACTERS for character in segment):
        alternative = ''
        if role == 'identity':
            alternative = (
                '; identity globbing is refused; use a predicate over a declared field'
            )
        raise SelectorError(
            'selector {!r}: {} segment {!r} contains glob syntax{}'.format(
                text, role, segment, alternative
            )
        )


def _reject_whitespace(text: str, segment: str) -> None:
    if any(character.isspace() for character in segment):
        raise SelectorError(
            'selector {!r}: segment {!r} contains whitespace'.format(text, segment)
        )


def _render_segment(segment: str | _Sentinel) -> str:
    return segment.text if isinstance(segment, _Sentinel) else segment


def _render_record_segment(
    segment: str | int | _Sentinel | Predicate,
) -> str:
    if isinstance(segment, int):
        return '#{}'.format(segment)
    if isinstance(segment, _Sentinel):
        return segment.text
    if isinstance(segment, Predicate):
        separator = (
            ' {} '.format(segment.op)
            if segment.op in ('has', 'in')
            else segment.op
        )
        return '[{}{}{}]'.format(segment.field, separator, ','.join(segment.values))
    return segment


def evaluate(selector: Selector, profile: Profile, corpus: Corpus) -> Evaluation:
    '''Expand a parsed selector into concrete corpus points.'''
    if selector.type_seg is STAR:
        raise EvaluationError(
            'selector {!r}: type wildcard evaluation belongs to wildcard step 5'.format(
                selector.text
            )
        )
    type_id = selector.type_seg
    type_spec = profile.types.get(type_id)
    if type_spec is None:
        raise EvaluationError(
            'selector {!r}: {!r} names no profile type; types: {}'.format(
                selector.text, type_id, _listed(profile.types)
            )
        )

    records = _select_records(selector, type_spec, profile, corpus)
    if any(step is STAR for step in selector.field_path):
        raise EvaluationError(
            'selector {!r}: field wildcard evaluation belongs to wildcard step 5'.format(
                selector.text
            )
        )

    points: set[Point] = set()
    for record in records:
        field_path = _resolve_field_path(selector, type_spec, record, profile, corpus)
        points.add(
            Point(
                type_id=type_id,
                record=_record_key(record),
                field_path=field_path,
            )
        )
    return Evaluation(
        selector=selector,
        points=frozenset(points),
        matched_records=len(records),
    )


def _select_records(
    selector: Selector,
    type_spec: TypeSpec,
    profile: Profile,
    corpus: Corpus,
) -> list[Record]:
    records = corpus.of_type(type_spec.id)
    segment = selector.record_seg
    if segment is None:
        return records
    if segment is STAR or isinstance(segment, Predicate):
        raise EvaluationError(
            'selector {!r}: record wildcard and predicate evaluation belongs to step 5'.format(
                selector.text
            )
        )
    if segment is DOC:
        return [_document_record(selector, type_spec, profile, records)]
    if isinstance(segment, int):
        return [_ordinal_record(selector, type_spec, profile, corpus, records, segment)]

    matches = [record for record in records if record.identity == segment]
    if not matches:
        raise EvaluationError(
            'selector {!r}: no record of type {!r} has identity {!r}'.format(
                selector.text, type_spec.id, segment
            )
        )
    if len(matches) > 1:
        files = ' and '.join(record.file for record in matches)
        raise EvaluationError(
            'selector {!r}: identity {!r} matches records in {}; duplicate identities '
            'cannot be addressed'.format(selector.text, segment, files)
        )
    return matches


def _document_record(
    selector: Selector,
    type_spec: TypeSpec,
    profile: Profile,
    records: list[Record],
) -> Record:
    has_document_record = type_spec.value is not None or any(
        source.layout == 'single' for source in profile.sources_for(type_spec.id)
    )
    if not has_document_record:
        raise EvaluationError(
            'selector {!r}: type {!r} has no document metadata record; {!r} addresses '
            'a {!r} document or a value-shaped type metadata'.format(
                selector.text, type_spec.id, '@doc', 'single'
            )
        )
    matches = [
        record
        for record in records
        if record.identity is None and record.ordinal is None
    ]
    if len(matches) != 1:
        files = _listed(record.file for record in matches)
        raise EvaluationError(
            'selector {!r}: {!r} resolves to {} document records for type {!r}; '
            'files: {}'.format(
                selector.text, '@doc', len(matches), type_spec.id, files
            )
        )
    return matches[0]


def _ordinal_record(
    selector: Selector,
    type_spec: TypeSpec,
    profile: Profile,
    corpus: Corpus,
    records: list[Record],
    index: int,
) -> Record:
    if type_spec.identified_by is not None:
        raise EvaluationError(
            'selector {!r}: type {!r} is identified by {!r}; {!r} addresses only '
            'identity-less rows'.format(
                selector.text, type_spec.id, type_spec.identified_by, '#INDEX'
            )
        )
    sources = profile.sources_for(type_spec.id)
    if not sources or any(source.layout != 'rows' for source in sources):
        layouts = _listed(source.layout for source in sources)
        raise EvaluationError(
            'selector {!r}: type {!r} has source layouts {}; {!r} addresses only '
            'identity-less rows'.format(selector.text, type_spec.id, layouts, '#INDEX')
        )
    files = sorted(
        {record.file for record in records} or {source.path for source in sources}
    )
    if len(files) != 1:
        raise EvaluationError(
            'selector {!r}: rows of type {!r} live in {} files; a bare {!r} is '
            'ambiguous'.format(selector.text, type_spec.id, len(files), '#INDEX')
        )
    matches = [record for record in records if record.ordinal == index]
    if not matches:
        row_count = _row_count(corpus, sources[0], records)
        raise EvaluationError(
            'selector {!r}: {!r}: {} has {} rows'.format(
                selector.text, '#{}'.format(index), files[0], row_count
            )
        )
    if len(matches) > 1:
        raise EvaluationError(
            'selector {!r}: {!r} matches more than one row; the ordinal is ambiguous'.format(
                selector.text, '#{}'.format(index)
            )
        )
    return matches[0]


def _row_count(corpus: Corpus, source: SourceSpec, records: list[Record]) -> int:
    path = corpus.root / source.path
    try:
        document = yaml.safe_load(path.read_text(encoding='utf-8'))
        rows = (
            document.get(source.key)
            if source.key is not None and isinstance(document, dict)
            else document
        )
        if isinstance(rows, list):
            return len(rows)
    except (OSError, ValueError, yaml.YAMLError):
        pass
    ordinals = [record.ordinal for record in records if record.ordinal is not None]
    return max(ordinals, default=-1) + 1


def _record_key(record: Record) -> str | RowKey | _Sentinel:
    if record.identity is not None:
        return record.identity
    if record.ordinal is not None:
        return (record.file, record.ordinal)
    return DOC


def _listed(values: Iterable[Any]) -> str:
    rendered = sorted(str(value) for value in values)
    if len(rendered) > _MAX_LISTED_VALUES:
        rendered = rendered[:_MAX_LISTED_VALUES] + ['...']
    return '[{}]'.format(', '.join(rendered))


def _resolve_field_path(
    selector: Selector,
    type_spec: TypeSpec,
    record: Record,
    profile: Profile,
    corpus: Corpus,
) -> tuple[str, ...]:
    if not selector.field_path:
        return ()
    path = tuple(str(segment) for segment in selector.field_path)

    if type_spec.value is not None and record.identity is not None:
        _walk_value_shape(selector, type_spec, record, path, profile, corpus)
        return path

    if selector.record_seg is DOC:
        fields = type_spec.fields
    else:
        discriminator_value: Any = None
        if type_spec.variants is not None and isinstance(record.data, dict):
            discriminator_value = record.data.get(type_spec.variants.on)
        fields = type_spec.fields_for(discriminator_value)
    _walk_named_fields(
        selector,
        type_spec,
        record,
        fields,
        path,
        profile,
        corpus,
    )
    return path


def _walk_named_fields(
    selector: Selector,
    type_spec: TypeSpec,
    record: Record,
    fields: dict[str, FieldSpec],
    path: tuple[str, ...],
    profile: Profile,
    corpus: Corpus,
) -> Any:
    first = path[0]
    field_spec = fields.get(first)
    if field_spec is None:
        _raise_unknown_first_field(selector, type_spec, record, fields, first)
    value = record.data.get(first, ABSENT) if isinstance(record.data, dict) else ABSENT
    return _walk_field_tail(
        selector,
        field_spec,
        value,
        path,
        1,
        profile,
        corpus,
    )


def _walk_value_shape(
    selector: Selector,
    type_spec: TypeSpec,
    record: Record,
    path: tuple[str, ...],
    profile: Profile,
    corpus: Corpus,
) -> Any:
    value_spec = type_spec.value
    if value_spec is None:
        raise RuntimeError('value-shaped walk requested for a regular type')
    if path[0] in type_spec.fields:
        hint = '{}/@doc/{}'.format(type_spec.id, path[0])
        raise EvaluationError(
            'selector {!r}: {!r} is not in the value declaration of value-shaped '
            'type {!r}; it is a metadata field -- address it as {!r}'.format(
                selector.text, path[0], type_spec.id, hint
            )
        )
    return _walk_field_tail(
        selector,
        value_spec,
        record.data,
        path,
        0,
        profile,
        corpus,
    )


def _walk_field_tail(
    selector: Selector,
    field_spec: FieldSpec,
    value: Any,
    path: tuple[str, ...],
    consumed: int,
    profile: Profile,
    corpus: Corpus,
) -> Any:
    current_spec = field_spec
    current_value = value
    for index in range(consumed, len(path)):
        segment = path[index]
        current_spec, data_key = _step_into_field(
            selector,
            current_spec,
            segment,
            index,
            profile,
            corpus,
        )
        if current_value is ABSENT:
            continue
        if not isinstance(current_value, dict):
            current_value = ABSENT
            continue
        current_value = current_value.get(data_key, ABSENT)
    return current_value


def _step_into_field(
    selector: Selector,
    field_spec: FieldSpec,
    segment: str,
    consumed: int,
    profile: Profile,
    corpus: Corpus,
) -> tuple[FieldSpec, Any]:
    unit = _selector_prefix(selector, consumed)
    if field_spec.kind == 'list':
        raise EvaluationError(
            'selector {!r}: a selector cannot step into a list; {!r} denotes the '
            'complete list'.format(selector.text, unit)
        )
    if field_spec.kind == 'ref':
        raise EvaluationError(
            'selector {!r}: cannot continue with segment {!r} through ref {!r}; '
            'reaching through a ref is a join'.format(selector.text, segment, unit)
        )
    if field_spec.shape_from is not None:
        raise EvaluationError(
            'selector {!r}: cannot continue with segment {!r} through {!r}, whose '
            'shape comes from shape_from and is unavailable at address evaluation'.format(
                selector.text, segment, unit
            )
        )
    if field_spec.kind == 'record':
        next_field = field_spec.fields.get(segment)
        if next_field is None:
            raise EvaluationError(
                'selector {!r}: {!r} does not resolve against the record fields at '
                '{!r}; fields: {}'.format(
                    selector.text, segment, unit, _listed(field_spec.fields)
                )
            )
        return next_field, segment
    if field_spec.kind == 'map':
        data_key = _map_key(selector, field_spec, segment, unit, profile, corpus)
        if field_spec.value is None:
            raise EvaluationError(
                'selector {!r}: map at {!r} declares no value shape'.format(
                    selector.text, unit
                )
            )
        return field_spec.value, data_key
    raise EvaluationError(
        'selector {!r}: cannot continue with segment {!r} past {!r}, a {!r}; only '
        'a record or map can be stepped into'.format(
            selector.text, segment, unit, field_spec.kind or 'shape'
        )
    )


def _raise_unknown_first_field(
    selector: Selector,
    type_spec: TypeSpec,
    record: Record,
    fields: dict[str, FieldSpec],
    segment: str,
) -> None:
    if (
        type_spec.variants is not None
        and segment in type_spec.every_possible_field()
        and segment not in fields
    ):
        discriminator = type_spec.variants.on
        value = (
            record.data.get(discriminator)
            if isinstance(record.data, dict)
            else ABSENT
        )
        raise EvaluationError(
            'selector {!r}: variant field {!r} is not declared when discriminator '
            '{!r} is {!r}; fields for this record: {}'.format(
                selector.text, segment, discriminator, value, _listed(fields)
            )
        )
    target = 'fields of type {!r}'.format(type_spec.id)
    if selector.record_seg is DOC and type_spec.value is not None:
        target = 'metadata fields of value-shaped type {!r}'.format(type_spec.id)
    raise EvaluationError(
        'selector {!r}: {!r} does not resolve against the {}; fields: {}'.format(
            selector.text, segment, target, _listed(fields)
        )
    )


def _map_key(
    selector: Selector,
    map_spec: FieldSpec,
    segment: str,
    unit: str,
    profile: Profile,
    corpus: Corpus,
) -> Any:
    key_spec = map_spec.key
    if key_spec is None:
        raise EvaluationError(
            'selector {!r}: map at {!r} declares no key shape'.format(
                selector.text, unit
            )
        )
    if key_spec.kind == 'id':
        raise EvaluationError(
            'selector {!r}: steps into a map whose key is a bare {!r} with no '
            'declared legal set; the addressable unit is {!r}; declare the key set '
            '(values_from or an enum) to address inside it'.format(
                selector.text, 'id', unit
            )
        )
    if key_spec.kind == 'ref':
        legal: list[Any] = corpus.identities(key_spec.to or '')
        candidate: Any = segment
    elif key_spec.kind == 'enum':
        legal = (
            resolve_value_set(profile, corpus, key_spec.values_from)
            if key_spec.values_from is not None
            else key_spec.enum_members
        )
        candidate = _coerce_map_key(selector, segment, key_spec)
        if key_spec.stored != 'int':
            legal = [str(value) for value in legal]
    else:
        raise EvaluationError(
            'selector {!r}: map at {!r} has unsupported key declaration {!r}'.format(
                selector.text, unit, key_spec.kind
            )
        )
    if candidate not in legal:
        raise EvaluationError(
            'selector {!r}: key {!r} at map {!r} is not a member of the declared '
            'set {}'.format(selector.text, segment, unit, _listed(legal))
        )
    return candidate


def _coerce_map_key(selector: Selector, segment: str, key_spec: FieldSpec) -> Any:
    if key_spec.stored != 'int':
        return segment
    try:
        return int(segment)
    except ValueError as exc:
        raise EvaluationError(
            'selector {!r}: map key {!r} is not coercible to the declared stored '
            'integer enum'.format(selector.text, segment)
        ) from exc


def _selector_prefix(selector: Selector, consumed: int) -> str:
    prefix = Selector(
        text=selector.text,
        type_seg=selector.type_seg,
        record_seg=selector.record_seg,
        field_path=selector.field_path[:consumed],
    )
    return prefix.canonical()
