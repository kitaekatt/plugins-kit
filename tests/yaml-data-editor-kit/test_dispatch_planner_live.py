"""Live-backend verification for the agentic dispatch planner.

Every other planner test in this directory drives ``AgenticCommentPlanner``
through ``MockBackend``, which ignores the prompt it is handed and returns
whatever the test scripted. That is real coverage of the PARSER
(``parse_grouping`` / ``parse_agentic_result``) and of the plumbing, but it
proves nothing about whether the prompts this plugin actually renders
(``planner.PLANNER_SYSTEM`` + ``planner._planner_input`` for grouping,
``adapter.AGENTIC_SYSTEM_PROMPT`` + ``units.prompt_for_payload`` for the
worker step) elicit a response a real model would produce. This file closes
that gap in two ways:

1. ``test_worker_system_prompt_describes_each_worker_response_shape`` runs
   unconditionally (no network, no credential) and checks that the agentic
   worker prompt states the JSON contract required by
   ``units.parse_agentic_result``, while the mechanical prompt remains
   unchanged.

2. ``test_live_grouping_prompt_elicits_a_parseable_partition`` is the actual
   live check the task asked for: it drives the real ``PLANNER_SYSTEM`` /
   grouping prompt, and -- when the live model happens to merge two base
   units with distinct write anchors -- also drives the real worker
   ``AGENTIC_SYSTEM_PROMPT`` / ``prompt_for_payload`` prompt for that merged
   unit, all through ``content_pipeline.llm.backends.route()`` exactly as
   ``AgenticCommentPlanner`` calls it.

   Two live transports are supported, tried in this order:

   - ``openrouter`` -- an OpenAI-compatible HTTP completion. Needs an
     OpenRouter key (see ``llm-scripting-kit status`` / the
     ``openrouter-account`` skill) and the ``openai`` SDK importable in the
     running interpreter (deliberately excluded from this repo's ``dev``
     extra, so a live run against this transport needs an ad hoc
     interpreter, e.g. ``uv run --with openai --extra dev pytest ...``).
   - ``claude-cli`` -- the local ``claude -p`` CLI, delegated to
     ``llm_scripting_kit.completion.ClaudeCliBackend``. Needs no separate
     credential (billed at the CLI's own subscription) and no ``openai`` SDK
     import (the delegate module has no top-level ``openai`` import), so it
     runs under the plain ``uv run --extra dev pytest ...`` interpreter as
     long as a ``claude`` executable resolves on ``PATH``.

   Both transports spend a real model call, so this test is OPT-IN: it is
   skipped unless ``YDEK_LIVE_BACKEND_TESTS`` is set (see
   ``LIVE_OPT_IN_ENV``), and skipped again when neither transport is usable
   in the running interpreter. The gate exists because ``claude-cli``
   resolves on any machine with the CLI on ``PATH``, which would otherwise
   make a plain ``pytest tests/yaml-data-editor-kit`` spend a
   subscription-billed call on every run.

A response captured from a real ``claude-cli`` run that merged both base
units is recorded at
``tests/yaml-data-editor-kit/fixtures/live_grouping_merge_response.json``
(the module docstring on
``test_replay_captured_merge_response_partitions_every_anchor`` below names
the run that produced it). That fixture feeds a hermetic replay test with no
network -- the durable artifact this file exists to produce, and the only
part of this file that runs by default. Do not hand-edit that fixture's
captured text; it is verbatim model output.

That capture also settled a defect the mocks could not reach, and it is
worth being exact about how far the evidence reaches. The GROUPING response
arrives wrapped in a Markdown code fence even though ``PLANNER_SYSTEM``
forbids one, across repeated escalations of the prompt wording; the replay
test feeds those bytes to the shipped ``parse_grouping`` unmodified, so
reverting that parser's tolerance fails the suite. The WORKER response from
the same run arrives as bare JSON, so no captured bytes demonstrate the same
failure on ``units.parse_agentic_result``.

Its tolerance is therefore DEFENSIVE rather than observed: one model
demonstrably fences one of the two response shapes, both shapes are parsed
from the same model's output under prompts that forbid a fence equally, and
an asymmetry between the two parsers would turn that model's habit into a
rejected-but-valid worker result. ``test_parse_agentic_result_tolerates_one_code_fence``
pins it directly rather than leaning on the fixture to do it by accident.
"""

from __future__ import annotations

import json
import shutil
import os
import sys
from pathlib import Path
from typing import Any, Callable, Optional

import pytest

from content_pipeline.llm import submit_validated
from content_pipeline.llm.backends import ClaudeCliBackend, OpenRouterBackend

from yaml_data_editor_kit.comments import Comment
from yaml_data_editor_kit.dispatch.adapter import SYSTEM_PROMPT
from yaml_data_editor_kit.dispatch.units import AGENTIC_SYSTEM_PROMPT
from yaml_data_editor_kit.dispatch.planner import (
    CommentPlanStore,
    CommentPlanner,
    MechanicalCommentPlanner,
    PLANNER_SYSTEM,
    PlannerPolicy,
    parse_grouping,
)
from yaml_data_editor_kit.dispatch.planner import _agentic_units
from yaml_data_editor_kit.dispatch.units import (
    parse_agentic_result,
    prompt_for_payload,
    unit_targets,
)
from yaml_data_editor_kit.schema import Corpus, Profile, load_corpus, load_profile

Writer = Callable[[str, str], Path]

LIVE_MODELS = {
    "openrouter": "openai/gpt-4o-mini",
    "claude-cli": "claude-haiku-4-5",
}
"""Cheap, JSON-reliable model per transport. Keep this the ONLY model per
transport this file calls -- the task budget is one or two live calls total
per transport, not a model sweep."""

# Make llm-scripting-kit's key-resolution helper importable for the
# availability probe below. Stdlib-only module (see api_key.py's docstring),
# so this import alone never requires the `openai` SDK.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_LSK_LIB = str(_REPO_ROOT / "plugins" / "llm-scripting-kit" / "lib")
if _LSK_LIB not in sys.path:
    sys.path.insert(0, _LSK_LIB)

_FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _openrouter_unavailable_reason() -> Optional[str]:
    """Why the OpenRouter transport cannot run in this interpreter, or None."""
    try:
        from llm_scripting_kit.api_key import get_api_key  # noqa: PLC0415
    except ImportError as exc:
        return "llm-scripting-kit lib not importable: {}".format(exc)
    resolved = get_api_key()
    if not resolved.key:
        return (
            "no OpenRouter API key configured -- see the "
            "llm-scripting-kit:openrouter-account skill (llm-scripting-kit status)"
        )
    try:
        import openai  # noqa: F401,PLC0415
    except ImportError:
        return (
            "openai SDK not importable in this interpreter -- run with e.g. "
            "`uv run --with openai --extra dev pytest "
            "tests/yaml-data-editor-kit/test_dispatch_planner_live.py -v`"
        )
    return None


LIVE_OPT_IN_ENV = "YDEK_LIVE_BACKEND_TESTS"
"""Opt-in switch for every live-backend test in this file.

The claude-cli transport resolves on any machine with the ``claude`` CLI on
PATH, so without this gate a plain ``pytest tests/yaml-data-editor-kit`` would
spend a subscription-billed call on every run. The hermetic replay test below
is the durable artifact and is NOT gated -- it needs no network.
"""


def _claude_cli_unavailable_reason() -> Optional[str]:
    """Why the local claude-cli transport cannot run here, or None."""
    try:
        from llm_scripting_kit.completion import ClaudeCliBackend as _  # noqa: F401,PLC0415
    except ImportError as exc:
        return "llm-scripting-kit completion lib not importable: {}".format(exc)
    if shutil.which("claude") is None:
        return "no `claude` executable resolves on PATH"
    return None


def _live_backend_choice() -> "tuple[Optional[str], Optional[str]]":
    """Pick a usable live transport: (backend_name, None) or (None, reason).

    Tried in order -- OpenRouter first (metered, deterministic pricing),
    then the subscription-billed local claude-cli. The test is skipped only
    when neither is usable in the running interpreter.
    """
    if os.environ.get(LIVE_OPT_IN_ENV) not in ("1", "true", "TRUE", "yes"):
        return None, (
            "live-backend tests are opt-in: set {}=1 to run them. They spend a "
            "real metered or subscription-billed model call per run, so they "
            "must never fire from a default `pytest` invocation.".format(
                LIVE_OPT_IN_ENV
            )
        )
    openrouter_reason = _openrouter_unavailable_reason()
    if openrouter_reason is None:
        return "openrouter", None
    claude_cli_reason = _claude_cli_unavailable_reason()
    if claude_cli_reason is None:
        return "claude-cli", None
    return None, "openrouter: {}; claude-cli: {}".format(
        openrouter_reason, claude_cli_reason
    )


_LIVE_BACKEND, _SKIP_REASON = _live_backend_choice()
requires_live_backend = pytest.mark.skipif(
    _SKIP_REASON is not None, reason=_SKIP_REASON or ""
)


class _RecordingBackend:
    """A real live backend that records the exact bytes exchanged.

    Passed to ``CommentPlanner(backend=...)`` / ``submit_validated(backend=...)``.
    ``AgenticCommentPlanner.units`` calls ``route(mock=self.backend)``, and
    ``route()`` returns a supplied ``mock`` unconditionally (see
    ``backends.route``'s docstring) -- so this genuinely goes through CPK's
    routing seam, on the real transport, while still letting this test inspect
    what was actually sent and actually came back.
    """

    def __init__(self, backend_name: str) -> None:
        self.name = backend_name
        if backend_name == "openrouter":
            self._delegate = OpenRouterBackend()
        elif backend_name == "claude-cli":
            self._delegate = ClaudeCliBackend()
        else:
            raise ValueError("unknown live backend {!r}".format(backend_name))
        self.calls: list[dict[str, Any]] = []

    def complete(self, system, user, *, model, options=None):
        response = self._delegate.complete(system, user, model=model, options=options)
        self.calls.append(
            {
                "system": system,
                "user": user,
                "model": model,
                "response_text": response.text,
            }
        )
        return response

    def classify_halt(self, exc):
        return self._delegate.classify_halt(exc)


def _two_record_catalogue(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> tuple[Profile, Corpus]:
    """Two records of the same type -- the minimum shape that can produce a
    grouping merge across two distinct write anchors (one per record)."""
    write(
        "profile/catalogue.yaml",
        """
dialect: type/1
id: product
identified_by: id
fields:
  id: { type: id }
  name: { type: string }
  summary: { type: text }
---
dialect: source/1
of: product
layout: rows
path: content/products.yaml
""",
    )
    write(
        "content/products.yaml",
        "- { id: bolt, name: Bolt, summary: fastener }\n"
        "- { id: nut, name: Nut, summary: fastener }\n",
    )
    profile = load_profile(profile_dir)
    return profile, load_corpus(profile, tmp_path)


def _record_merge_fixture(
    *, grouping_response_text: str, worker_response_text: str, backend_name: str, model: str
) -> None:
    """Persist the verbatim live responses that produced a real merge, for
    the hermetic replay test below. Never overwrites a fixture captured by an
    earlier run with a fixture from a run that did not merge -- this is only
    called after the merge + partition assertions above already passed."""
    _FIXTURES.mkdir(exist_ok=True)
    path = _FIXTURES / "live_grouping_merge_response.json"
    path.write_text(
        json.dumps(
            {
                "backend": backend_name,
                "model": model,
                "grouping_response_text": grouping_response_text,
                "worker_response_text": worker_response_text,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _two_record_comments(profile: Profile, corpus: Corpus) -> list[Comment]:
    """The two comments used to probe the merge case, shared by the live test
    and its hermetic replay so both exercise the identical mechanical plan."""
    return [
        Comment.create(
            profile,
            corpus,
            id="bolt-note",
            anchor="product/bolt/summary",
            text="Rename this fastener's summary to 'small fastener'.",
            created="2026-09-02",
        ),
        Comment.create(
            profile,
            corpus,
            id="nut-note",
            anchor="product/nut/summary",
            text="Apply the exact same rename to this fastener's summary.",
            created="2026-09-02",
        ),
    ]


def test_worker_system_prompt_describes_each_worker_response_shape() -> None:
    """Static contract check -- no network, no credential, always runs.

    ``units.validation_spec_for_unit`` (units.py) branches on
    ``"targets" in unit.payload`` to decide whether the response must be plain
    text or the ``{"schema_version","results"}`` JSON
    ``parse_agentic_result`` requires. The shared system-prompt selector uses
    the same branch. Every unit the agentic planner produces carries "targets"
    (``planner._agentic_units`` always sets it, even for a singleton group), so
    this contract applies to every unit the agentic planner hands off.

    The agentic prompt must describe the response contract, while the mechanical
    prompt must remain byte-identical.
    """
    for marker in ("schema_version", "results", '"anchor"', "machine", "exactly one", "verbatim", "Markdown"):
        assert marker in AGENTIC_SYSTEM_PROMPT
    assert SYSTEM_PROMPT == "Transform the anchored slice according to the comments. Return only the result."


def test_parse_agentic_result_tolerates_one_code_fence() -> None:
    """A fenced worker response parses identically to the bare one.

    Hermetic. Unlike the grouping half, no captured live response
    demonstrates a fenced worker result, so nothing else in this file would
    fail if ``units.parse_agentic_result`` lost its fence tolerance. This
    test is what makes that tolerance falsifiable.
    """
    bare = json.dumps(
        {
            "schema_version": "1",
            "results": [{"anchor": "product/bolt", "machine": {"id": "bolt"}}],
        }
    )
    targets = [{"anchor": "product/bolt"}]
    expected = parse_agentic_result(bare, targets, "group:fence-probe")

    for fenced in ("```json\n{}\n```".format(bare), "```\n{}\n```".format(bare)):
        assert (
            parse_agentic_result(fenced, targets, "group:fence-probe") == expected
        ), "a fenced worker response must parse as the bare one does"


@requires_live_backend
def test_live_grouping_prompt_elicits_a_parseable_partition(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    """Run the shipped grouping prompt, and (if it merges) the shipped worker
    prompt, against a live model -- exactly the check MockBackend cannot do.

    Up to two live calls: one for grouping, one for the merged unit's worker
    step (only made when the model actually merged the two base units).
    """
    profile, corpus = _two_record_catalogue(tmp_path, profile_dir, write)
    comments = _two_record_comments(profile, corpus)
    store = CommentPlanStore(profile, corpus, comments)
    assert _LIVE_BACKEND is not None  # requires_live_backend guards this
    grouping_backend = _RecordingBackend(_LIVE_BACKEND)
    policy = PlannerPolicy(model=LIVE_MODELS[_LIVE_BACKEND], max_attempts=3)
    planner = CommentPlanner(profile, corpus, comments, backend=grouping_backend, policy=policy)

    units = planner.units(store)

    assert grouping_backend.calls, "expected the planner to call the live backend at all"
    call = grouping_backend.calls[0]
    assert call["system"] == PLANNER_SYSTEM, "test drifted from the shipped system prompt"

    # THE premise under test: did the shipped prompt, unedited, elicit a
    # response parse_grouping accepts? A silent mechanical fallback (unit ids
    # not starting "group:") means it did not.
    assert all(unit.id.startswith("group:") for unit in units), (
        "planner fell back to the mechanical plan -- the live response did "
        "not parse against parse_grouping. Captured response: {!r}".format(
            call["response_text"]
        )
    )

    merged = [unit for unit in units if len(unit.payload["targets"]) > 1]
    if not merged:
        pytest.skip(
            "live model returned {} work unit(s), none merging the two "
            "distinct-write-anchor base units -- parse_grouping accepted "
            "the response, but the merge case (task item 3) was not "
            "exercised this run. Captured response: {!r}".format(
                len(units), call["response_text"]
            )
        )

    merged_unit = merged[0]
    targets = unit_targets(merged_unit)
    anchors = {target["anchor"] for target in targets}
    assert len(anchors) == len(targets), "merge combined duplicate write anchors -- parse_grouping should have rejected this"

    # Second half of the pipeline: the SAME merged unit's worker prompt/
    # response contract for a multi-target unit whose response must partition
    # every target anchor.
    #
    # The response goes to the shipped `units.parse_agentic_result`
    # unmodified. It tolerates one Markdown code fence, as
    # `planner.parse_grouping` does, so a fenced worker response needs no
    # help from this test.
    worker_backend = _RecordingBackend(_LIVE_BACKEND)
    worker_response = worker_backend.complete(
        AGENTIC_SYSTEM_PROMPT,
        prompt_for_payload(merged_unit.payload),
        model=LIVE_MODELS[_LIVE_BACKEND],
    )
    worker_text = worker_response.text
    try:
        worker_payload = parse_agentic_result(worker_text, targets, merged_unit.id)
    except ValueError as exc:
        pytest.fail(
            "worker prompt did not elicit a response parse_agentic_result accepts "
            "(after stripping a Markdown code fence, if any). The agentic system "
            "prompt may not describe the required contract. Error: {!r}. "
            "Captured response: {!r}".format(exc, worker_response.text)
        )

    result_anchors = {item["anchor"] for item in worker_payload["results"]}
    assert result_anchors == anchors, (
        "worker response did not partition every target anchor -- "
        "expected {!r}, got {!r}".format(anchors, result_anchors)
    )

    _record_merge_fixture(
        grouping_response_text=call["response_text"],
        worker_response_text=worker_response.text,
        backend_name=_LIVE_BACKEND,
        model=LIVE_MODELS[_LIVE_BACKEND],
    )


def test_replay_captured_merge_response_partitions_every_anchor(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    """Hermetic replay of a REAL merge -- no network, no credential, always runs.

    ``tests/yaml-data-editor-kit/fixtures/live_grouping_merge_response.json``
    holds the verbatim ``claude-cli`` / ``claude-haiku-4-5`` responses captured
    by ``test_live_grouping_prompt_elicits_a_parseable_partition`` on a run
    where the model merged the two distinct-write-anchor base units. This test
    feeds those exact bytes through the shipped parsers
    (``planner.parse_grouping`` for the grouping response, then
    ``units.parse_agentic_result`` for the worker response, fence-stripped the
    same way the live test strips it -- see the comment above the worker half
    of that test for why ``units.py`` itself needs no edit here) and asserts
    the merge invariants: distinct write anchors are preserved, and the
    worker response partitions every target anchor exactly once. This is the
    durable artifact the live check exists to produce -- it must keep passing
    with no network access.
    """
    fixture_path = _FIXTURES / "live_grouping_merge_response.json"
    if not fixture_path.exists():
        pytest.fail(
            "no captured merge fixture at {} -- run "
            "test_live_grouping_prompt_elicits_a_parseable_partition with a "
            "live backend available to (re)capture one".format(fixture_path)
        )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))

    profile, corpus = _two_record_catalogue(tmp_path, profile_dir, write)
    comments = _two_record_comments(profile, corpus)
    store = CommentPlanStore(profile, corpus, comments)
    mechanical = MechanicalCommentPlanner(profile, corpus, comments).units(store)

    grouping = parse_grouping(fixture["grouping_response_text"], mechanical)
    units = _agentic_units(grouping, mechanical)

    merged = [unit for unit in units if len(unit.payload["targets"]) > 1]
    assert merged, "captured fixture's grouping response no longer merges -- was it replaced?"
    merged_unit = merged[0]
    targets = unit_targets(merged_unit)
    anchors = {target["anchor"] for target in targets}
    assert len(anchors) == len(targets), "merge combined duplicate write anchors"
    assert len(anchors) == 2, "expected exactly the two base units' distinct write anchors"

    worker_text = fixture["worker_response_text"]
    worker_payload = parse_agentic_result(worker_text, targets, merged_unit.id)
    result_anchors = {item["anchor"] for item in worker_payload["results"]}
    assert result_anchors == anchors, "worker response did not partition every target anchor"
