"""Machine asks -> human answers -> answers re-enter as context.

When a pipeline stage cannot resolve a candidate without more information
than it has, it emits a question. Questions are collected, presented to a
human, and the answers re-enter the pipeline as additional context for the
next generation attempt -- the human is a context source, not a blocking
approval gate.
"""


def ask(entity_id: str, question: str) -> None:
    """Record a question for an entity, to be surfaced to a human reviewer."""
    raise NotImplementedError


def answer(entity_id: str, answer_text: str) -> dict:
    """Record a human answer and return the context fragment it contributes."""
    raise NotImplementedError
