"""The Stage protocol: a pure function over the store.

Every pipeline shape (single-pass, convergence-loop) is a composition of
Stage instances. A Stage takes the store (and whatever work-unit context it
needs) and returns an updated store -- no hidden side effects, no direct I/O
inside a stage body. This is what lets both pipeline shapes reuse the same
stage implementations and what lets a stage be tested in isolation against a
mock store.
"""

from typing import Protocol


class Stage(Protocol):
    def __call__(self, store, context) -> object:
        ...
