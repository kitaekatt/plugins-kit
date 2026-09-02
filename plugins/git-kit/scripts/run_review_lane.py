#!/usr/bin/env python3
"""Run ONE code-review reviewer lane against a configured endpoint.

CANONICAL SOURCE + VENDORED COPY. This file is byte-identical in git-kit and
p4-kit, the way bootstrap_guard.py is: both kits run the same lanes with the
same prompts, and a hand-maintained second copy is exactly how the two review
skills drifted before (see scripts/gen_code_review_skills.py). A drift test
asserts the two match. Nothing here names its own plugin -- the plugin id is
read from this file's location -- which is what lets the copies stay identical.

WHY IT IS NOT IN bootstrap_lib, where the rest of the shared review pipeline
lives. It calls llm_scripting_kit, and bootstrap_lib is linked into many plugin
venvs that will never make an LLM call. Putting it there makes `openai` a
transitive requirement of the BOOTSTRAP plugin itself -- the one every other
plugin depends on -- which tests/bootstrap/test_dependency_completeness.py
correctly rejects. Only the VCS-neutral, LLM-neutral half (prompts, the issue
schema, dispatch classification) lives in bootstrap_lib, as
`bootstrap_lib.code_review.lane_prompts`, and both dispatch paths read it.

This is the endpoint half of the code-review dispatch. The native half is
unchanged and does not come through here at all: when a resolved review
profile's `model` is an Agent-tool alias, the skill launches an Agent subagent
as it does with no override. Only a NON-alias `model` -- read as an
llm-scripting-kit endpoint id -- reaches this script.

FAILURE IS LOUD AND FINAL. There is no fallback to the Agent path. A silent
fallback would hand back a review the user reads as the configured one, which
is a measurement lie -- the one outcome worse than no review. A lane that
cannot run exits non-zero with a reason, and the skill renders the review with
that lane's coverage marked missing.

Usage:
    run_review_lane.py --lane <name> --model <endpoint id> --chunk <path> \
        [--file <path>]... [--description <text>] [--project-root <path>]

Stdout: the lane result envelope as JSON.

Exit codes:
    0  the lane ran and its output satisfied the contract
    1  the lane failed (endpoint unreachable, halted, over budget, or output
       that did not satisfy the contract after one repair attempt)
    2  usage or configuration error (unknown lane, ineligible lane, a model
       that is an Agent alias, an endpoint that cannot serve the lane)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Optional, Sequence

# Plugins define their own bootstrap-provisioned venv and must run under it
# preferentially. A bare `python` or `uv run` invocation lands in a different
# environment with no shared-libs .pth, so re-exec under the provisioned venv
# before importing bootstrap_lib below -- a no-op when already there. The plugin
# id comes from this file's own path (`<plugin>/scripts/run_review_lane.py`) so
# the two vendored copies stay byte-identical.
from bootstrap_guard import reexec_under_plugin_venv  # noqa: E402

_PLUGIN_NAME = Path(__file__).resolve().parents[1].name

reexec_under_plugin_venv(_PLUGIN_NAME)

try:
    from bootstrap_lib.path_repair import repair_path  # noqa: E402
    from bootstrap_lib.code_review.lane_prompts import (  # noqa: E402
        ENDPOINT_ELIGIBLE_LANES,
        KNOWN_LANES,
        LANE_PROMPTS,
        LANES_REQUIRING_AGENT_LOOP,
        PROMPT_VERSION,
        LaneOutputError,
        build_user_message,
        is_agent_alias,
        parse_issue_array,
    )
except ImportError:
    from bootstrap_guard import require_bootstrap

    require_bootstrap(
        _PLUGIN_NAME, feature="code review", missing="bootstrap_lib", force=True
    )

repair_path()


EXIT_OK = 0
EXIT_LANE_FAILED = 1
EXIT_USAGE = 2

# Tokens are estimated from characters because no tokenizer is available here
# and pulling one in for a pre-flight check would be a heavy dependency for a
# guard rail. 3.0 chars/token is deliberately PESSIMISTIC for diff text (code
# and paths tokenize worse than prose), so the check errs toward refusing a
# borderline chunk rather than sending one that will be truncated server-side.
# Truncation is the failure this guard exists to prevent: it is invisible, and
# a reviewer silently shown half a diff reports a clean chunk.
CHARS_PER_TOKEN = 3.0

# Held back from the context window for the model's own answer.
DEFAULT_MAX_OUTPUT_TOKENS = 4096

# Reviewers are graded on consistency, not creativity, so the sampling
# temperature is pinned to 0 rather than inheriting the seam's 0.3 general
# default. Two runs over one diff that disagree are not two opinions, they are
# an unreproducible result.
REVIEW_TEMPERATURE = 0.0

_REPAIR_SUFFIX = (
    "\n\nYour previous response did not parse as the required JSON array. "
    "Respond again with ONLY the JSON array, starting with [ and ending with "
    "]. No prose, no code fence."
)


class LaneConfigError(Exception):
    """The lane cannot be dispatched as configured."""


class LaneRunError(Exception):
    """The lane was dispatched and did not produce a usable result."""


def _estimate_tokens(text: str) -> int:
    """Estimated prompt tokens for ``text``. See CHARS_PER_TOKEN."""
    return int(len(text) / CHARS_PER_TOKEN) + 1


def _endpoint_context_window(endpoint: str, project_root: Optional[str]) -> Optional[int]:
    """Return the configured context window for ``endpoint``, when it states one.

    ``None`` means the entry declares no window, in which case the budget check
    is skipped -- an unknown limit must not be invented, and a guess that is too
    small would refuse work the endpoint could have done.
    """
    from llm_scripting_kit.models import discover_model_entries  # noqa: PLC0415

    try:
        entries = discover_model_entries(project_root=project_root)
    except Exception:  # pragma: no cover - discovery is best-effort here
        return None
    entry = entries.get(endpoint)
    return getattr(entry, "context_window", None) if entry is not None else None


def check_lane_dispatchable(lane: str, model: str) -> None:
    """Raise ``LaneConfigError`` unless this lane may run on this model.

    Both refusals below are about what the LANE needs, never about user policy,
    which is why neither is configurable.
    """
    if is_agent_alias(model):
        raise LaneConfigError(
            f"model {model!r} is an Agent-tool alias -- lane {lane!r} should have "
            "been launched as an Agent subagent, not routed through this runner"
        )
    if lane not in KNOWN_LANES:
        raise LaneConfigError(
            f"lane {lane!r} is not a review lane; known lanes: {sorted(KNOWN_LANES)}"
        )
    # Eligibility is checked BEFORE the prompt lookup so an ineligible lane is
    # refused for the reason the user can act on. Reporting "no canonical
    # prompt" for `validator` would be true and useless -- it reads as a typo.
    if lane not in ENDPOINT_ELIGIBLE_LANES:
        raise LaneConfigError(
            f"lane {lane!r} is not eligible for endpoint dispatch; eligible "
            f"lanes: {sorted(ENDPOINT_ELIGIBLE_LANES)}. Give it an Agent-tool "
            "model (sonnet, opus, haiku, fable) in the review profile."
        )
    if lane not in LANE_PROMPTS:  # pragma: no cover - guarded by the test below
        raise LaneConfigError(
            f"lane {lane!r} is eligible but has no canonical prompt; this is a "
            "bug in lane_prompts, not a configuration error"
        )


def _check_selection(lane: str, selection: Any) -> None:
    """Refuse a backend that structurally cannot serve the lane."""
    if lane in LANES_REQUIRING_AGENT_LOOP and selection.kind != "harness":
        raise LaneConfigError(
            f"lane {lane!r} reads files beyond its chunk and needs an agent loop, "
            f"but endpoint {selection.endpoint!r} is a {selection.kind} entry "
            "(a plain completion). Use a harness endpoint or an Agent-tool model."
        )


def _check_transport_sdk(selection: Any) -> None:
    """Refuse a transport selection when the ``openai`` SDK is not importable.

    A transport entry is a plain OpenAI-compatible completion, and the seam
    builds its client lazily -- on the first request, inside the backend. So a
    missing SDK surfaces as a bare ImportError raised past every guard this
    runner has, after the prompt is built and the budget checked, which reads
    as a crash rather than as the configuration gap it is.

    `openai` is DELIBERATELY not a hard dependency of this kit: only a profile
    that names a non-Agent transport `model` ever reaches this code path, so
    its absence is the default state and has to be reported by name.

    A harness entry shells out to a CLI that carries its own client, so it is
    exempt -- checking it would refuse a lane that would have run.
    """
    if selection.kind != "transport":
        return
    try:
        import openai  # noqa: F401, PLC0415
    except ImportError as exc:
        raise LaneConfigError(
            f"endpoint {selection.endpoint!r} is a transport entry (a plain "
            "OpenAI-compatible completion) and needs the 'openai' package, "
            "which this plugin declares in the OPTIONAL 'endpoint-dispatch' "
            "dependency group rather than as a requirement. Declare that group "
            'in the plugin\'s bootstrap.json ("venv": {"extras": '
            '["endpoint-dispatch"]}) and let the bootstrap engine reprovision '
            "the venv, or configure a harness endpoint (which shells out to a "
            "CLI and needs no SDK) or an Agent-tool model instead. Underlying "
            f"error: {exc}"
        ) from exc


def _allowed_tools_for(lane: str) -> Optional[str]:
    """The `allowed_tools` value a lane needs, or None for a pure completion.

    Passing the harness check is NOT the same as being handed tools. The codex
    and opencode adapters run their own agent loop and drop this parameter, but
    claude-cli emits `--allowedTools ""` for a None value -- a deliberate
    allow-NOTHING list, not an absent flag -- so a lane admitted as needing an
    agent loop would still reach the model as a tool-less completion, holding a
    prompt that tells it to read files it cannot open. That is precisely the
    hallucination LANES_REQUIRING_AGENT_LOOP exists to prevent, so the guard has
    to grant the capability it just finished checking for.

    `Read` and nothing else: these lanes inspect the changed files' surrounding
    context, which is the read-only use the seam sanctions. A reviewer has no
    business writing or running anything.
    """
    return "Read" if lane in LANES_REQUIRING_AGENT_LOOP else None


def run_lane(
    *,
    lane: str,
    model: str,
    diff_text: str,
    files: Sequence[str] = (),
    description: str = "",
    project_root: Optional[str] = None,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    timeout_s: Optional[float] = None,
) -> dict[str, Any]:
    """Run one reviewer lane and return its result envelope.

    Raises ``LaneConfigError`` for a dispatch that must not be attempted and
    ``LaneRunError`` for one that was attempted and failed.
    """
    check_lane_dispatchable(lane, model)

    try:
        from llm_scripting_kit.completion import (  # noqa: PLC0415
            BackendOptions,
            HaltError,
            create_backend,
        )
    except ImportError as exc:  # pragma: no cover - exercised by venv wiring
        raise LaneConfigError(
            "llm_scripting_kit is not available in this plugin's venv. The "
            "plugin must declare it in bootstrap.json "
            '("shared_lib_imports": ["bootstrap_lib", "llm_scripting_kit"]) '
            "and declare 'openai' in pyproject.toml, then be reprovisioned by "
            f"the bootstrap engine. Underlying error: {exc}"
        ) from exc

    from llm_scripting_kit.models import EndpointResolveError  # noqa: PLC0415

    try:
        selection = create_backend(model, project_root=project_root)
    except EndpointResolveError as exc:
        raise LaneConfigError(
            f"model {model!r} is neither an Agent-tool alias nor a known "
            f"llm-scripting-kit endpoint id: {exc}"
        ) from exc
    _check_selection(lane, selection)
    _check_transport_sdk(selection)

    system = LANE_PROMPTS[lane].system
    user = build_user_message(
        lane, diff_text=diff_text, files=files, description=description
    )

    window = _endpoint_context_window(selection.endpoint, project_root)
    estimate = _estimate_tokens(system) + _estimate_tokens(user)
    if window is not None and estimate + max_output_tokens > window:
        raise LaneRunError(
            f"chunk does not fit endpoint {selection.endpoint!r}: about "
            f"{estimate} prompt tokens plus {max_output_tokens} reserved for "
            f"output exceeds its {window}-token context window. Review this "
            "range on an endpoint with a larger window, or on an Agent-tool "
            "model."
        )

    options = BackendOptions(
        max_tokens=max_output_tokens,
        temperature=REVIEW_TEMPERATURE,
        timeout_s=timeout_s,
        effort=selection.effort,
        allowed_tools=_allowed_tools_for(lane),
        log_prefix=f"[review:{lane}]",
    )

    started = time.monotonic()
    attempts = 0
    last_error: Optional[str] = None
    response = None
    issues: list[dict[str, Any]] = []
    # One repair attempt, and exactly one: a model that ignores the output
    # contract twice is not going to honor it on a third ask, and an unbounded
    # loop turns a formatting failure into an unbounded bill.
    for attempt in range(2):
        attempts = attempt + 1
        message = user if attempt == 0 else user + _REPAIR_SUFFIX
        try:
            response = selection.backend.complete(
                system, message, model=selection.model, options=options
            )
        except HaltError as exc:
            raise LaneRunError(
                f"endpoint {selection.endpoint!r} halted ({exc}); it is not "
                "serving this run"
            ) from exc
        except Exception as exc:
            raise LaneRunError(
                f"endpoint {selection.endpoint!r} failed: {type(exc).__name__}: {exc}"
            ) from exc
        try:
            issues = parse_issue_array(response.text)
            last_error = None
            break
        except LaneOutputError as exc:
            last_error = str(exc)
            options = BackendOptions(
                max_tokens=options.max_tokens,
                temperature=options.temperature,
                timeout_s=options.timeout_s,
                effort=options.effort,
                allowed_tools=options.allowed_tools,
                log_prefix=options.log_prefix,
                cache_salt=attempt + 1,
            )
    if last_error is not None:
        finish = getattr(response, "finish_reason", None)
        hint = (
            " The response stopped on 'length', so the output budget was "
            "exhausted -- raise --max-output-tokens."
            if finish == "length"
            else ""
        )
        raise LaneRunError(
            f"endpoint {selection.endpoint!r} did not return a valid issue array "
            f"after {attempts} attempt(s): {last_error}.{hint}"
        )

    return {
        "lane": lane,
        "configured_model": model,
        "endpoint": selection.endpoint,
        "backend": getattr(selection.backend, "name", "unknown"),
        "served_model": getattr(response, "model", selection.model) or selection.model,
        "prompt_version": PROMPT_VERSION,
        "attempts": attempts,
        "input_tokens": getattr(response, "input_tokens", 0),
        "output_tokens": getattr(response, "output_tokens", 0),
        "wall_ms": int((time.monotonic() - started) * 1000),
        "issues": issues,
    }


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run_review_lane",
        description="Run one code-review reviewer lane against a configured endpoint.",
    )
    parser.add_argument("--lane", required=True, help="reviewer lane name")
    parser.add_argument(
        "--model",
        required=True,
        help="the resolved profile's model value (an llm-scripting-kit endpoint id)",
    )
    parser.add_argument(
        "--chunk", required=True, type=Path, help="path to the chunk .diff file"
    )
    parser.add_argument(
        "--file",
        action="append",
        default=[],
        dest="files",
        help="a path in this chunk; repeatable",
    )
    parser.add_argument("--description", default="", help="the change description")
    parser.add_argument("--project-root", default=None)
    parser.add_argument(
        "--max-output-tokens", type=int, default=DEFAULT_MAX_OUTPUT_TOKENS
    )
    parser.add_argument("--timeout", type=float, default=None, dest="timeout_s")
    return parser.parse_args(list(argv))


def main(argv: Sequence[str]) -> int:
    """CLI entry point. Prints the result envelope as JSON on success."""
    args = _parse_args(argv)
    try:
        diff_text = args.chunk.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"lane {args.lane}: cannot read chunk {args.chunk}: {exc}", file=sys.stderr)
        return EXIT_USAGE
    try:
        result = run_lane(
            lane=args.lane,
            model=args.model,
            diff_text=diff_text,
            files=args.files,
            description=args.description,
            project_root=args.project_root,
            max_output_tokens=args.max_output_tokens,
            timeout_s=args.timeout_s,
        )
    except LaneConfigError as exc:
        print(f"lane {args.lane}: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except LaneRunError as exc:
        print(f"lane {args.lane}: {exc}", file=sys.stderr)
        return EXIT_LANE_FAILED
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
