"""Canonical reviewer-lane prompts, output schema, and dispatch classification.

WHY THIS MODULE EXISTS. Before it, there were no reusable reviewer "prompt
bodies" at all: each code-review SKILL.md carried a DESCRIPTION of a reviewer
(scope / input / restrictions) that the main session turned into an Agent prompt
ad hoc, per run. That is fine while every lane is an Agent call, because one
model reads the description and writes the prompt. It stops being fine the
moment a lane can also run as a plain completion against a configured endpoint,
because then something has to build an actual prompt string -- and if that
string lives only in the runner, the Agent path and the endpoint path drift
apart silently, reviewing the same diff by two different standards.

So the text lives HERE, once, and both paths consume it:

  * the endpoint path imports it (``lane_runner``);
  * the Agent path gets it rendered into both SKILL.md files by
    ``scripts/gen_code_review_skills.py``, whose output is pinned byte-for-byte
    by ``tests/bootstrap/code_review/test_skill_drift.py``.

Editing a prompt here and not regenerating therefore FAILS THE SUITE, which is
the property we want: the two dispatch paths cannot disagree by accident.

DELIBERATELY NO SEAM IMPORT. This module is data plus pure functions --
stdlib only, no ``llm_scripting_kit``, no ``openai``. ``bootstrap_lib`` is
linked into many plugin venvs that will never make an LLM call, and importing a
completion transport here would make ``openai`` a transitive requirement of the
BOOTSTRAP plugin itself -- the one every other plugin depends on. The seam call
therefore lives outside this package entirely, in each kit's vendored
``scripts/run_review_lane.py``. The boundary is enforced by
``tests/bootstrap/test_dependency_completeness.py`` and asserted directly by
``tests/bootstrap/test_run_review_lane_drift.py``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


# --------------------------------------------------------------------------
# Model classification
# --------------------------------------------------------------------------

# The Agent tool's model enum. A `model` value in a resolved review profile that
# is one of these names the NATIVE path (an Agent subagent);
# anything else is read as an llm-scripting-kit endpoint id and dispatched
# through the completion seam. This is the whole override mechanism -- there is
# no new configuration field, because `model` already exists, is already a
# free-form string, and is already resolved through the three review-profile
# layers.
#
# The set is deliberately CLOSED and deliberately not read from the harness: a
# typo ("sonnett") must fall through to the endpoint path and fail loudly with
# "no such endpoint", not silently launch some default Agent.
AGENT_MODEL_ALIASES = frozenset({"sonnet", "opus", "haiku", "fable"})


def is_agent_alias(model: str) -> bool:
    """Return whether ``model`` names the native Agent-tool path."""
    return model.strip() in AGENT_MODEL_ALIASES


# Lanes that may run on a configured endpoint. The set is deliberately
# ONE reviewer: the diff-only lane, whose entire input is the chunk text and
# which is the only reviewer that both needs no repository access and is not
# the false-positive control.
#
# The validator is NOT here on purpose. It is the control that suppresses a
# weak reviewer's noise; replacing it in the same phase as a reviewer would
# remove the instrument the reviewer change has to be measured with.
ENDPOINT_ELIGIBLE_LANES = frozenset({"reviewer_b_diff_only_bugs"})

# Every lane the review pipeline runs, eligible or not. Kept so a lane that
# exists but may not take an endpoint id ("validator") is refused for the RIGHT
# reason -- "not eligible" -- rather than reported as an unknown name, which
# would read as a typo and send the user looking for one.
KNOWN_LANES = frozenset(
    {
        "reviewer_a_claude_md_compliance",
        "reviewer_b_diff_only_bugs",
        "reviewer_c_introduced_code",
        "validator",
    }
)

# Lanes whose prompt tells the model to read files beyond its chunk. These need
# an agent loop, so a `transport`-kind selection (a raw /v1 completion) cannot
# serve them and the runner refuses rather than producing a reviewer that
# hallucinates the context it cannot fetch. Kept as immutable code, NOT as an
# overridable config field: it is a fact about what the lane's prompt asks for,
# not a policy the user gets to state.
LANES_REQUIRING_AGENT_LOOP = frozenset({"reviewer_c_introduced_code"})


# --------------------------------------------------------------------------
# Output contract
# --------------------------------------------------------------------------

# A REAL JSON Schema for a reviewer lane's output. The `issue_format` block in
# SKILL.md is illustrative prose -- `"bug" | "claude_md"` is not valid JSON and
# cannot be validated against -- so machine checking needs this instead.
ISSUE_ARRAY_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "array",
    "items": {
        "type": "object",
        "additionalProperties": False,
        "required": ["file", "lines", "reason", "description"],
        "properties": {
            "file": {"type": "string", "minLength": 1},
            "lines": {"type": "string", "minLength": 1},
            "reason": {"type": "string", "enum": ["bug", "claude_md"]},
            "description": {"type": "string", "minLength": 1},
            "citation": {"type": "string"},
        },
    },
}

_REQUIRED_ISSUE_FIELDS = ("file", "lines", "reason", "description")
_ALLOWED_ISSUE_FIELDS = _REQUIRED_ISSUE_FIELDS + ("citation",)
_ALLOWED_REASONS = ("bug", "claude_md")


class LaneOutputError(ValueError):
    """A lane's response did not satisfy the issue-array contract."""


def _strip_code_fence(text: str) -> str:
    """Return ``text`` without a surrounding Markdown code fence.

    Models that are asked for bare JSON still wrap it in a fence often enough
    that refusing the response outright would spend a retry on formatting
    rather than on substance. Unwrapping is the one repair applied before the
    contract is enforced literally.
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if len(lines) < 2:
        return stripped
    body = lines[1:]
    if body and body[-1].strip().startswith("```"):
        body = body[:-1]
    return "\n".join(body).strip()


def parse_issue_array(text: str) -> list[dict[str, Any]]:
    """Parse and validate a reviewer lane's response.

    Raises ``LaneOutputError`` with a reason a human can act on. An empty
    response is an error, not an empty finding list: "the model said nothing"
    and "the model found nothing" are different outcomes, and only the second
    one may be rendered as a clean review.
    """
    candidate = _strip_code_fence(text)
    if not candidate:
        raise LaneOutputError("empty response (no text returned)")
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError as exc:
        preview = candidate[:200].replace("\n", " ")
        raise LaneOutputError(
            f"response is not valid JSON ({exc.msg} at line {exc.lineno}); "
            f"first 200 chars: {preview!r}"
        ) from exc
    if not isinstance(value, list):
        raise LaneOutputError(
            f"response must be a JSON array of issues, got {type(value).__name__}"
        )
    issues: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        issues.append(_validate_issue(item, index))
    return issues


def _validate_issue(item: Any, index: int) -> dict[str, Any]:
    """Validate one issue record against the schema above."""
    where = f"issue[{index}]"
    if not isinstance(item, Mapping):
        raise LaneOutputError(f"{where} must be an object, got {type(item).__name__}")
    unknown = sorted(key for key in item if key not in _ALLOWED_ISSUE_FIELDS)
    if unknown:
        raise LaneOutputError(f"{where} has unknown field(s): {unknown}")
    for field in _REQUIRED_ISSUE_FIELDS:
        if field not in item:
            raise LaneOutputError(f"{where} is missing required field {field!r}")
    for field in _ALLOWED_ISSUE_FIELDS:
        if field in item and not isinstance(item[field], str):
            raise LaneOutputError(f"{where}.{field} must be a string")
    if item["reason"] not in _ALLOWED_REASONS:
        raise LaneOutputError(
            f"{where}.reason must be one of {list(_ALLOWED_REASONS)}, "
            f"got {item['reason']!r}"
        )
    for field in _REQUIRED_ISSUE_FIELDS:
        if not item[field].strip():
            raise LaneOutputError(f"{where}.{field} must not be empty")
    return {key: item[key] for key in _ALLOWED_ISSUE_FIELDS if key in item}


# --------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------

# Bumped whenever any prompt text below changes, so a recorded lane result says
# which wording produced it. A comparison across prompt versions is not a
# like-for-like measurement, and without this the difference is invisible.
PROMPT_VERSION = "1"


# The false-positive guardrails, stated once. These are the same rules the
# SKILL.md `false_positive_guardrails` block states for the Agent path; both
# paths render from this string.
GUARDRAILS = """\
Only flag an issue when it is one of these:
- code that will fail to compile or parse (syntax errors, type errors, missing
  imports, unresolved references)
- code that will definitely produce wrong results regardless of inputs (clear
  logic errors)
- a project-standard rule clearly and unambiguously violated, with the exact
  rule quotable

Never flag any of these:
- code style or quality concerns
- potential issues that depend on specific inputs or state
- subjective suggestions or improvements
- pre-existing issues (only review the diff)
- anything a linter would catch (do not run a linter)
- issues that appear in a standards file but are explicitly silenced in the
  code (for example a lint-ignore comment)

If you are not certain an issue is real, do not flag it. False positives erode
trust: an empty array is a perfectly good answer and is much better than a
speculative finding."""


OUTPUT_INSTRUCTION = """\
Respond with a JSON array and nothing else. No prose before it, no prose after
it, no Markdown code fence. Each element is an object with exactly these keys:

  "file"        the path of the file the issue is in, as it appears in the diff
  "lines"       the affected line or range, for example "42" or "42-48"
  "reason"      exactly "bug" or "claude_md"
  "description" one sentence explaining the problem
  "citation"    optional, and only for "claude_md": the exact rule text quoted

Return [] when there is nothing to report."""


@dataclass(frozen=True)
class LanePrompt:
    """The canonical prompt for one reviewer lane."""

    lane: str
    system: str
    user_preamble: str


REVIEWER_B_SYSTEM = f"""\
You are reviewing one chunk of a code change for obvious bugs that are visible
in the diff alone. You are one of several independent reviewers; other files
and other concerns belong to other reviewers.

Scope. Report only won't-compile problems, syntax and type errors, missing
imports, unresolved references, and logic that is definitely wrong regardless
of inputs. For data and documentation files, report malformed syntax, duplicate
keys, schema or column-count violations, and broken cross-file references.

Restrictions. The diff below is everything you get and everything you may
consider. Do not ask for other files, do not reason about code you cannot see,
and do not report an issue in a file that does not appear in this diff.

{GUARDRAILS}

{OUTPUT_INSTRUCTION}"""


LANE_PROMPTS: dict[str, LanePrompt] = {
    "reviewer_b_diff_only_bugs": LanePrompt(
        lane="reviewer_b_diff_only_bugs",
        system=REVIEWER_B_SYSTEM,
        user_preamble="Review this diff chunk.",
    ),
}


def build_user_message(
    lane: str,
    *,
    diff_text: str,
    files: Sequence[str] = (),
    description: str = "",
) -> str:
    """Assemble the user message for a lane.

    The diff is INLINED rather than referenced by path: an endpoint-dispatched
    lane is a plain completion with no file access, so a path would name
    something it cannot open.
    """
    prompt = LANE_PROMPTS.get(lane)
    if prompt is None:
        raise KeyError(f"no canonical prompt for lane {lane!r}")
    parts = [prompt.user_preamble]
    if description:
        parts.append(f"Change description: {description}")
    if files:
        parts.append("Files in this chunk:\n" + "\n".join(f"- {f}" for f in files))
    parts.append("Diff:\n" + diff_text)
    return "\n\n".join(parts)


__all__ = [
    "AGENT_MODEL_ALIASES",
    "ENDPOINT_ELIGIBLE_LANES",
    "GUARDRAILS",
    "KNOWN_LANES",
    "ISSUE_ARRAY_SCHEMA",
    "LANES_REQUIRING_AGENT_LOOP",
    "LANE_PROMPTS",
    "LaneOutputError",
    "LanePrompt",
    "OUTPUT_INSTRUCTION",
    "PROMPT_VERSION",
    "REVIEWER_B_SYSTEM",
    "build_user_message",
    "is_agent_alias",
    "parse_issue_array",
]
