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
    contains,
    resolve_anchor,
)
from .errors import AddressError, EvaluationError, SelectorError
from .hashing import slice_hash
from .staleness import (
    MOVED,
    OK,
    UNRESOLVABLE,
    AnchorReport,
    check_anchors,
    reanchor,
)
from .store import Comment, CommentSet, CommentStore

__all__ = [
    'AddressError',
    'AnchorReport',
    'Comment',
    'CommentSet',
    'CommentStore',
    'DOC',
    'Evaluation',
    'EvaluationError',
    'MOVED',
    'OK',
    'Point',
    'Predicate',
    'ResolvedAnchor',
    'STAR',
    'Selector',
    'SelectorError',
    'UNRESOLVABLE',
    'check_anchors',
    'evaluate',
    'intersects',
    'overlaps',
    'parse_anchor',
    'parse_selector',
    'contains',
    'reanchor',
    'resolve_anchor',
    'slice_hash',
]
