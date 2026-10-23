"""The Stage protocol: a pure function over the store.

Every pipeline shape (single-pass, convergence-loop) is a composition of
Stage instances. A Stage takes the store (and whatever work-unit context it
needs) and returns an updated store -- no hidden side effects, no direct I/O
inside a stage body. This is what lets both pipeline shapes reuse the same
stage implementations and what lets a stage be tested in isolation against a
mock store.
"""

from __future__ import annotations

from typing import Any, Protocol, Sequence


class Stage(Protocol):
    def __call__(self, store: Any, context: Any) -> Any:
        ...


def compose(store: Any, stages: Sequence[Stage], context: Any = None) -> Any:
    """Fold ``stages`` over ``store``, threading each stage's result forward.

    Each stage receives the store returned by the previous stage and the same
    ``context``. A stage that mutates the store in place may return ``None``;
    in that case the (mutated) store is carried forward unchanged, so both the
    functional (return-a-new-store) and imperative (mutate-and-return-None)
    stage styles compose under one helper.
    """
    current = store
    for stage in stages:
        result = stage(current, context)
        if result is not None:
            current = result
    return current


__all__ = ["Stage", "compose"]
