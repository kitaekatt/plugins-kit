'''The anchor and comment model: anchored units of intent over a loaded corpus.'''

from .address import (
    DOC,
    STAR,
    Evaluation,
    Point,
    Predicate,
    ResolvedAnchor,
    Selector,
    evaluate,
    intersects,
    overlaps,
    parse_anchor,
    parse_selector,
    point_within,
    resolve_anchor,
)
from .errors import AddressError, EvaluationError, SelectorError
from .hashing import slice_hash

__all__ = [
    'AddressError',
    'DOC',
    'Evaluation',
    'EvaluationError',
    'Point',
    'Predicate',
    'ResolvedAnchor',
    'STAR',
    'Selector',
    'SelectorError',
    'evaluate',
    'intersects',
    'overlaps',
    'parse_anchor',
    'parse_selector',
    'point_within',
    'resolve_anchor',
    'slice_hash',
]
