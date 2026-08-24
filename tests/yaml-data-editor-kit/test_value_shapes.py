'''Value-shaped keyed_map records and map totality.'''

from pathlib import Path
from typing import Callable

import pytest

from yaml_data_editor_kit.schema import (
    ProfileError,
    errors_only,
    load_corpus,
    load_profile,
    validate_corpus,
)

Writer = Callable[[str, str], Path]


def test_map_value_records_and_document_metadata_use_their_separate_shapes(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    write(
        'profile/catalog.yaml',
        '''
dialect: type/1
id: catalog
fields:
  category_order: { type: list, of: { type: string } }
---
dialect: source/1
of: catalog
layout: single
path: content/catalog.yaml
''',
    )
    write(
        'profile/rate_table.yaml',
        '''
dialect: type/1
id: rate_table
fields:
  note: { type: text }
value:
  type: map
  key: { type: enum, values_from: catalog.category_order }
  value: { type: int, unit: cents }
  total: true
---
dialect: source/1
of: rate_table
layout: keyed_map
path: content/rates.yaml
record_keys: [basic, plus]
''',
    )
    write('content/catalog.yaml', 'category_order: [standard, premium]\n')
    write(
        'content/rates.yaml',
        '''
note: reviewed
basic: { standard: 10, premium: 20 }
plus: { standard: 30, premium: 40 }
''',
    )

    profile = load_profile(profile_dir)
    rate_type = profile.types['rate_table']
    assert rate_type.value is not None
    assert rate_type.value.kind == 'map'
    assert rate_type.value.total is True
    assert rate_type.value.value is not None
    assert rate_type.value.value.unit == 'cents'

    corpus = load_corpus(profile, tmp_path)
    records = corpus.of_type('rate_table')
    assert [record.identity for record in records] == ['basic', 'plus', None]
    assert records[-1].data == {'note': 'reviewed'}
    assert errors_only(validate_corpus(profile, tmp_path)) == []


def test_list_value_records_apply_ref_and_min_length_annotations(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    write(
        'profile/types.yaml',
        '''
dialect: type/1
id: label
identified_by: id
fields:
  id: { type: id }
---
dialect: source/1
of: label
layout: rows
path: content/labels.yaml
---
dialect: type/1
id: label_pool
value:
  type: list
  of: { type: ref, to: label }
  min_length: 3
---
dialect: source/1
of: label_pool
layout: keyed_map
path: content/pools.yaml
record_keys: [default]
''',
    )
    write('content/labels.yaml', '- { id: metal }\n- { id: wood }\n- { id: glass }\n')
    write('content/pools.yaml', 'default: [metal, wood, glass]\n')

    profile = load_profile(profile_dir)
    assert errors_only(validate_corpus(profile, tmp_path)) == []


def test_list_value_records_enforce_min_length_once_on_the_record(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    write(
        'profile/types.yaml',
        '''
dialect: type/1
id: label
identified_by: id
fields:
  id: { type: id }
---
dialect: source/1
of: label
layout: rows
path: content/labels.yaml
---
dialect: type/1
id: label_pool
value:
  type: list
  of: { type: ref, to: label }
  min_length: 3
---
dialect: source/1
of: label_pool
layout: keyed_map
path: content/pools.yaml
record_keys: [default]
''',
    )
    write('content/labels.yaml', '- { id: metal }\n- { id: wood }\n')
    write('content/pools.yaml', 'default: [metal, wood]\n')

    problems = errors_only(validate_corpus(load_profile(profile_dir), tmp_path))

    assert len(problems) == 1
    assert problems[0].record == 'default'
    assert problems[0].field == 'value'
    assert 'below the declared min_length of 3' in problems[0].message


def test_bare_id_map_value_accepts_numeric_values_without_inventing_a_set(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    write(
        'profile/measure_table.yaml',
        '''
dialect: type/1
id: measure_table
value:
  type: map
  key: { type: id }
  value: { type: float }
---
dialect: source/1
of: measure_table
layout: keyed_map
path: content/measures.yaml
record_keys: [default]
''',
    )
    write('content/measures.yaml', 'default: { width: 2, height: 3.5 }\n')

    profile = load_profile(profile_dir)
    assert errors_only(validate_corpus(profile, tmp_path)) == []


def test_scalar_value_record_is_not_forced_into_a_field_mapping(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    write(
        'profile/count_table.yaml',
        '''
dialect: type/1
id: count_table
value: { type: int }
---
dialect: source/1
of: count_table
layout: keyed_map
path: content/counts.yaml
record_keys: [default]
''',
    )
    write('content/counts.yaml', 'default: 3\n')

    profile = load_profile(profile_dir)
    assert errors_only(validate_corpus(profile, tmp_path)) == []


def test_wrong_kind_value_body_uses_the_ordinary_value_type_error(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    write(
        'profile/rate_table.yaml',
        '''
dialect: type/1
id: rate_table
value:
  type: map
  key: { type: enum, values: [standard] }
  value: { type: int }
---
dialect: source/1
of: rate_table
layout: keyed_map
path: content/rates.yaml
record_keys: [basic]
''',
    )
    write('content/rates.yaml', 'basic: [10]\n')

    problems = errors_only(validate_corpus(load_profile(profile_dir), tmp_path))

    assert len(problems) == 1
    assert problems[0].record == 'basic'
    assert problems[0].field == 'value'
    assert 'declared \'map\'' in problems[0].message
    assert 'mapping of that record\'s fields' not in problems[0].message


def test_required_metadata_is_checked_once_per_document(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    write(
        'profile/count_table.yaml',
        '''
dialect: type/1
id: count_table
fields:
  note: { type: text }
value: { type: int }
---
dialect: source/1
of: count_table
layout: keyed_map
path: content/counts.yaml
record_keys: [basic, plus]
''',
    )
    write('content/counts.yaml', 'basic: 2\nplus: 3\n')

    problems = errors_only(validate_corpus(load_profile(profile_dir), tmp_path))

    assert len(problems) == 1
    assert problems[0].record == '<the document>'
    assert problems[0].field == 'note'
    assert 'required but absent' in problems[0].message


def test_unknown_metadata_key_names_both_declared_sets(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    write(
        'profile/count_table.yaml',
        '''
dialect: type/1
id: count_table
fields:
  note: { type: text }
value: { type: int }
---
dialect: source/1
of: count_table
layout: keyed_map
path: content/counts.yaml
record_keys: [basic]
''',
    )
    write('content/counts.yaml', 'note: reviewed\nnote_typo: stale\nbasic: 2\n')
    profile = load_profile(profile_dir)

    problems = errors_only(load_corpus(profile, tmp_path).diagnostics)

    assert len(problems) == 1
    assert problems[0].file == 'content/counts.yaml'
    assert problems[0].record == 'note_typo'
    assert 'unknown keyed_map key \'note_typo\'' in problems[0].message
    assert 'declared record keys: [basic]' in problems[0].message
    assert (
        'fields of value-shaped type \'count_table\': [note]'
        in problems[0].message
    )


def test_metadata_field_cannot_also_be_a_record_key(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    write(
        'profile/count_table.yaml',
        '''
dialect: type/1
id: count_table
fields:
  note: { type: text }
value: { type: int }
---
dialect: source/1
of: count_table
layout: keyed_map
path: content/counts.yaml
record_keys: [note, basic]
''',
    )
    write('content/counts.yaml', 'note: 4\nbasic: 2\n')
    profile = load_profile(profile_dir)

    problems = errors_only(load_corpus(profile, tmp_path).diagnostics)

    assert len(problems) == 1
    assert problems[0].file == 'content/counts.yaml'
    assert problems[0].record == 'note'
    assert 'metadata field \'note\'' in problems[0].message
    assert 'declared record keys' in problems[0].message
    assert 'value-shaped type \'count_table\'' in problems[0].message


def test_total_map_field_passes_when_every_inline_enum_member_is_present(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    write(
        'profile/product.yaml',
        '''
dialect: type/1
id: product
identified_by: id
fields:
  id: { type: id }
  rates:
    type: map
    key: { type: enum, values: [standard, premium] }
    value: { type: int }
    total: true
---
dialect: source/1
of: product
layout: rows
path: content/products.yaml
''',
    )
    write(
        'content/products.yaml',
        '- id: basic\n  rates: { standard: 10, premium: 20 }\n',
    )

    profile = load_profile(profile_dir)
    assert errors_only(validate_corpus(profile, tmp_path)) == []


def test_total_value_map_names_a_missing_values_from_member_set_map_and_record(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    write(
        'profile/catalog.yaml',
        '''
dialect: type/1
id: catalog
fields:
  category_order: { type: list, of: { type: string } }
---
dialect: source/1
of: catalog
layout: single
path: content/catalog.yaml
''',
    )
    write(
        'profile/rate_table.yaml',
        '''
dialect: type/1
id: rate_table
value:
  type: map
  key: { type: enum, values_from: catalog.category_order }
  value: { type: int }
  total: true
---
dialect: source/1
of: rate_table
layout: keyed_map
path: content/rates.yaml
record_keys: [basic]
''',
    )
    write('content/catalog.yaml', 'category_order: [standard, premium]\n')
    write('content/rates.yaml', 'basic: { standard: 10 }\n')

    problems = errors_only(validate_corpus(load_profile(profile_dir), tmp_path))

    assert len(problems) == 1
    assert problems[0].record == 'basic'
    assert problems[0].field == 'value'
    assert 'missing key \'premium\'' in problems[0].message
    assert 'declared set \'catalog.category_order\'' in problems[0].message
    assert 'map \'value\'' in problems[0].message


def test_total_ref_key_map_uses_the_referenced_type_identity_set(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    write(
        'profile/types.yaml',
        '''
dialect: type/1
id: category
identified_by: id
fields:
  id: { type: id }
---
dialect: source/1
of: category
layout: rows
path: content/categories.yaml
---
dialect: type/1
id: product
identified_by: id
fields:
  id: { type: id }
  rates:
    type: map
    key: { type: ref, to: category }
    value: { type: int }
    total: true
---
dialect: source/1
of: product
layout: rows
path: content/products.yaml
''',
    )
    write('content/categories.yaml', '- { id: standard }\n- { id: premium }\n')
    write('content/products.yaml', '- id: basic\n  rates: { standard: 10 }\n')

    problems = errors_only(validate_corpus(load_profile(profile_dir), tmp_path))

    assert len(problems) == 1
    assert problems[0].record == 'basic'
    assert 'missing key \'premium\'' in problems[0].message
    assert 'declared set of ref type \'category\'' in problems[0].message
    assert 'map \'rates\'' in problems[0].message


@pytest.mark.parametrize('conflict', ['identified_by', 'variants', 'extensible', 'open'])
def test_value_refuses_record_field_machinery(
    conflict: str, profile_dir: Path, write: Writer
) -> None:
    declarations = {
        'identified_by': 'identified_by: id',
        'variants': 'variants: { on: category, when: {} }',
        'extensible': 'extensible: { via: extends }',
        'open': 'open: { prefix: flag_, type: { type: text } }',
    }
    write(
        'profile/rate_table.yaml',
        (
            '''
dialect: type/1
id: rate_table
fields:
  id: { type: id }
  category: { type: enum, values: [standard] }
value: { type: int }
'''
            + declarations[conflict]
            + '\n'
        ),
    )

    with pytest.raises(ProfileError) as caught:
        load_profile(profile_dir)

    message = str(caught.value)
    assert 'type \'rate_table\'' in message
    assert '\'value:\'' in message
    assert '\'{0}:\''.format(conflict) in message


def test_value_shaped_type_refuses_non_keyed_map_source_after_deferred_resolution(
    profile_dir: Path, write: Writer
) -> None:
    write(
        'profile/a_source.yaml',
        '''
dialect: source/1
of: count_table
layout: rows
path: content/counts.yaml
''',
    )
    write(
        'profile/z_type.yaml',
        '''
dialect: type/1
id: count_table
value: { type: int }
''',
    )

    with pytest.raises(ProfileError) as caught:
        load_profile(profile_dir)

    message = str(caught.value)
    assert 'source for \'count_table\'' in message
    assert 'layout \'rows\'' in message
    assert 'value-shaped type \'count_table\'' in message
    assert '\'keyed_map\'' in message


def test_value_shaped_source_refuses_metadata_keys_restatement(
    profile_dir: Path, write: Writer
) -> None:
    write(
        'profile/count_table.yaml',
        '''
dialect: type/1
id: count_table
fields:
  note: { type: text }
value: { type: int }
---
dialect: source/1
of: count_table
layout: keyed_map
path: content/counts.yaml
record_keys: [basic]
metadata_keys: [note]
''',
    )

    with pytest.raises(ProfileError) as caught:
        load_profile(profile_dir)

    message = str(caught.value)
    assert 'source for \'count_table\'' in message
    assert '\'metadata_keys:\'' in message
    assert 'value-shaped type \'count_table\'' in message
    assert 'field names' in message


def test_total_true_refuses_a_bare_id_key(profile_dir: Path, write: Writer) -> None:
    write(
        'profile/measure_table.yaml',
        '''
dialect: type/1
id: measure_table
value:
  type: map
  key: { type: id }
  value: { type: float }
  total: true
''',
    )

    with pytest.raises(ProfileError) as caught:
        load_profile(profile_dir)

    message = str(caught.value)
    assert 'type \'measure_table\' \'value:\'' in message
    assert '\'total: true\'' in message
    assert 'bare \'id\'' in message
    assert 'no declared set' in message


def test_total_true_is_only_legal_on_a_map(profile_dir: Path, write: Writer) -> None:
    write(
        'profile/count_table.yaml',
        '''
dialect: type/1
id: count_table
value: { type: list, of: { type: int }, total: true }
''',
    )

    with pytest.raises(ProfileError) as caught:
        load_profile(profile_dir)

    message = str(caught.value)
    assert 'type \'count_table\' \'value:\'' in message
    assert '\'total: true\'' in message
    assert 'only legal on a map' in message


def test_view_of_value_shaped_type_can_name_metadata_but_not_value_internals(
    profile_dir: Path, write: Writer
) -> None:
    write(
        'profile/rate_table.yaml',
        '''
dialect: type/1
id: rate_table
fields:
  note: { type: text }
value:
  type: map
  key: { type: enum, values: [standard] }
  value: { type: int }
---
dialect: view/1
id: rate_table_card
of: rate_table
form: card
fields:
  - { field: note }
''',
    )
    profile = load_profile(profile_dir)
    assert profile.views['rate_table_card'].field_names() == ['note']

    write(
        'profile/bad_view.yaml',
        '''
dialect: view/1
id: rate_table_values
of: rate_table
form: table
fields:
  - { field: standard }
''',
    )
    with pytest.raises(ProfileError) as caught:
        load_profile(profile_dir)
    assert 'which type \'rate_table\' does not declare' in str(caught.value)


def test_anchored_metadata_path_does_not_read_identified_record_values(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    write(
        'profile/vocabulary_table.yaml',
        '''
dialect: type/1
id: vocabulary_table
fields:
  values: { type: list, of: { type: string } }
value:
  type: map
  key: { type: id }
  value: { type: float }
---
dialect: source/1
of: vocabulary_table
layout: keyed_map
path: content/vocabulary.yaml
record_keys: [default]
''',
    )
    write(
        'profile/product.yaml',
        '''
dialect: type/1
id: product
identified_by: id
fields:
  id: { type: id }
  label: { type: enum, values_from: vocabulary_table.values }
---
dialect: source/1
of: product
layout: rows
path: content/products.yaml
''',
    )
    write(
        'content/vocabulary.yaml',
        'values: [small, large]\ndefault: { values: 1.0 }\n',
    )
    write('content/products.yaml', '- { id: basic, label: 1.0 }\n')

    problems = errors_only(validate_corpus(load_profile(profile_dir), tmp_path))

    assert len(problems) == 1
    assert problems[0].record == 'basic'
    assert problems[0].field == 'label'
    assert 'not one of the declared values' in problems[0].message


def test_value_shaped_record_keys_from_keeps_the_missing_record_refusal(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    write(
        'profile/types.yaml',
        '''
dialect: type/1
id: item
identified_by: id
fields:
  id: { type: id }
---
dialect: source/1
of: item
layout: rows
path: content/items.yaml
---
dialect: type/1
id: count_table
value: { type: int }
---
dialect: source/1
of: count_table
layout: keyed_map
path: content/counts.yaml
record_keys_from: item.id
''',
    )
    write('content/items.yaml', '- { id: basic }\n- { id: plus }\n')
    write('content/counts.yaml', 'basic: 2\n')

    problems = errors_only(
        load_corpus(load_profile(profile_dir), tmp_path).diagnostics
    )

    assert len(problems) == 1
    assert problems[0].file == 'content/counts.yaml'
    assert problems[0].record == 'plus'
    assert 'record_keys_from: item.id' in problems[0].message
    assert 'document has no such key' in problems[0].message
