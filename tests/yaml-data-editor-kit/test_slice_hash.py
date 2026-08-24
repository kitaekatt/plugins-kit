'''Anchor slice definitions and deterministic canonical hashing.'''

from pathlib import Path
from typing import Callable

import pytest

from yaml_data_editor_kit.comments import (
    DOC,
    EvaluationError,
    Point,
    parse_anchor,
    resolve_anchor,
    slice_hash,
)
from yaml_data_editor_kit.comments.hashing import canonical_bytes
from yaml_data_editor_kit.schema import Corpus, Profile, load_corpus, load_profile
from yaml_data_editor_kit.schema.corpus import ABSENT

Writer = Callable[[str, str], Path]


def _catalogue(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> tuple[Profile, Corpus]:
    write(
        'profile/catalogue.yaml',
        '''
dialect: type/1
id: product
identified_by: id
fields:
  id: { type: id }
  summary: { type: text, required: false }
  labels:
    type: list
    of: { type: string }
    ordered: { significance: display order }
---
dialect: source/1
of: product
layout: rows
path: content/products.yaml
---
dialect: type/1
id: settings
fields:
  theme: { type: string }
  note: { type: text, required: false }
---
dialect: source/1
of: settings
layout: single
path: content/settings.yaml
---
dialect: type/1
id: decision
fields:
  what: { type: text }
---
dialect: source/1
of: decision
layout: rows
path: content/decisions.yaml
---
dialect: type/1
id: rate_table
fields:
  note: { type: text }
value:
  type: map
  key: { type: enum, values: [standard, premium] }
  value: { type: int }
---
dialect: source/1
of: rate_table
layout: keyed_map
path: content/rates.yaml
record_keys: [basic]
''',
    )
    write(
        'content/products.yaml',
        '''
- id: bolt
  summary: threaded fastener
  labels: [metal, hardware]
- id: nut
  labels: [metal, connector]
''',
    )
    write('content/settings.yaml', 'theme: plain\n')
    write('content/decisions.yaml', '- { what: first }\n- { what: second }\n')
    write(
        'content/rates.yaml',
        'note: reviewed\nbasic: { standard: 10, premium: 20 }\n',
    )
    profile = load_profile(profile_dir)
    return profile, load_corpus(profile, tmp_path)


def test_resolve_anchor_defines_every_anchor_kind_slice(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    profile, corpus = _catalogue(tmp_path, profile_dir, write)

    type_anchor = resolve_anchor(parse_anchor('product'), profile, corpus)
    record_anchor = resolve_anchor(
        parse_anchor('product/bolt'), profile, corpus
    )
    document_anchor = resolve_anchor(
        parse_anchor('settings/@doc'), profile, corpus
    )
    row_anchor = resolve_anchor(
        parse_anchor('decision/#1'), profile, corpus
    )
    field_anchor = resolve_anchor(
        parse_anchor('product/bolt/summary'), profile, corpus
    )

    assert type_anchor.point is None
    assert [pair[0] for pair in type_anchor.slice_value] == ['bolt', 'nut']
    assert record_anchor.point == Point('product', 'bolt')
    assert record_anchor.slice_value is record_anchor.record.data
    assert document_anchor.point == Point('settings', DOC)
    assert document_anchor.slice_value == {'theme': 'plain'}
    assert row_anchor.point == Point(
        'decision', ('content/decisions.yaml', 1)
    )
    assert row_anchor.slice_value == {'what': 'second'}
    assert field_anchor.point == Point(
        'product', 'bolt', ('summary',)
    )
    assert field_anchor.slice_value == 'threaded fastener'


def test_value_shaped_record_and_metadata_use_their_raw_loaded_slices(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    profile, corpus = _catalogue(tmp_path, profile_dir, write)

    record = resolve_anchor(
        parse_anchor('rate_table/basic'), profile, corpus
    )
    metadata = resolve_anchor(
        parse_anchor('rate_table/@doc/note'), profile, corpus
    )

    assert record.slice_value == {'standard': 10, 'premium': 20}
    assert metadata.slice_value == 'reviewed'


def test_type_slice_orders_row_records_by_numeric_ordinal(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    profile, corpus = _catalogue(tmp_path, profile_dir, write)
    bolt = corpus.find('product', 'bolt')
    nut = corpus.find('product', 'nut')
    assert bolt is not None and nut is not None
    bolt.ordinal = 10
    nut.ordinal = 2

    resolved = resolve_anchor(parse_anchor('product'), profile, corpus)

    assert [pair[0] for pair in resolved.slice_value] == ['nut', 'bolt']


def test_independent_reloads_produce_identical_anchor_hashes(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    profile, first = _catalogue(tmp_path, profile_dir, write)
    second = load_corpus(profile, tmp_path)
    anchors = [
        'product',
        'product/bolt',
        'product/bolt/summary',
        'settings/@doc',
        'decision/#1',
        'rate_table/basic',
        'rate_table/@doc/note',
    ]

    first_hashes = [
        slice_hash(resolve_anchor(parse_anchor(text), profile, first).slice_value)
        for text in anchors
    ]
    second_hashes = [
        slice_hash(resolve_anchor(parse_anchor(text), profile, second).slice_value)
        for text in anchors
    ]

    assert first_hashes == second_hashes
    assert all(
        value.startswith('sha256:') and len(value) == 71
        for value in first_hashes
    )


def test_editing_outside_a_field_slice_does_not_change_its_hash(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    profile, corpus = _catalogue(tmp_path, profile_dir, write)
    anchor = parse_anchor('product/bolt/summary')
    before = slice_hash(resolve_anchor(anchor, profile, corpus).slice_value)
    record = corpus.find('product', 'bolt')
    assert record is not None
    record.data['labels'].append('featured')

    after = slice_hash(resolve_anchor(anchor, profile, corpus).slice_value)

    assert after == before


def test_editing_inside_a_field_slice_changes_its_hash(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    profile, corpus = _catalogue(tmp_path, profile_dir, write)
    anchor = parse_anchor('product/bolt/summary')
    before = slice_hash(resolve_anchor(anchor, profile, corpus).slice_value)
    record = corpus.find('product', 'bolt')
    assert record is not None
    record.data['summary'] = 'changed'

    after = slice_hash(resolve_anchor(anchor, profile, corpus).slice_value)

    assert after != before


def test_list_reordering_changes_the_slice_hash(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    profile, corpus = _catalogue(tmp_path, profile_dir, write)
    anchor = parse_anchor('product/bolt/labels')
    before = slice_hash(resolve_anchor(anchor, profile, corpus).slice_value)
    record = corpus.find('product', 'bolt')
    assert record is not None
    record.data['labels'].reverse()

    after = slice_hash(resolve_anchor(anchor, profile, corpus).slice_value)

    assert after != before


def test_mixed_integer_and_string_map_keys_hash_without_sort_failure() -> None:
    first = {1: 'integer', '1': 'string', 2: {'b': 2, 'a': 1}}
    second = {2: {'a': 1, 'b': 2}, '1': 'string', 1: 'integer'}

    assert slice_hash(first) == slice_hash(second)


def test_absent_and_present_null_have_different_hashes(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    profile, corpus = _catalogue(tmp_path, profile_dir, write)
    anchor = parse_anchor('product/nut/summary')
    absent = resolve_anchor(anchor, profile, corpus).slice_value
    assert absent is ABSENT
    record = corpus.find('product', 'nut')
    assert record is not None
    record.data['summary'] = None

    present_null = resolve_anchor(anchor, profile, corpus).slice_value

    assert present_null is None
    assert slice_hash(absent) != slice_hash(present_null)


def test_mapping_order_is_canonical_but_list_order_is_not() -> None:
    assert canonical_bytes({'b': 2, 'a': 1}) == canonical_bytes(
        {'a': 1, 'b': 2}
    )
    assert canonical_bytes(['a', 'b']) != canonical_bytes(['b', 'a'])


def test_unserializable_slice_is_a_specific_evaluation_error() -> None:
    with pytest.raises(EvaluationError) as exc_info:
        slice_hash(object())

    assert 'anchored slice cannot be serialized' in str(exc_info.value)
