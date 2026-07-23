"""Tests for content_pipeline.roundtrip.questions.

Translates the conversation_file questions-loop behaviors: ask records a
question, answer persists a human answer, answered questions re-enter as
context, and -- the load-bearing case -- a regeneration preserves answers by id
AND retains an answered question that dropped out of the new set (an orphaned
answer is authored work).
"""

import pytest

from content_pipeline.roundtrip.questions import (
    Question,
    answer,
    answered_context,
    ask,
    merge_questions,
    unanswered,
)


def test_ask_appends_new_question():
    qs = ask([], "q1", "What tone?")
    assert qs == [{"id": "q1", "prompt": "What tone?", "answer": ""}]


def test_ask_refreshes_prompt_preserving_answer():
    qs = [{"id": "q1", "prompt": "old", "answer": "warm"}]
    out = ask(qs, "q1", "new wording")
    assert out[0]["prompt"] == "new wording"
    assert out[0]["answer"] == "warm"  # answer preserved on re-ask


def test_answer_sets_answer():
    qs = ask([], "q1", "What tone?")
    out = answer(qs, "q1", "warm and dry")
    assert out[0]["answer"] == "warm and dry"


def test_answer_unknown_question_raises():
    with pytest.raises(KeyError):
        answer([], "nope", "text")


def test_unanswered_filters():
    qs = [
        {"id": "a", "prompt": "p", "answer": ""},
        {"id": "b", "prompt": "p", "answer": "done"},
        {"id": "c", "prompt": "p", "answer": "   "},  # whitespace == unanswered
    ]
    assert [q["id"] for q in unanswered(qs)] == ["a", "c"]


def test_answered_context_is_what_reenters_generation():
    qs = [
        {"id": "a", "prompt": "tone?", "answer": "warm"},
        {"id": "b", "prompt": "pace?", "answer": ""},
    ]
    ctx = answered_context(qs)
    assert ctx == [{"id": "a", "prompt": "tone?", "answer": "warm"}]


# -- merge preservation (do-no-harm answer boundary) --------------------------

def test_merge_preserves_answer_by_id():
    existing = [{"id": "q1", "prompt": "old", "answer": "human answer"}]
    incoming = [{"id": "q1", "prompt": "regenerated prompt", "answer": ""}]
    merged = merge_questions(existing, incoming)
    q1 = next(q for q in merged if q["id"] == "q1")
    assert q1["answer"] == "human answer"  # answer carried forward
    assert q1["prompt"] == "regenerated prompt"  # prompt refreshed


def test_merge_retains_orphaned_answered_question():
    # A regen that drops q_old from the set must keep it BECAUSE it carries an
    # answer -- authored work is never dropped.
    existing = [{"id": "q_old", "prompt": "gone", "answer": "still valuable"}]
    incoming = [{"id": "q_new", "prompt": "fresh", "answer": ""}]
    merged = merge_questions(existing, incoming)
    ids = {q["id"] for q in merged}
    assert ids == {"q_new", "q_old"}  # orphaned-but-answered retained


def test_merge_drops_orphaned_unanswered_question():
    existing = [{"id": "q_old", "prompt": "gone", "answer": ""}]
    incoming = [{"id": "q_new", "prompt": "fresh", "answer": ""}]
    merged = merge_questions(existing, incoming)
    assert {q["id"] for q in merged} == {"q_new"}  # unanswered orphan dropped


def test_merge_none_existing_returns_incoming():
    incoming = [{"id": "q1", "prompt": "p", "answer": ""}]
    assert merge_questions(None, incoming) == incoming


# -- typed view ---------------------------------------------------------------

def test_question_from_dict_answered():
    q = Question.from_dict({"id": "x", "prompt": "p", "answer": "yes"})
    assert q.answered is True
    assert Question.from_dict({"id": "x", "prompt": "p"}).answered is False
