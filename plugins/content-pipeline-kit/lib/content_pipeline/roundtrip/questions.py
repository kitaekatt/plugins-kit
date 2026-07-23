"""Machine asks -> human answers -> answers re-enter as context.

When a pipeline stage cannot resolve a candidate without more information than
it has, it emits a question against an entity. Questions accumulate on the
entity's record, are surfaced to a human, and once answered the answers
re-enter the pipeline as additional generation context for the next attempt --
the human is a context SOURCE, not a blocking approval gate.

The load-bearing property, carried from the source ``conversation_file``
questions loop, is that a human answer SURVIVES regeneration: a structural
regen that rewrites the question set must preserve any answer matched by
question id, AND retain an answered question that dropped out of the new set
entirely (an orphaned answer is authored work). :func:`merge_questions`
delegates that to ``store.attributed.merge_preserved_fields`` with a
:class:`~content_pipeline.store.attributed.CollectionMerge` -- so the do-no-harm
answer-preservation rule lives in exactly one place, shared with the store.

Questions are plain dicts (``{"id", "prompt", "answer"}``) so they round-trip
through any YAML engine the consumer already uses; a frozen :class:`Question`
view is offered for typed access.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Mapping, MutableMapping, Optional, Sequence

from content_pipeline.store.attributed import (
    CollectionMerge,
    MergePolicy,
    merge_preserved_fields,
)

ID_KEY = "id"
PROMPT_KEY = "prompt"
ANSWER_KEY = "answer"

# The question sub-collection preservation rule: an answer is human-authored
# work carried forward whenever present, and an answered question that has no
# match in a regenerated set is retained as an orphan.
QUESTION_MERGE = CollectionMerge(
    id_key=ID_KEY,
    human_fields=(ANSWER_KEY,),
    keep_orphans_when=(ANSWER_KEY,),
)


@dataclass(frozen=True)
class Question:
    """A typed view of one question dict."""

    id: str
    prompt: str = ""
    answer: str = ""

    @property
    def answered(self) -> bool:
        """True when a non-empty human answer is present."""
        return bool(self.answer and self.answer.strip())

    @classmethod
    def from_dict(cls, doc: Mapping[str, Any]) -> "Question":
        return cls(
            id=str(doc.get(ID_KEY, "")),
            prompt=str(doc.get(PROMPT_KEY, "") or ""),
            answer=str(doc.get(ANSWER_KEY, "") or ""),
        )


def ask(questions: Sequence[MutableMapping], question_id: str, prompt: str) -> List[dict]:
    """Return ``questions`` with a question for ``question_id`` present.

    If a question with that id already exists its prompt is refreshed (a
    re-ask with updated wording) and any existing answer is preserved; a new id
    is appended with no answer. Returns a new list (input is not mutated).
    """
    out: List[dict] = [dict(q) for q in questions]
    for q in out:
        if q.get(ID_KEY) == question_id:
            q[PROMPT_KEY] = prompt
            return out
    out.append({ID_KEY: question_id, PROMPT_KEY: prompt, ANSWER_KEY: ""})
    return out


def answer(
    questions: Sequence[MutableMapping], question_id: str, answer_text: str
) -> List[dict]:
    """Return ``questions`` with ``question_id``'s answer set to ``answer_text``.

    Raises :class:`KeyError` when no question carries ``question_id`` -- an
    answer to a question that was never asked is a caller error, not a silent
    new question. Returns a new list.
    """
    out: List[dict] = [dict(q) for q in questions]
    for q in out:
        if q.get(ID_KEY) == question_id:
            q[ANSWER_KEY] = answer_text
            return out
    raise KeyError(f"no question with id {question_id!r} to answer")


def unanswered(questions: Sequence[Mapping]) -> List[dict]:
    """Return the questions with no non-empty answer (still awaiting a human)."""
    return [
        dict(q)
        for q in questions
        if not (q.get(ANSWER_KEY) and str(q.get(ANSWER_KEY)).strip())
    ]


def answered_context(questions: Sequence[Mapping]) -> List[dict]:
    """Return ``{id, prompt, answer}`` fragments for every answered question.

    This is what re-enters generation as context -- the human's answers as
    additional prompt material. Unanswered questions contribute nothing.
    """
    out: List[dict] = []
    for q in questions:
        ans = q.get(ANSWER_KEY)
        if ans and str(ans).strip():
            out.append(
                {
                    ID_KEY: q.get(ID_KEY, ""),
                    PROMPT_KEY: q.get(PROMPT_KEY, ""),
                    ANSWER_KEY: ans,
                }
            )
    return out


def merge_questions(
    existing: Optional[Sequence[Mapping]],
    incoming: Sequence[MutableMapping],
    *,
    collection_key: str = "questions",
) -> List[dict]:
    """Merge a regenerated question set onto the existing one, keeping answers.

    Delegates to ``store.attributed.merge_preserved_fields`` with the
    :data:`QUESTION_MERGE` collection rule, so a matched question keeps its
    human answer and an answered question dropped from the new set is retained
    as an orphan -- the do-no-harm answer-preservation the source loop pins.
    Returns the merged question list.
    """
    policy = MergePolicy(collections={collection_key: QUESTION_MERGE})
    existing_doc = {collection_key: list(existing or ())}
    incoming_doc: dict = {collection_key: [dict(q) for q in incoming]}
    merged = merge_preserved_fields(existing_doc, incoming_doc, policy=policy)
    return list(merged.get(collection_key) or ())


__all__ = [
    "ID_KEY",
    "PROMPT_KEY",
    "ANSWER_KEY",
    "QUESTION_MERGE",
    "Question",
    "ask",
    "answer",
    "unanswered",
    "answered_context",
    "merge_questions",
]
