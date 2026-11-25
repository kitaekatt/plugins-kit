"""Run one code-review VALIDATOR on a codex harness entry.

A validator confirms or rejects ONE candidate issue. The Agent path runs it as
a `<kit>:review-lane-<effort>` subagent that answers with one line,
`CONFIRMED: <reason>` or `REJECTED: <reason>`. This module is the codex path:
it builds a protocol-1 request for llm-kit's existing completion CLI
(`llm-kit completion complete --request-file -`), runs it, and maps the
answer back onto that same one-line form, so step 7 of the review skill reads
both paths the same way.

The request pins three things the review relies on:

- a read-only sandbox with network off (`extras.sandbox: read-only`,
  `extras.network: false`), and the returned envelope must show the sandbox
  control applied and no network control enabled, or the verdict is refused;
- a strict JSON Schema output contract, `{verdict: CONFIRMED|REJECTED,
  reason}`. Codex applies OpenAI strict-mode rules to it, so every object has
  `additionalProperties: false` and lists every property in `required`;
- the entry's own stated effort and the bundle's project root as the working
  directory, so the validator can read the changed files and, for a
  silenced-error issue, the consumer the description names.

Malformed output never becomes a rejection: it exits EXIT_MALFORMED and the
skill reports the issue as unvalidated. Only a codex harness entry is run; any
other entry is refused before a call, because only the codex adapter honours
the sandbox extras.

DELIBERATELY NO llm_kit IMPORT. Like lane_prompts, this module is stdlib only.
It reaches llm-kit through the CLI of the interpreter that runs it (the kit's
plugin venv, where the wrapper re-execs), so bootstrap_lib gains no dependency.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from bootstrap_lib.code_review.lane_output import (
    LaneOutputInputError,
    claude_mds_by_file,
    load_bundle,
)
from bootstrap_lib.code_review.lane_prompts import (
    GUARDRAILS,
    LaneOutputError,
    parse_issue_array,
)

# Exit codes. The skill's step 7 acts on each one differently.
EXIT_VERDICT = 0  # stdout holds one CONFIRMED:/REJECTED: line
EXIT_CALL_FAILED = 1  # the call did not run to an answer; re-select
EXIT_REFUSED = 2  # refused as configured; report, do not re-select
EXIT_HALT = 3  # a quota or credit halt; record it, then re-select
EXIT_MALFORMED = 4  # the call answered, but not with a verdict; unvalidated

CODEX_BACKEND = "codex-cli"
SANDBOX_CONTROL = "sandbox-mode"
NETWORK_ENABLE_CONTROL = "network-enable"
VIOLATION_ERROR_CODE = "output-contract-violation"
PROTOCOL_ERROR_EXIT = 4  # llm-kit's exit for a request that never ran
LLM_KIT_HALT_EXIT = 3

VERDICTS = ("CONFIRMED", "REJECTED")
VERDICT_CONTRACT_ID = "code-review.validator-verdict"
VERDICT_SCHEMA_VERSION = "1"
VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "reason"],
    "properties": {
        "verdict": {"type": "string", "enum": list(VERDICTS)},
        "reason": {"type": "string", "minLength": 1},
    },
}

# The sandbox the request asks for. Only the codex adapter reads these keys.
READ_ONLY_EXTRAS: dict[str, Any] = {"sandbox": "read-only", "network": False}

DEFAULT_TIMEOUT_S = 900.0
# The CLI enforces timeout_s itself; this margin only catches a CLI that hangs
# past its own watchdog.
_PROCESS_MARGIN_S = 120.0

# The validator's restrictions, stated once. The review skill's `validator`
# subagent definition renders these same strings (scripts/gen_code_review_skills.py),
# so the Agent path and this path judge an issue by the same rules.
VALIDATOR_RESTRICTIONS = (
    "Validator does not see who flagged the issue. Independence is the value.",
    "Read the chunk diff, and each supplied CLAUDE.md, in full: when Read returns a "
    "partial view, continue with the offset it names until the last line. Never judge "
    "from a partial page. Do not Read other chunks.",
    "For a silenced-error issue (an input the change newly admits that a consumer "
    "ignores), MAY Read the consumer the description names. CONFIRMED when no consumer "
    "acts on the admitted input; it is not an input-dependent issue.",
)

VALIDATOR_SYSTEM = (
    "You are validating ONE candidate issue that a reviewer reported against one "
    "chunk of a code change. Confirm or reject it with high confidence.\n\n"
    "Rules:\n"
    + "\n".join(f"- {rule}" for rule in VALIDATOR_RESTRICTIONS)
    + "\n\n"
    "The chunk diff and the change description are given below; for a claude_md "
    "issue, so is the text of every CLAUDE.md that governs the issue's file. Your "
    "working directory is the repository: you may read the changed files there for "
    "surrounding context. Do not modify anything and do not run anything.\n\n"
    "The reviewer worked under the rules below. REJECT an issue that does not meet "
    "them, and REJECT an issue you are not certain is real.\n\n"
    + GUARDRAILS
    + "\n\n"
    "Reading those rules: an issue depends on specific inputs or state only when it "
    "needs an unusual input or state to occur at all. Logic that gives the wrong "
    "result for ordinary inputs is a definite error, even when a few coincidental "
    "inputs happen to produce the right answer.\n\n"
    "Answer with a JSON object: \"verdict\" is exactly \"CONFIRMED\" or \"REJECTED\", "
    "and \"reason\" is one sentence saying why."
)

Runner = Callable[[list[str], "str | None", float], "tuple[int, str, str]"]


class _Refused(Exception):
    """The lane declined to run as configured (EXIT_REFUSED)."""


def _subprocess_runner(argv: list[str], stdin: str | None, timeout: float) -> tuple[int, str, str]:
    """Run one llm-kit CLI verb under this interpreter."""
    completed = subprocess.run(
        [sys.executable, "-m", "llm_kit.cli", *argv],
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
    )
    return completed.returncode, completed.stdout, completed.stderr


def _load_issue(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
        value = json.loads(text)
    except OSError as exc:
        raise _Refused(f"cannot read issue {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise _Refused(f"issue {path} is not valid JSON: {exc.msg}") from exc
    if not isinstance(value, Mapping):
        raise _Refused(f"issue {path} must hold one JSON object")
    try:
        (issue,) = parse_issue_array(json.dumps([value]))
    except LaneOutputError as exc:
        raise _Refused(f"issue {path}: {exc}") from exc
    # Citation verification is reviewer-pipeline metadata, not evidence.
    issue.pop("citation_verification", None)
    return issue


def _chunk(bundle: Mapping[str, Any], chunk_index: int) -> tuple[str, list[str]]:
    chunks = bundle.get("diff_chunks")
    if not isinstance(chunks, list) or not 0 <= chunk_index < len(chunks):
        size = len(chunks) if isinstance(chunks, list) else 0
        raise _Refused(
            f"--chunk-index {chunk_index} is out of range for bundle.diff_chunks "
            f"({size} chunk(s))"
        )
    entry = chunks[chunk_index]
    bundle_dir = bundle.get("bundle_dir")
    if not isinstance(entry, Mapping) or not isinstance(bundle_dir, str) or not bundle_dir:
        raise _Refused("bundle.bundle_dir and bundle.diff_chunks[i].path are required")
    path = Path(bundle_dir) / str(entry.get("path", ""))
    try:
        diff = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise _Refused(f"cannot read chunk diff {path}: {exc}") from exc
    files = [str(f) for f in entry.get("files", []) if isinstance(f, str)]
    return diff, files


def _claude_md_text(bundle: Mapping[str, Any], file: str) -> str:
    chain = claude_mds_by_file(bundle).get(file, ())
    if not chain:
        return "(no governing CLAUDE.md was recorded for this file)"
    parts = []
    for path in chain:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            raise _Refused(f"cannot read governing CLAUDE.md {path}: {exc}") from exc
        parts.append(f"--- {path} ---\n{text}")
    return "\n".join(parts)


def build_user_message(
    issue: Mapping[str, Any], bundle: Mapping[str, Any], chunk_index: int
) -> str:
    """The user message: the issue, its chunk, and, for claude_md, its standards."""
    diff, files = _chunk(bundle, chunk_index)
    parts = ["Validate this candidate issue."]
    description = bundle.get("description") or ""
    if description:
        parts.append(f"Change description: {description}")
    parts.append("Candidate issue (JSON):\n" + json.dumps(dict(issue), indent=2))
    if issue["reason"] == "claude_md":
        parts.append(
            f"Governing CLAUDE.md files for {issue['file']}:\n"
            + _claude_md_text(bundle, str(issue["file"]))
        )
    if files:
        parts.append("Files in this chunk:\n" + "\n".join(f"- {f}" for f in files))
    parts.append("Diff:\n" + diff)
    return "\n\n".join(parts)


def project_root(bundle: Mapping[str, Any]) -> Path:
    root = bundle.get("project_root")
    if root is None:
        return Path.cwd().resolve()
    if not isinstance(root, str) or not root:
        raise _Refused("bundle.project_root must be a non-empty string or null")
    return Path(root).resolve()


def build_request(
    *,
    model: str,
    effort: str,
    system: str,
    prompt: str,
    cwd: Path,
    timeout_s: float,
) -> dict[str, Any]:
    """The protocol-1 request for `llm-kit completion complete --request-file -`."""
    return {
        "protocol": 1,
        "endpoint": model,
        "system": system,
        "prompt": prompt,
        "options": {
            "effort": effort,
            "cwd": str(cwd),
            "timeout_s": timeout_s,
            "extras": dict(READ_ONLY_EXTRAS),
            "output_contract": {
                "id": VERDICT_CONTRACT_ID,
                "policy": "native-required",
                "schema": VERDICT_SCHEMA,
                "schema_version": VERDICT_SCHEMA_VERSION,
            },
        },
    }


def parse_verdict(structured: Any) -> tuple[str, str]:
    """Return ``(verdict, one-line reason)``, or raise ``ValueError``."""
    if not isinstance(structured, Mapping):
        raise ValueError(f"structured answer is {type(structured).__name__}, not an object")
    verdict = structured.get("verdict")
    if verdict not in VERDICTS:
        raise ValueError(f"verdict {verdict!r} is not one of {list(VERDICTS)}")
    reason = structured.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("reason is missing or empty")
    return verdict, re.sub(r"\s+", " ", reason).strip()


def _check_codex(runner: Runner, model: str, root: Path) -> None:
    rc, out, err = runner(
        ["models", "resolve", "--models", model, "--project-root", str(root)], None, 120.0
    )
    if rc != 0:
        raise _CallFailed(f"`llm-kit models resolve --models {model}` exited {rc}: {err.strip()}")
    try:
        resolved = json.loads(out)
    except json.JSONDecodeError as exc:
        raise _CallFailed(f"`llm-kit models resolve` printed no JSON: {exc.msg}") from exc
    backend = resolved.get("backend") if isinstance(resolved, Mapping) else None
    if backend != CODEX_BACKEND:
        raise _Refused(
            f"entry {model!r} resolves to backend {backend!r}; the validator lane runs "
            f"only on a {CODEX_BACKEND} harness entry, the one adapter that honours the "
            "read-only sandbox. Dispatch a claude entry as an Agent validator instead."
        )


class _CallFailed(Exception):
    """The call did not run to an answer (EXIT_CALL_FAILED)."""


def _sandbox_evidence(envelope: Mapping[str, Any]) -> list[str]:
    response = envelope.get("response")
    controls = response.get("execution_controls_applied") if isinstance(response, Mapping) else None
    controls = list(controls) if isinstance(controls, list) else []
    if envelope.get("backend") != CODEX_BACKEND:
        raise _Refused(f"answer came from backend {envelope.get('backend')!r}, not {CODEX_BACKEND}")
    if SANDBOX_CONTROL not in controls:
        raise _Refused(
            f"the call applied no {SANDBOX_CONTROL} control (applied: {controls}); "
            "a validator verdict is used only from a read-only sandbox"
        )
    if NETWORK_ENABLE_CONTROL in controls:
        raise _Refused(f"the call enabled network access (applied: {controls})")
    return controls


def _error_of(envelope: Any) -> tuple[str, str]:
    response = envelope.get("response") if isinstance(envelope, Mapping) else None
    error = response.get("error") if isinstance(response, Mapping) else None
    if isinstance(error, Mapping):
        return str(error.get("code", "")), str(error.get("message", ""))
    return "", ""


def run(
    *,
    issue_path: Path,
    bundle_path: Path,
    chunk_index: int,
    model: str,
    effort: str,
    timeout_s: float,
    runner: Runner,
) -> tuple[int, str, str]:
    """Run one validator and return ``(exit code, stdout, stderr)``."""
    label = f"validator {issue_path.name} on {model} at {effort}"
    try:
        issue = _load_issue(issue_path)
        try:
            bundle = load_bundle(bundle_path)
        except LaneOutputInputError as exc:
            raise _Refused(str(exc)) from exc
        prompt = build_user_message(issue, bundle, chunk_index)
        root = project_root(bundle)
        _check_codex(runner, model, root)
        request = build_request(
            model=model, effort=effort, system=VALIDATOR_SYSTEM, prompt=prompt,
            cwd=root, timeout_s=timeout_s,
        )
        try:
            rc, out, err = runner(
                ["completion", "complete", "--request-file", "-"],
                json.dumps(request),
                timeout_s + _PROCESS_MARGIN_S,
            )
        except subprocess.TimeoutExpired as exc:
            raise _CallFailed(f"the completion CLI did not exit within {exc.timeout}s") from exc
        if rc == PROTOCOL_ERROR_EXIT:
            raise _Refused(f"llm-kit refused the request (exit 4): {err.strip()}")
        try:
            envelope = json.loads(out)
        except json.JSONDecodeError:
            if rc == 0:
                return (
                    EXIT_MALFORMED, "",
                    f"{label}: unvalidated: the completion CLI printed no JSON envelope\n",
                )
            raise _CallFailed(f"completion CLI exited {rc}: {err.strip()}")
        code, message = _error_of(envelope)
        if rc == LLM_KIT_HALT_EXIT:
            halt = envelope.get("halt") if isinstance(envelope, Mapping) else None
            return EXIT_HALT, "", f"{label}: halted: {code}: {message} {json.dumps(halt)}\n"
        if code == VIOLATION_ERROR_CODE:
            return EXIT_MALFORMED, "", f"{label}: unvalidated: {message}\n"
        if rc != 0:
            raise _CallFailed(f"completion CLI exited {rc}: {code}: {message}")
        controls = _sandbox_evidence(envelope)
        response = envelope.get("response") or {}
        try:
            verdict, reason = parse_verdict(response.get("structured"))
        except ValueError as exc:
            return EXIT_MALFORMED, "", f"{label}: unvalidated: {exc}\n"
        evidence = {
            "endpoint": envelope.get("endpoint"),
            "backend": envelope.get("backend"),
            "model": response.get("model"),
            "effort": effort,
            "execution_controls_applied": controls,
        }
        return EXIT_VERDICT, f"{verdict}: {reason}\n", f"{label}: {json.dumps(evidence)}\n"
    except _Refused as exc:
        return EXIT_REFUSED, "", f"{label}: refused: {exc}\n"
    except _CallFailed as exc:
        return EXIT_CALL_FAILED, "", f"{label}: failed: {exc}\n"


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run_validator_lane",
        description="Validate one candidate code-review issue on a codex harness entry.",
    )
    parser.add_argument("--issue", required=True, type=Path, help="file holding one issue object")
    parser.add_argument("--bundle", required=True, type=Path, help="prepared review bundle.json")
    parser.add_argument(
        "--chunk-index", required=True, type=int,
        help="index in bundle.diff_chunks of the chunk the issue was reported against",
    )
    parser.add_argument("--model", required=True, help="the chosen entry's id (a codex entry)")
    parser.add_argument("--effort", required=True, help="the chosen entry's stated effort")
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT_S,
        help=f"per-call timeout in seconds (default {DEFAULT_TIMEOUT_S:g})",
    )
    return parser.parse_args(list(argv))


def main(argv: Sequence[str] | None = None, *, runner: Runner = _subprocess_runner) -> int:
    """CLI entry point; exit codes are the EXIT_* constants above."""
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    code, out, err = run(
        issue_path=args.issue,
        bundle_path=args.bundle,
        chunk_index=args.chunk_index,
        model=args.model,
        effort=args.effort,
        timeout_s=args.timeout,
        runner=runner,
    )
    sys.stdout.write(out)
    sys.stderr.write(err)
    return code


__all__ = [
    "EXIT_CALL_FAILED",
    "EXIT_HALT",
    "EXIT_MALFORMED",
    "EXIT_REFUSED",
    "EXIT_VERDICT",
    "READ_ONLY_EXTRAS",
    "VALIDATOR_RESTRICTIONS",
    "VALIDATOR_SYSTEM",
    "VERDICT_CONTRACT_ID",
    "VERDICT_SCHEMA",
    "build_request",
    "build_user_message",
    "main",
    "parse_verdict",
    "run",
]


if __name__ == "__main__":
    sys.exit(main())
