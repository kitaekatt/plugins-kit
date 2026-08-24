'''The anchor and comment model: anchored units of intent over a loaded corpus.'''

from .address import (
    DOC,
    STAR,
    Evaluation,
    Point,
    Predicate,
    Selector,
    evaluate,
    parse_anchor,
    parse_selector,
)
from .errors import AddressError, EvaluationError, SelectorError

__all__ = [
    'AddressError',
    'DOC',
    'Evaluation',
    'EvaluationError',
    'Point',
    'Predicate',
    'STAR',
    'Selector',
    'SelectorError',
    'evaluate',
    'parse_anchor',
    'parse_selector',
]
