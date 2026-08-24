'''Predicate coercion and check-time wildcard expansion.'''

from pathlib import Path
from typing import Callable

import pytest

from yaml_data_editor_kit.comments import (
    DOC,
    EvaluationError,
    Point,
    evaluate,
    parse_selector,
)
from yaml_data_editor_kit.schema import Corpus, Profile, load_corpus, load_profile

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
  kind: { type: enum, values: [physical, service] }
  tier: { type: enum, values: [standard, premium] }
  code:
    type: enum
    stored: int
    values: { 1: one, 2: two }
  active: { type: bool }
  quantity: { type: int }
  ratio: { type: float }
  labels: { type: list, of: { type: string } }
  prices:
    type: map
    key: { type: enum, values: [standard, premium] }
    value:
      type: record
      fields:
        amount: { type: int }
  attributes:
    type: map
    key: { type: id }
    value: { type: text }
variants:
  on: kind
  when:
    service:
      plan:
        type: record
        fields:
          level: { type: int }
---
dialect: source/1
of: product
layout: rows
path: content/products.yaml
---
dialect: type/1
id: settings
fields:
  enabled: { type: bool }
---
dialect: source/1
of: settings
layout: single
path: content/settings.yaml
''',
    )
    write(
        'content/products.yaml',
        '''
- id: bolt
  kind: physical
  tier: standard
  code: 1
  active: true
  quantity: 2
  ratio: 1.5
  labels: [metal, fastener]
  prices:
    standard: { amount: 10 }
    premium: { amount: 15 }
  attributes: { finish: zinc }
- id: support
  kind: service
  tier: premium
  code: 2
  active: false
  quantity: 5
  ratio: 2
  labels: [recurring]
  prices:
    standard: { amount: 20 }
  attributes: { cadence: monthly }
  plan: { level: 3 }
''',
    )
    write('content/settings.yaml', 'enabled: true\n')
    profile = load_profile(profile_dir)
    return profile, load_corpus(profile, tmp_path)


@pytest.mark.parametrize(
    ('selector_text', 'identity'),
    [
        ('product/[quantity=2]', 'bolt'),
        ('product/[ratio=2]', 'support'),
        ('product/[active=true]', 'bolt'),
        ('product/[code=1]', 'bolt'),
        ('product/[tier in premium,standard]', 'bolt'),
        ('product/[tier!=standard]', 'support'),
        ('product/[labels has recurring]', 'support'),
    ],
)
def test_predicates_coerce_values_under_the_declared_type(
    tmp_path: Path,
    profile_dir: Path,
    write: Writer,
    selector_text: str,
    identity: str,
) -> None:
    profile, corpus = _catalogue(tmp_path, profile_dir, write)

    result = evaluate(parse_selector(selector_text), profile, corpus)

    assert Point('product', identity) in result.points


def test_in_matches_every_value_named_by_the_value_list(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    profile, corpus = _catalogue(tmp_path, profile_dir, write)

    result = evaluate(
        parse_selector('product/[tier in premium,standard]'), profile, corpus
    )

    assert result.points == frozenset(
        {Point('product', 'bolt'), Point('product', 'support')}
    )
    assert result.matched_records == 2


def test_zero_match_predicate_is_visible_and_legal(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    profile, corpus = _catalogue(tmp_path, profile_dir, write)
    corpus.records = [
        record
        for record in corpus.records
        if record.type_id != 'product' or record.identity != 'support'
    ]

    result = evaluate(
        parse_selector('product/[tier=premium]'), profile, corpus
    )

    assert result.points == frozenset()
    assert result.matched_records == 0


def test_zero_match_still_refuses_an_unknown_output_field(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    profile, corpus = _catalogue(tmp_path, profile_dir, write)
    corpus.records = [
        record
        for record in corpus.records
        if record.type_id != 'product' or record.identity != 'support'
    ]

    with pytest.raises(EvaluationError) as exc_info:
        evaluate(
            parse_selector('product/[tier=premium]/unknown'), profile, corpus
        )

    message = str(exc_info.value)
    assert 'unknown' in message
    assert 'fields of type' in message


def test_unknown_predicate_field_is_an_error_not_zero_matches(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    profile, corpus = _catalogue(tmp_path, profile_dir, write)

    with pytest.raises(EvaluationError) as exc_info:
        evaluate(parse_selector('product/[teir=premium]'), profile, corpus)

    message = str(exc_info.value)
    assert 'teir' in message
    assert 'tier' in message


def test_value_outside_its_enum_refuses_the_entire_predicate(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    profile, corpus = _catalogue(tmp_path, profile_dir, write)
    record = corpus.find('product', 'support')
    assert record is not None
    record.data['tier'] = 'retired'

    with pytest.raises(EvaluationError) as exc_info:
        evaluate(
            parse_selector('product/[tier in premium,standard]'), profile, corpus
        )

    message = str(exc_info.value)
    assert 'support' in message
    assert 'retired' in message
    assert 'declared enum' in message


def test_uncoercible_scalar_names_the_record_field_and_value(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    profile, corpus = _catalogue(tmp_path, profile_dir, write)
    record = corpus.find('product', 'bolt')
    assert record is not None
    record.data['quantity'] = 'two'

    with pytest.raises(EvaluationError) as exc_info:
        evaluate(parse_selector('product/[quantity=2]'), profile, corpus)

    message = str(exc_info.value)
    assert 'bolt' in message
    assert 'quantity' in message
    assert 'two' in message


def test_has_requires_a_declared_list(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    profile, corpus = _catalogue(tmp_path, profile_dir, write)

    with pytest.raises(EvaluationError) as exc_info:
        evaluate(parse_selector('product/[tier has premium]'), profile, corpus)

    assert 'requires a declared list' in str(exc_info.value)


def test_record_wildcard_expands_fields_and_skips_absent_variant_fields(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    profile, corpus = _catalogue(tmp_path, profile_dir, write)

    result = evaluate(parse_selector('product/*/plan.level'), profile, corpus)

    assert result.points == frozenset(
        {Point('product', 'support', ('plan', 'level'))}
    )
    assert result.matched_records == 2


def test_mid_path_map_wildcard_expands_present_keys(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    profile, corpus = _catalogue(tmp_path, profile_dir, write)

    result = evaluate(
        parse_selector('product/bolt/prices.*.amount'), profile, corpus
    )

    assert result.points == frozenset(
        {
            Point('product', 'bolt', ('prices', 'standard', 'amount')),
            Point('product', 'bolt', ('prices', 'premium', 'amount')),
        }
    )


def test_map_wildcard_refuses_a_bare_id_key(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    profile, corpus = _catalogue(tmp_path, profile_dir, write)

    with pytest.raises(EvaluationError) as exc_info:
        evaluate(
            parse_selector('product/bolt/attributes.*.length'), profile, corpus
        )

    message = str(exc_info.value)
    assert 'bare' in message and 'id' in message
    assert 'product/bolt/attributes' in message


def test_type_wildcard_denotes_every_loaded_record(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    profile, corpus = _catalogue(tmp_path, profile_dir, write)

    result = evaluate(parse_selector('*'), profile, corpus)

    assert result.matched_records == len(corpus.records)
    assert result.points == frozenset(
        {
            Point('product', 'bolt'),
            Point('product', 'support'),
            Point('settings', DOC),
        }
    )
