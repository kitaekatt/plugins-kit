'''Selector parsing, normalization, canonical rendering, and anchor syntax.'''

import pytest

from yaml_data_editor_kit.comments import (
    DOC,
    STAR,
    Predicate,
    SelectorError,
    parse_anchor,
    parse_selector,
)


@pytest.mark.parametrize(
    ('text', 'canonical'),
    [
        ('*', '*'),
        ('product', 'product'),
        ('product/bolt', 'product/bolt'),
        ('settings/@doc', 'settings/@doc'),
        ('decision/#7', 'decision/#7'),
        ('product/*/price', 'product/*/price'),
        ('product/[tier=premium]', 'product/[tier=premium]'),
        ('product/[tier!=premium]', 'product/[tier!=premium]'),
        ('product/[labels has metal]', 'product/[labels has metal]'),
        (
            'product/[tier in standard,premium]',
            'product/[tier in standard,premium]',
        ),
        ('product/bolt/stats.weight', 'product/bolt/stats.weight'),
        ('settings/@doc/rates.*.value', 'settings/@doc/rates.*.value'),
    ],
)
def test_every_selector_production_round_trips(text: str, canonical: str) -> None:
    assert parse_selector(text).canonical() == canonical


def test_parsed_segments_have_structural_carriers() -> None:
    selector = parse_selector('product/[tier in standard, premium]/prices.*.amount')

    assert selector.record_seg == Predicate(
        field='tier', op='in', values=('standard', 'premium')
    )
    assert selector.field_path == ('prices', STAR, 'amount')
    assert parse_selector('settings/@doc').record_seg is DOC


def test_redundant_record_and_trailing_field_wildcards_normalize() -> None:
    assert parse_selector('product/*') == parse_selector('product')
    assert parse_selector('settings/@doc/rates.*') == parse_selector(
        'settings/@doc/rates'
    )
    assert parse_selector('product/*/*').canonical() == 'product'


@pytest.mark.parametrize(
    'text',
    [
        'product/bolt',
        'settings/@doc/rates.standard',
        'decision/#7',
    ],
)
def test_singular_selectors_parse_as_anchors(text: str) -> None:
    assert parse_anchor(text).canonical() == text


@pytest.mark.parametrize(
    ('text', 'message'),
    [
        ('', 'empty'),
        ('product//price', 'empty slash-separated segment'),
        ('product/bolt/price/amount', 'at most three'),
        ('product/bolt/prices..amount', 'empty path element'),
        ('product/shuriken_*', 'identity globbing is refused'),
        ('product/[price>10]', 'numeric comparison predicates are refused'),
        ('product/[price<=10]', 'numeric comparison predicates are refused'),
        ('product/[tier contains premium]', 'unknown operator'),
        ('product/[tier.name=premium]', 'malformed'),
        ('product/[tier=premium', 'missing its closing ]'),
        ('product/[tier=]', 'empty value'),
        ('product/[tier in premium,]', 'empty value in its value list'),
        ('product/[tier=premium,standard]', 'only in takes a value list'),
        ('decision/#seven', 'non-negative integer'),
    ],
)
def test_malformed_selectors_fail_with_the_offending_content(
    text: str, message: str
) -> None:
    with pytest.raises(SelectorError) as exc_info:
        parse_selector(text)

    assert repr(text) in str(exc_info.value)
    assert message in str(exc_info.value)


@pytest.mark.parametrize(
    ('text', 'segment'),
    [
        ('*', '*'),
        ('product/*/price', '*'),
        ('product/[tier=premium]', '[tier=premium]'),
        ('settings/@doc/rates.*', '*'),
    ],
)
def test_anchors_refuse_wildcards_and_predicates(text: str, segment: str) -> None:
    with pytest.raises(SelectorError) as exc_info:
        parse_anchor(text)

    message = str(exc_info.value)
    assert repr(text) in message
    assert repr(segment) in message
    assert 'anchor is singular' in message
