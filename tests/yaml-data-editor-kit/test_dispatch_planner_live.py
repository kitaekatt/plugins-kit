"""Live-backend verification for the agentic dispatch planner.

Every other planner test in this directory drives ``AgenticCommentPlanner``
through ``MockBackend``, which ignores the prompt it is handed and returns
whatever the test scripted. That is real coverage of the PARSER
(``parse_grouping`` / ``parse_agentic_result``) and of the plumbing, but it
proves nothing about whether the prompts this plugin actually renders
(``planner.PLANNER_SYSTEM`` + ``planner._planner_input`` for grouping,
``adapter.SYSTEM_PROMPT`` + ``units.prompt_for_payload`` for the worker step)
elicit a response a real model would produce. This file closes that gap in
two ways:

1. ``test_worker_system_prompt_describes_each_worker_response_shape`` runs
   unconditionally (no network, no credential) and checks that the agentic
   worker prompt states the JSON contract required by
   ``units.parse_agentic_result``, while the mechanical prompt remains
   unchanged.

2. ``test_live_grouping_prompt_elicits_a_parseable_partition`` is the actual
   live check the task asked for: it drives the real ``PLANNER_SYSTEM`` /
   grouping prompt, and -- when the live model happens to merge two base
   units with distinct write anchors -- also drives the real worker
   ``SYSTEM_PROMPT`` / ``prompt_for_payload`` prompt for that merged unit, all
   through ``content_pipeline.llm.backends.route()`` exactly as
   ``AgenticCommentPlanner`` calls it. It is SKIPPED BY DEFAULT: it needs an
   OpenRouter key (none was configured on the machine this file was written
   on -- see ``llm-scripting-kit status`` / the ``openrouter-account`` skill)
   and the ``openai`` SDK importable in the running interpreter (deliberately
   excluded from this repo's ``dev`` extra; the plugin-provisioned venvs at
   ``~/.claude/plugins/data/plugins-kit/{llm-scripting-kit,content-pipeline-kit}/.venv``
   have it, but lack ``pytest`` and this repo's own packages, so a live run
   needs an ad hoc interpreter, e.g.::

       uv run --with openai --extra dev pytest \\
           tests/yaml-data-editor-kit/test_dispatch_planner_live.py -v

   with ``OPENROUTER_API_KEY`` set or the key resolved via llm-scripting-kit's
   normal .env precedence.

No REAL response has ever been captured on this machine (the key was never
configured), so there is deliberately no recorded-fixture replay test here --
writing one would mean fabricating a "captured" response, which is exactly
what this file exists to refuse to do. Once someone runs the live test with a
working key, capture the ``backend.calls`` it prints/records into a fixture
and add a hermetic replay test alongside it.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable, Optional

import pytest

from content_pipeline.llm import submit_validated
from content_pipeline.llm.backends import OpenRouterBackend

from yaml_data_editor_kit.comments import Comment
from yaml_data_editor_kit.dispatch.adapter import SYSTEM_PROMPT
from yaml_data_editor_kit.dispatch.units import AGENTIC_SYSTEM_PROMPT
from yaml_data_editor_kit.dispatch.planner import (
    CommentPlanStore,
    CommentPlanner,
    PLANNER_SYSTEM,
    PlannerPolicy,
)
from yaml_data_editor_kit.dispatch.units import (
    parse_agentic_result,
    prompt_for_payload,
    unit_targets,
)
from yaml_data_editor_kit.schema import Corpus, Profile, load_corpus, load_profile

Writer = Callable[[str, str], Path]

LIVE_MODEL = "openai/gpt-4o-mini"
"""Cheap, JSON-reliable OpenRouter model. Keep this the ONLY model this file
calls -- the task budget is one or two live calls total, not a model sweep."""

# Make llm-scripting-kit's key-resolution helper importable for the
# availability probe below. Stdlib-only module (see api_key.py's docstring),
# so this import alone never requires the `openai` SDK.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_LSK_LIB = str(_REPO_ROOT / "plugins" / "llm-scripting-kit" / "lib")
if _LSK_LIB not in sys.path:
    sys.path.insert(0, _LSK_LIB)


def _live_skip_reason() -> Optional[str]:
    """Why a live call cannot run in this interpreter, or None when it can."""
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


_SKIP_REASON = _live_skip_reason()
requires_live_backend = pytest.mark.skipif(
    _SKIP_REASON is not None, reason=_SKIP_REASON or ""
)


class _RecordingBackend:
    """A real ``OpenRouterBackend`` that records the exact bytes exchanged.

    Passed to ``CommentPlanner(backend=...)`` / ``submit_validated(backend=...)``.
    ``AgenticCommentPlanner.units`` calls ``route(mock=self.backend)``, and
    ``route()`` returns a supplied ``mock`` unconditionally (see
    ``backends.route``'s docstring) -- so this genuinely goes through CPK's
    routing seam, on the real transport, while still letting this test inspect
    what was actually sent and actually came back.
    """

    name = "openrouter"

    def __init__(self) -> None:
        self._delegate = OpenRouterBackend()
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
    comments = [
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
    store = CommentPlanStore(profile, corpus, comments)
    grouping_backend = _RecordingBackend()
    policy = PlannerPolicy(model=LIVE_MODEL, max_attempts=3)
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
    worker_backend = _RecordingBackend()
    result = submit_validated(
        backend=worker_backend,
        system=AGENTIC_SYSTEM_PROMPT,
        user=prompt_for_payload(merged_unit.payload),
        model=LIVE_MODEL,
        parse_fn=lambda text: parse_agentic_result(text, targets, merged_unit.id),
        validators=(),
        max_attempts=1,
    )

    if not result.accepted:
        captured = worker_backend.calls[-1]["response_text"] if worker_backend.calls else None
        pytest.fail(
            "worker prompt did not elicit a response parse_agentic_result accepts. "
            "The agentic system prompt may not describe the required contract. "
            "Captured response: {!r}".format(captured)
        )
