"""Codex usage from the ``codex exec --json`` event stream.

The two fixtures under ``fixtures/codex_json`` are real streams captured from
codex-cli 0.164.0 (``codex exec --json --skip-git-repo-check -s read-only
-m gpt-6-luna``). ``trivial_turn.jsonl`` is verbatim; ``reasoning_turn.jsonl``
has one command's ``aggregated_output`` replaced by ``[elided]`` and is
otherwise verbatim. The measured relations the parser relies on, confirmed
against the session rollout's own ``token_count`` record: cached input is a
subset of input, reasoning output is a subset of output, and the blended total
is (input - cached) + output.
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from llm_kit.adapters.codex_backend import (
    CodexCliBackend,
    CodexUsage,
    parse_codex_usage_events,
)
from llm_kit.completion.types import BackendOptions

FIXTURES = Path(__file__).parent / "fixtures" / "codex_json"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _turn(usage: dict) -> str:
    return json.dumps({"type": "turn.completed", "usage": usage})


def test_trivial_turn_fixture_is_parsed():
    usage = parse_codex_usage_events(_fixture("trivial_turn.jsonl"))
    assert usage == CodexUsage(
        input_tokens=20467,
        cached_input_tokens=11520,
        output_tokens=5,
        reasoning_output_tokens=0,
    )
    assert usage.blended_total_tokens == (20467 - 11520) + 5


def test_reasoning_turn_fixture_reports_the_reasoning_share():
    usage = parse_codex_usage_events(_fixture("reasoning_turn.jsonl"))
    assert usage == CodexUsage(
        input_tokens=44811,
        cached_input_tokens=31744,
        output_tokens=224,
        reasoning_output_tokens=148,
    )
    # Reasoning is part of output, so it is not added to the total again.
    assert usage.blended_total_tokens == (44811 - 31744) + 224


def test_blended_total_matches_the_measured_tokens_used_line():
    # Measured pair: `codex exec` (no --json) printed "tokens used 8,913" for
    # a run whose rollout recorded input 20428, cached 11520, output 5.
    usage = CodexUsage(20428, 11520, 5, 0)
    assert usage.blended_total_tokens == 8913


def test_no_turn_completed_event_is_no_figure_not_zero():
    stream = "\n".join(
        [
            '{"type":"thread.started","thread_id":"t"}',
            '{"type":"turn.started"}',
            '{"type":"item.completed","item":{"id":"i","type":"agent_message","text":"x"}}',
        ]
    )
    assert parse_codex_usage_events(stream) is None
    assert parse_codex_usage_events("") is None


def test_turn_completed_without_usage_is_no_figure():
    assert parse_codex_usage_events('{"type":"turn.completed"}') is None


def test_non_json_lines_and_other_events_are_skipped():
    stream = "\n".join(
        [
            "not json at all",
            '{"type":"turn.started"}',
            "[1, 2, 3]",
            '{"truncated": ',
            _turn({"input_tokens": 10, "output_tokens": 2}),
        ]
    )
    assert parse_codex_usage_events(stream) == CodexUsage(10, 0, 2, 0)


def test_several_turns_are_summed():
    stream = "\n".join(
        [
            _turn({"input_tokens": 10, "cached_input_tokens": 4, "output_tokens": 2, "reasoning_output_tokens": 1}),
            _turn({"input_tokens": 20, "cached_input_tokens": 6, "output_tokens": 3, "reasoning_output_tokens": 2}),
        ]
    )
    assert parse_codex_usage_events(stream) == CodexUsage(30, 10, 5, 3)


def test_optional_categories_default_to_zero_when_absent_or_null():
    stream = _turn({"input_tokens": 7, "output_tokens": 1, "cached_input_tokens": None})
    assert parse_codex_usage_events(stream) == CodexUsage(7, 0, 1, 0)


@pytest.mark.parametrize(
    "usage",
    [
        {"output_tokens": 1},
        {"input_tokens": 1},
        {"input_tokens": "12", "output_tokens": 1},
        {"input_tokens": 1.5, "output_tokens": 1},
        {"input_tokens": -1, "output_tokens": 1},
        {"input_tokens": True, "output_tokens": 1},
        {"input_tokens": 1, "output_tokens": 1, "cached_input_tokens": "many"},
    ],
)
def test_a_malformed_usage_object_raises(usage):
    with pytest.raises(ValueError, match="turn.completed usage field"):
        parse_codex_usage_events(_turn(usage))


# -- end to end through CodexCliBackend.complete ---------------------------


def _backend(stdout: str, stderr: str = "", answer: str = "the answer"):
    calls = []

    def runner(argv, prompt, cwd, **kwargs):
        calls.append(list(argv))
        if answer is not None:
            Path(argv[argv.index("-o") + 1]).write_text(answer, encoding="utf-8")
        return stdout, stderr, 0

    return CodexCliBackend(argv_prefix=("codex",), runner=runner), calls


def _complete(backend, tmp_path):
    return backend.complete(
        "s", "u", model="gpt-5.4-codex", options=BackendOptions(cwd=tmp_path.resolve())
    )


def test_every_run_asks_for_json_events(tmp_path):
    backend, calls = _backend(_fixture("trivial_turn.jsonl"))
    _complete(backend, tmp_path)
    assert "--json" in calls[0]


def test_usage_lands_on_the_response(tmp_path):
    backend, _ = _backend(_fixture("reasoning_turn.jsonl"))
    response = _complete(backend, tmp_path)
    assert response.input_tokens == 44811
    assert response.cache_hit_tokens == 31744
    assert response.output_tokens == 224
    assert response.total_tokens == (44811 - 31744) + 224


def test_reasoning_share_lands_on_the_response_as_a_subset_of_output(tmp_path):
    backend, _ = _backend(_fixture("reasoning_turn.jsonl"))
    response = _complete(backend, tmp_path)
    assert response.reasoning_tokens == 148
    assert response.reasoning_tokens <= response.output_tokens
    backend, _ = _backend(_fixture("trivial_turn.jsonl"))
    assert _complete(backend, tmp_path).reasoning_tokens == 0


def test_reasoning_tokens_defaults_to_zero_and_has_no_positional_slot():
    from llm_kit.completion.types import LLMResponse

    assert LLMResponse(text="t", model="m").reasoning_tokens == 0
    # Appended after every existing field, so no positional construction moves.
    assert [f.name for f in dataclasses.fields(LLMResponse)][-1] == "reasoning_tokens"


def test_the_answer_still_comes_from_the_output_file(tmp_path):
    # The stream's agent_message is "403" and the -o file says otherwise: the
    # file is the answer source.
    backend, _ = _backend(_fixture("reasoning_turn.jsonl"), answer="from the file")
    assert _complete(backend, tmp_path).text == "from the file"


def test_a_stream_without_usage_keeps_honest_zeros(tmp_path):
    backend, _ = _backend('{"type":"turn.started"}\n')
    response = _complete(backend, tmp_path)
    assert (response.input_tokens, response.output_tokens, response.cache_hit_tokens) == (0, 0, 0)
    assert response.total_tokens == 0


def test_a_stream_without_usage_still_reads_the_stderr_total(tmp_path):
    backend, _ = _backend("", stderr="tokens used\n14,214\n")
    response = _complete(backend, tmp_path)
    assert response.total_tokens == 14214
    assert response.input_tokens == 0


def test_a_malformed_usage_event_fails_the_call_loudly(tmp_path):
    backend, _ = _backend(_turn({"input_tokens": "lots", "output_tokens": 1}))
    with pytest.raises(ValueError, match="turn.completed usage field"):
        _complete(backend, tmp_path)
