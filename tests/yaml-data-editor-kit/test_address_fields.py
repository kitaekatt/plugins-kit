'''Field address evaluation follows the profile's declared type shapes.'''

from pathlib import Path
from typing import Callable

import pytest

from yaml_data_editor_kit.comments import DOC, EvaluationError, Point, evaluate, parse_selector
from yaml_data_editor_kit.schema import Corpus, Profile, load_corpus, load_profile

Writer = Callable[[str, str], Path]


def _field_catalogue(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> tuple[Profile, Corpus]:
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
id: measure
identified_by: id
fields:
  id: { type: id }
---
dialect: source/1
of: measure
layout: rows
path: content/measures.yaml
---
dialect: type/1
id: product
identified_by: id
fields:
  id: { type: id }
  kind: { type: enum, values: [physical, subscription] }
  category_prices:
    type: map
    key: { type: enum, values_from: category.id }
    value: { type: int }
  measurements:
    type: map
    key: { type: ref, to: measure }
    value: { type: float }
  attributes:
    type: map
    key: { type: id }
    value: { type: text }
  labels: { type: list, of: { type: string } }
  summary: { type: text, required: false }
variants:
  on: kind
  when:
    subscription:
      plan:
        type: record
        fields:
          level: { type: int }
---
dialect: source/1
of: product
layout: rows
path: content/products.yaml
''',
    )
    write(
        'profile/value_types.yaml',
        '''
dialect: type/1
id: rate_table
fields:
  note: { type: text }
value:
  type: map
  key: { type: enum, values_from: category.id }
  value: { type: int }
---
dialect: source/1
of: rate_table
layout: keyed_map
path: content/rates.yaml
record_keys: [basic]
---
dialect: type/1
id: label_pool
value:
  type: list
  of: { type: string }
---
dialect: source/1
of: label_pool
layout: keyed_map
path: content/pools.yaml
record_keys: [default]
''',
    )
    write('content/categories.yaml', '- { id: standard }\n- { id: premium }\n')
    write('content/measures.yaml', '- { id: weight }\n- { id: length }\n')
    write(
        'content/products.yaml',
        '''
- id: bolt
  kind: physical
  category_prices: { standard: 10, premium: 15 }
  measurements: { weight: 1.5 }
  attributes: { finish: zinc }
  labels: [metal, fastener]
- id: service
  kind: subscription
  category_prices: { standard: 20, premium: 30 }
  measurements: { length: 12.0 }
  attributes: { cadence: monthly }
  labels: [recurring]
  plan: { level: 2 }
''',
    )
    write(
        'content/rates.yaml',
        'note: reviewed\nbasic: { standard: 10, premium: 20 }\n',
    )
    write('content/pools.yaml', 'default: [metal, recurring]\n')
    profile = load_profile(profile_dir)
    return profile, load_corpus(profile, tmp_path)


def test_value_shaped_identity_resolves_against_the_value_declaration(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    profile, corpus = _field_catalogue(tmp_path, profile_dir, write)

    result = evaluate(parse_selector('rate_table/basic/standard'), profile, corpus)

    assert result.points == frozenset(
        {Point('rate_table', 'basic', ('standard',))}
    )


def test_value_shaped_metadata_resolves_only_through_document_identity(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    profile, corpus = _field_catalogue(tmp_path, profile_dir, write)

    result = evaluate(parse_selector('rate_table/@doc/note'), profile, corpus)

    assert result.points == frozenset({Point('rate_table', DOC, ('note',))})


def test_metadata_field_under_value_identity_errors_with_document_hint(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    profile, corpus = _field_catalogue(tmp_path, profile_dir, write)

    with pytest.raises(EvaluationError) as exc_info:
        evaluate(parse_selector('rate_table/basic/note'), profile, corpus)

    message = str(exc_info.value)
    assert 'not in the value declaration' in message
    assert 'metadata field' in message
    assert 'rate_table/@doc/note' in message


def test_value_key_under_document_identity_is_an_unknown_metadata_field(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    profile, corpus = _field_catalogue(tmp_path, profile_dir, write)

    with pytest.raises(EvaluationError) as exc_info:
        evaluate(parse_selector('rate_table/@doc/standard'), profile, corpus)

    message = str(exc_info.value)
    assert 'metadata fields of value-shaped type' in message
    assert 'standard' in message
    assert 'note' in message


def test_values_from_map_key_membership_is_checked(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    profile, corpus = _field_catalogue(tmp_path, profile_dir, write)

    result = evaluate(
        parse_selector('product/bolt/category_prices.premium'), profile, corpus
    )
    assert result.points == frozenset(
        {Point('product', 'bolt', ('category_prices', 'premium'))}
    )

    with pytest.raises(EvaluationError) as exc_info:
        evaluate(
            parse_selector('product/bolt/category_prices.unknown'), profile, corpus
        )
    assert 'declared set' in str(exc_info.value)


def test_ref_map_key_membership_is_checked(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    profile, corpus = _field_catalogue(tmp_path, profile_dir, write)

    result = evaluate(
        parse_selector('product/bolt/measurements.weight'), profile, corpus
    )
    assert result.points == frozenset(
        {Point('product', 'bolt', ('measurements', 'weight'))}
    )

    with pytest.raises(EvaluationError) as exc_info:
        evaluate(
            parse_selector('product/bolt/measurements.volume'), profile, corpus
        )
    assert 'declared set' in str(exc_info.value)
    assert 'volume' in str(exc_info.value)


def test_bare_id_map_key_refusal_names_the_addressable_unit(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    profile, corpus = _field_catalogue(tmp_path, profile_dir, write)

    with pytest.raises(EvaluationError) as exc_info:
        evaluate(parse_selector('product/bolt/attributes.finish'), profile, corpus)

    message = str(exc_info.value)
    assert 'bare' in message and 'id' in message
    assert 'product/bolt/attributes' in message
    assert 'declare the key set' in message


def test_selector_may_end_at_a_list_but_cannot_step_into_it(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    profile, corpus = _field_catalogue(tmp_path, profile_dir, write)

    result = evaluate(parse_selector('product/bolt/labels'), profile, corpus)
    assert result.points == frozenset({Point('product', 'bolt', ('labels',))})

    with pytest.raises(EvaluationError) as exc_info:
        evaluate(parse_selector('product/bolt/labels.0'), profile, corpus)
    message = str(exc_info.value)
    assert 'cannot step into a list' in message
    assert 'product/bolt/labels' in message


def test_value_shaped_list_is_terminal(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    profile, corpus = _field_catalogue(tmp_path, profile_dir, write)

    assert evaluate(
        parse_selector('label_pool/default'), profile, corpus
    ).points == frozenset({Point('label_pool', 'default')})

    with pytest.raises(EvaluationError) as exc_info:
        evaluate(parse_selector('label_pool/default/0'), profile, corpus)
    assert 'label_pool/default' in str(exc_info.value)
    assert 'complete list' in str(exc_info.value)


def test_variant_field_resolves_for_the_matching_discriminator(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    profile, corpus = _field_catalogue(tmp_path, profile_dir, write)

    result = evaluate(parse_selector('product/service/plan.level'), profile, corpus)

    assert result.points == frozenset(
        {Point('product', 'service', ('plan', 'level'))}
    )


def test_variant_field_on_the_wrong_discriminator_is_an_error(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    profile, corpus = _field_catalogue(tmp_path, profile_dir, write)

    with pytest.raises(EvaluationError) as exc_info:
        evaluate(parse_selector('product/bolt/plan.level'), profile, corpus)

    message = str(exc_info.value)
    assert 'variant field' in message
    assert 'kind' in message and 'physical' in message


def test_declared_optional_absent_field_still_resolves_to_a_point(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    profile, corpus = _field_catalogue(tmp_path, profile_dir, write)
    record = corpus.find('product', 'bolt')
    assert record is not None
    assert 'summary' not in record.data

    result = evaluate(parse_selector('product/bolt/summary'), profile, corpus)

    assert result.points == frozenset({Point('product', 'bolt', ('summary',))})


def test_unknown_nested_record_field_names_where_the_walk_stopped(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    profile, corpus = _field_catalogue(tmp_path, profile_dir, write)

    with pytest.raises(EvaluationError) as exc_info:
        evaluate(parse_selector('product/service/plan.rank'), profile, corpus)

    message = str(exc_info.value)
    assert 'rank' in message
    assert 'product/service/plan' in message
    assert 'level' in message
