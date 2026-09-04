"""Run one code-review lane against a configured llm-scripting-kit endpoint.

This module owns the endpoint-dispatch policy after a consumer wrapper has
completed bootstrap setup and verified this public entry point. The review
prompt and issue schema remain in bootstrap_lib.code_review.lane_prompts so
Agent and endpoint lanes share one LLM-neutral contract.

A configured endpoint lane is REFUSE-only: absent or incompatible shared
library capability fails loudly. There is no fallback to an Agent review, and
a failed call never silently substitutes one.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import replace as _replace_dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

from bootstrap_lib.code_review.lane_prompts import (
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
from llm_scripting_kit.completion import (
    BackendOptions,
    HaltError,
    create_backend,
)
from llm_scripting_kit.models import EndpointResolveError, discover_model_entries

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

# A review lane is a long single generation over a whole diff chunk, so it is
# pinned rather than left to the seam. Two backend defaults sit behind that
# choice and they answer different questions: the transport backends bound a
# GENERATION at 900s, while the opencode harness bounds LIVENESS at 120s -- a
# figure chosen to exceed that CLI's roughly 66-second connection-refused
# retry window and to cap the never-exiting unreachable-host case, not to size
# a reviewer's answer. Inheriting the liveness bound killed a three-file chunk
# mid-generation while two-file chunks finished in 45-83s, and the lane is
# judged by its JSON envelope, so a timeout costs the whole lane rather than
# degrading it. 900s matches the generation-shaped default and still bounds a
# hung endpoint; --timeout overrides it per call.
DEFAULT_TIMEOUT_S = 900.0

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


def _timeout_errors() -> tuple[type[BaseException], ...]:
    """The exception types that mean THIS lane's deadline expired.

    Probed rather than imported. The shared lib is linked by a `.pth` that
    pins no version, so an owner's release reaches a consumer venv without the
    consumer asking; a hard import of a symbol added in some particular
    llm-scripting-kit version would turn "your endpoint timed out" into
    "run_review_lane will not start" on any venv linked to an older copy
    (plugins/CLAUDE.md, shared-lib version probing). An empty tuple is a valid
    answer -- `except ()` matches nothing, so the timeout simply falls through
    to the generic handler as it did before this distinction existed.
    """
    try:
        from llm_scripting_kit import completion  # noqa: PLC0415
    except ImportError:  # pragma: no cover - the caller already reported this
        return ()
    found = getattr(completion, "AgentTimeoutError", None)
    return (found,) if isinstance(found, type) else ()


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
            "dependency group rather than as a requirement, so it is absent "
            "from the provisioned venv. Configure a HARNESS endpoint instead "
            "-- one carrying `harness:` rather than `base_url:`, which shells "
            "out to a CLI and needs no SDK -- or set this lane's `model` back "
            "to an Agent-tool alias (sonnet, opus, haiku, fable). Underlying "
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

    `Read` and nothing else: these lanes inspect the repository around the chunk
    -- the changed files' surrounding context, or the CLAUDE.md files that govern
    them -- which is the read-only use the seam sanctions. A reviewer has no
    business writing or running anything.
    """
    return "Read" if lane in LANES_REQUIRING_AGENT_LOOP else None


def _cwd_for(lane: str, project_root: Optional[str]) -> Optional[Path]:
    """The working directory a lane needs, or None to inherit the process cwd.

    An agent-loop lane is handed repo-RELATIVE file paths and is told to walk up
    from them, so where it is rooted decides whether those paths resolve at all.
    The CLI adapters default an absent `cwd` to the process cwd, which is right
    when the runner was launched from the project root and wrong the moment it
    was not -- and the failure is a reviewer that reports nothing rather than an
    error, so the caller's own `--project-root` is passed down when it has one.

    A pure-completion lane gets None: it reads nothing, and OpenRouter drops the
    parameter anyway.
    """
    if project_root is None or lane not in LANES_REQUIRING_AGENT_LOOP:
        return None
    return Path(project_root)


def run_lane(
    *,
    lane: str,
    model: str,
    diff_text: str,
    files: Sequence[str] = (),
    description: str = "",
    project_root: Optional[str] = None,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    timeout_s: Optional[float] = DEFAULT_TIMEOUT_S,
) -> dict[str, Any]:
    """Run one reviewer lane and return its result envelope.

    Raises ``LaneConfigError`` for a dispatch that must not be attempted and
    ``LaneRunError`` for one that was attempted and failed.
    """
    check_lane_dispatchable(lane, model)

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
        cwd=_cwd_for(lane, project_root),
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
        except _timeout_errors() as exc:
            # OUR deadline, not the endpoint's health. This lane sets
            # timeout_s, so a timeout is evidence about the budget chosen here
            # and says nothing about whether the endpoint is serving -- report
            # it as the configuration fact it is, and name the knob. Caught
            # before HaltError deliberately: llm-scripting-kit maps a CLI
            # timeout to a rate-limit halt, which is the right reading for a
            # caller that did NOT set the deadline and the wrong one for this
            # lane (plugins/CLAUDE.md, "A caller that sets the deadline owns
            # the timeout").
            raise LaneRunError(
                f"lane {lane} exceeded its own {options.timeout_s:g}s budget on "
                f"endpoint {selection.endpoint!r} ({exc}). The endpoint is not "
                "implicated -- raise --timeout, or review a smaller chunk"
            ) from exc
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
            # `replace` rather than a field-by-field rebuild: the repair attempt
            # must differ from the first ONLY in the cache salt, and a rebuild
            # that lists the fields silently drops any field added later -- the
            # tool grant and the working directory both matter to an agent-loop
            # lane, and losing either turns a repair into a different call.
            options = _replace_dataclass(options, cache_salt=attempt + 1)
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
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT_S, dest="timeout_s"
    )
    return parser.parse_args(list(argv))


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Prints the result envelope as JSON on success."""
    args = _parse_args(sys.argv[1:] if argv is None else argv)
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
    sys.exit(main())
