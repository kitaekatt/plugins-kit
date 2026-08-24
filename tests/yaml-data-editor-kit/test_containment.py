'''Concrete point containment and evaluation intersection.'''

from yaml_data_editor_kit.comments import (
    Evaluation,
    Point,
    intersects,
    overlaps,
    parse_selector,
    point_within,
)


def _evaluation(text: str, *points: Point) -> Evaluation:
    return Evaluation(
        selector=parse_selector(text),
        points=frozenset(points),
        matched_records=len({(point.type_id, point.record) for point in points}),
    )


def test_whole_record_contains_its_field_but_not_the_reverse() -> None:
    record = Point('product', 'bolt')
    field = Point('product', 'bolt', ('summary',))

    assert point_within(record, field)
    assert not point_within(field, record)
    assert overlaps(record, field)
    assert overlaps(field, record)


def test_sibling_fields_do_not_overlap() -> None:
    short = Point('product', 'bolt', ('description', 'short'))
    long = Point('product', 'bolt', ('description', 'long'))

    assert not point_within(short, long)
    assert not point_within(long, short)
    assert not overlaps(short, long)


def test_type_and_record_keys_must_match() -> None:
    field = Point('product', 'bolt', ('summary',))

    assert not overlaps(Point('category', 'bolt'), field)
    assert not overlaps(Point('product', 'nut'), field)


def test_intersects_returns_every_overlapping_pair() -> None:
    bolt = Point('product', 'bolt')
    nut = Point('product', 'nut')
    bolt_summary = Point('product', 'bolt', ('summary',))
    bolt_nested = Point('product', 'bolt', ('summary', 'short'))
    nut_price = Point('product', 'nut', ('price',))
    category = Point('category', 'standard')

    outer = _evaluation('product', bolt, nut)
    inner = _evaluation(
        '*', bolt_summary, bolt_nested, nut_price, category
    )

    assert intersects(outer, inner) == frozenset(
        {
            (bolt, bolt_summary),
            (bolt, bolt_nested),
            (nut, nut_price),
        }
    )


def test_disjoint_evaluations_return_an_empty_pair_set() -> None:
    left = _evaluation(
        'product/bolt/summary',
        Point('product', 'bolt', ('summary',)),
    )
    right = _evaluation(
        'product/bolt/price',
        Point('product', 'bolt', ('price',)),
    )

    assert intersects(left, right) == frozenset()
