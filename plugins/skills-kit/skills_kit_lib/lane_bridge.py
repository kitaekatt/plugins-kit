"""lane_bridge -- run one md-domain lane on a codex harness entry.

A Workflow lane script cannot reach codex: its body has no shell, and agent()
starts Claude subagents only. When a lane's chosen ``lane_models`` entry has the
route ``codex`` (``lane_models.lane_route``), the script's laneAgent hands the
lane to the ``md-lane-bridge`` executor agent, a Bash-only subagent that runs
``scripts/run_lane.py`` once and returns its stdout. This module is that
helper's logic.

Input (the payload): one JSON object::

    {"v": 1, "entry": {"id": "<codex entry>", "effort": "<level>"},
     "access": "read-only" | "write", "prompt": "<the lane prompt>",
     "schema": {<the lane's result schema>}, "cwd": "<abs dir>"?}

The script reads it from stdin together with ``--input-digest``, the digest
the lane script computed over the exact text it sent. The executor copies that
text into its command, so a digest mismatch means the copy changed it, and
nothing is dispatched. ``--input-file`` (with ``--entry``/``--effort`` when
the file names no entry) is the direct-caller form: lane-prompt.cjs writes the
file, so nothing was transcribed.

Dispatch: llm-kit's ``Reselector`` ranks the one entry for a process caller
(the user's model overrides apply; an out-of-quota or disabled entry is
refused before any prompt is sent), ``create_backend`` builds it, and the
codex adapter runs one ``codex exec``. ``access`` decides the sandbox:
``read-only`` (detect, classify, coverage, generate verification) runs with
``-s read-only``; ``write`` (generate's compose and apply steps, apply-mode
remediate) runs with ``-s workspace-write`` rooted at ``cwd``. Network is off
in both. The lane schema is normalized for codex's strict structured-output
channel (every property required, optional ones nullable), the answer is
validated by the adapter, its optional nulls are removed, and the result is
validated again against the lane's own schema.

Output: one JSON line on stdout. Success::

    {"ok": true, "entry": {...}, "access": "...", "endpoint": "...",
     "served_model": "...", "argv": [...], "controls": [...],
     "digest": "<digest of canonical(result)>", "result": {...}}

Failure: ``{"ok": false, "entry": {...}?, "error": "<reason>", "exit": N}``.
Exit codes: 0 ok; 1 the lane ran or was refused by routing and produced no
valid result; 2 a malformed payload, digest mismatch, a schema codex cannot
carry, or an entry that is not a codex harness entry; 3 llm_kit is not
importable. The script writes no file; only codex, on a ``write`` lane,
changes files under ``cwd``.

``digest`` and ``canonical`` are mirrored byte for byte by the lane route
chunk in scripts/gen_workflow_js.py (laneDigest, laneCanonical); the lane
script recomputes the digest over the result the executor returned and
refuses a result that does not match.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import os
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

PAYLOAD_VERSION = 1
ACCESS_READ_ONLY = "read-only"
ACCESS_WRITE = "write"
ACCESSES = (ACCESS_READ_ONLY, ACCESS_WRITE)

#: Codex sandbox and network per access. Network is off for every lane.
CODEX_EXTRAS = {
    ACCESS_READ_ONLY: {"sandbox": "read-only", "network": False},
    ACCESS_WRITE: {"sandbox": "workspace-write", "network": False},
}

CONTRACT_ID = "skills-kit.md-lane"

#: The helper's own budget for one lane. It stays below the Bash tool's
#: 600 s ceiling, so the executor reports the helper's failure, not a kill.
DEFAULT_TIMEOUT_S = 540.0

EXIT_OK = 0
EXIT_LANE_FAILED = 1
EXIT_BAD_INPUT = 2
EXIT_NO_LLM_KIT = 3

SYSTEM_PROMPT = (
    "You are running one md-domain lane for the skills-kit plugin, under Codex. "
    "The task below was written for a Claude subagent: where it names a Claude "
    "Code tool (Read, Grep, Glob, Edit, Write), use your own equivalent -- read "
    "and search files with shell commands, and change a file only with your "
    "file-editing tool. Your final message is one JSON object that matches the "
    "output schema you were given, and nothing else."
)
ACCESS_NOTES = {
    ACCESS_READ_ONLY: (
        "This lane is read-only. The sandbox refuses writes, so do not try to "
        "change any file. Network access is off."
    ),
    ACCESS_WRITE: (
        "This lane may change files inside the working directory, as the task "
        "directs, and no others. Network access is off."
    ),
}


class BridgeInputError(ValueError):
    """The payload, its digest, its schema or its entry cannot be run (exit 2)."""


class BridgeRunError(RuntimeError):
    """The lane was dispatched or refused by routing and gave no valid result (exit 1)."""


class LlmKitUnavailable(RuntimeError):
    """llm_kit is not importable by this interpreter (exit 3)."""


# -- digest and canonical form (mirrored in the lane route chunk) -----------


def digest(text: str) -> str:
    """FNV-1a 32-bit over the code points of ``text``, with its length.

    The JS twin iterates ``for (const ch of s)`` (code points) and multiplies
    with ``Math.imul``; both reduce mod 2**32 after each step.
    """
    h = 0x811C9DC5
    n = 0
    for ch in text:
        h = ((h ^ ord(ch)) * 0x01000193) & 0xFFFFFFFF
        n += 1
    return f"fnv1a32:{n}:{h:08x}"


def canonical(value: Any) -> str:
    """Compact JSON with sorted keys; integral floats print as integers (as JS does)."""
    if isinstance(value, dict):
        return "{" + ",".join(
            json.dumps(key, ensure_ascii=False) + ":" + canonical(value[key])
            for key in sorted(value)
        ) + "}"
    if isinstance(value, list):
        return "[" + ",".join(canonical(item) for item in value) + "]"
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return json.dumps(value)


# -- payload ----------------------------------------------------------------


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise BridgeInputError(message)


def parse_payload(text: str, expected_digest: Optional[str]) -> dict:
    """Check the digest (when given) and the payload's shape; return the payload."""
    if expected_digest is not None:
        actual = digest(text)
        _require(
            actual == expected_digest,
            f"input digest mismatch: the lane sent {expected_digest}, this helper "
            f"received {actual}; the executor did not copy the payload verbatim, so "
            "nothing was dispatched",
        )
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise BridgeInputError(f"payload is not JSON: {exc}") from exc
    _require(isinstance(payload, dict), "payload must be a JSON object")
    _require(payload.get("v") == PAYLOAD_VERSION,
             f"payload v must be {PAYLOAD_VERSION}, got {payload.get('v')!r}")
    entry = payload.get("entry")
    _require(isinstance(entry, dict) and isinstance(entry.get("id"), str) and entry["id"].strip()
             and isinstance(entry.get("effort"), str) and entry["effort"].strip(),
             f"payload entry must be {{id, effort}} strings, got {entry!r}")
    _require(payload.get("access") in ACCESSES,
             f"payload access must be one of {', '.join(ACCESSES)}, got {payload.get('access')!r}")
    _require(isinstance(payload.get("prompt"), str) and payload["prompt"].strip() != "",
             "payload prompt must be a non-empty string")
    _require(isinstance(payload.get("schema"), dict), "payload schema must be a JSON object")
    cwd = payload.get("cwd")
    _require(cwd is None or (isinstance(cwd, str) and os.path.isabs(cwd)),
             f"payload cwd must be an absolute path when given, got {cwd!r}")
    unknown = sorted(set(payload) - {"v", "entry", "access", "prompt", "schema", "cwd"})
    _require(not unknown, f"payload has unknown field(s): {', '.join(unknown)}")
    return payload


# -- schema normalization for codex's strict channel ------------------------


def _is_object_schema(node: Mapping) -> bool:
    kind = node.get("type")
    names = (kind,) if isinstance(kind, str) else tuple(kind or ())
    return "object" in names or "properties" in node


def _nullable(node: dict) -> dict:
    out = dict(node)
    kind = out.get("type")
    if isinstance(kind, str):
        out["type"] = [kind, "null"] if kind != "null" else kind
    elif isinstance(kind, list):
        if "null" not in kind:
            out["type"] = [*kind, "null"]
    else:
        return {"anyOf": [out, {"type": "null"}]}
    if isinstance(out.get("enum"), list) and None not in out["enum"]:
        out["enum"] = [*out["enum"], None]
    if "const" in out:
        const = out.pop("const")
        return {"anyOf": [{**out, "const": const}, {"type": "null"}]}
    return out


def strict_schema(schema: Mapping) -> dict:
    """The lane schema rewritten for OpenAI strict mode.

    Every object lists all of its properties in ``required`` with
    ``additionalProperties: false``; a property the lane schema did not
    require becomes nullable, and :func:`strip_optional_nulls` removes the
    nulls again. An object with no declared properties (a free-form map)
    cannot be expressed in strict mode and raises ``BridgeInputError``.
    """

    def walk(node: Any, pointer: str) -> Any:
        if not isinstance(node, dict):
            return node
        out = copy.deepcopy(node)
        if _is_object_schema(out):
            props = out.get("properties") or {}
            extra = out.get("additionalProperties")
            _require(
                bool(props) and (extra is None or extra is False),
                f"schema object at {pointer or '/'} is not expressible in codex's strict "
                "structured output (it needs declared properties and no additional ones)",
            )
            required = set(out.get("required") or ())
            new_props = {}
            for name, child in props.items():
                walked = walk(child, f"{pointer}/properties/{name}")
                new_props[name] = walked if name in required else _nullable(walked)
            out["properties"] = new_props
            out["required"] = list(props)
            out["additionalProperties"] = False
        if "items" in out:
            out["items"] = walk(out["items"], f"{pointer}/items")
        if isinstance(out.get("anyOf"), list):
            out["anyOf"] = [walk(b, f"{pointer}/anyOf/{i}") for i, b in enumerate(out["anyOf"])]
        if isinstance(out.get("$defs"), dict):
            out["$defs"] = {k: walk(v, f"{pointer}/$defs/{k}") for k, v in out["$defs"].items()}
        return out

    return walk(dict(schema), "")


def strip_optional_nulls(value: Any, schema: Mapping, root: Optional[Mapping] = None) -> Any:
    """Remove the nulls :func:`strict_schema` let codex put in optional properties."""
    root = schema if root is None else root
    node = schema
    if isinstance(node.get("$ref"), str) and node["$ref"].startswith("#/$defs/"):
        node = (root.get("$defs") or {}).get(node["$ref"][len("#/$defs/"):], {})
    if isinstance(value, dict) and isinstance(node.get("properties"), dict):
        required = set(node.get("required") or ())
        out = {}
        for key, item in value.items():
            child = node["properties"].get(key)
            if item is None and key not in required:
                continue
            out[key] = strip_optional_nulls(item, child, root) if isinstance(child, dict) else item
        return out
    if isinstance(value, list) and isinstance(node.get("items"), dict):
        return [strip_optional_nulls(item, node["items"], root) for item in value]
    return value


# -- dispatch ---------------------------------------------------------------


def _llm_kit() -> dict:
    try:
        from llm_kit.completion import halt
        from llm_kit.completion.contract_types import POLICY_NATIVE_REQUIRED, OutputContract
        from llm_kit.completion.types import BackendOptions
        from llm_kit.dispatch.declaration import (
            NoUsableRoutingTarget,
            Reselector,
            call_with_fallthrough,
        )
        from llm_kit.dispatch.factory import create_backend
        from llm_kit.schema.json_schema import check_schema, validate
    except ImportError as exc:
        raise LlmKitUnavailable(
            f"llm_kit is not importable ({exc}); the md-lane bridge needs the "
            "katsoft-llm-kit package the skills-kit plugin venv installs"
        ) from exc
    return {
        "halt": halt, "OutputContract": OutputContract, "POLICY": POLICY_NATIVE_REQUIRED,
        "BackendOptions": BackendOptions, "NoUsableRoutingTarget": NoUsableRoutingTarget,
        "Reselector": Reselector, "call_with_fallthrough": call_with_fallthrough,
        "create_backend": create_backend, "check_schema": check_schema, "validate": validate,
    }


def _record_quota_halt(entry_id: str, exc: BaseException, project_root: str) -> str:
    """Write an observed quota or credit halt back to llm-kit; return a note."""
    from llm_kit.dispatch.usage_budget import record_observed_halt
    from llm_kit.registry.models import discover_model_entries

    entries = discover_model_entries(project_root=project_root)
    spec = getattr(entries.get(entry_id), "conserve_usage", None)
    if spec is None:
        return ""
    record_observed_halt(entry_id, spec, entries=entries, resets_at=getattr(exc, "resets_at", None))
    return f"; recorded the {entry_id} quota halt with llm-kit"


def run_lane(
    payload: Mapping,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    lib: Optional[dict] = None,
    backend_factory: Optional[Callable[..., Any]] = None,
) -> dict:
    """Run the payload's lane on its codex entry and return the success status.

    Raises ``BridgeInputError``, ``BridgeRunError`` or ``LlmKitUnavailable``.
    ``lib`` and ``backend_factory`` are test seams for the llm_kit names and
    for ``create_backend``.
    """
    k = lib if lib is not None else _llm_kit()
    entry = {"id": payload["entry"]["id"].strip(), "effort": payload["entry"]["effort"].strip()}
    access = payload["access"]
    cwd = Path(payload.get("cwd") or os.getcwd()).resolve()
    schema = dict(payload["schema"])
    try:
        k["check_schema"](schema)
    except ValueError as exc:
        raise BridgeInputError(f"lane schema is outside llm-kit's schema subset: {exc}") from exc
    strict = strict_schema(schema)
    try:
        contract = k["OutputContract"](id=CONTRACT_ID, policy=k["POLICY"], schema=strict)
    except ValueError as exc:
        raise BridgeInputError(f"normalized lane schema is not a valid output contract: {exc}") from exc
    violations = tuple(getattr(contract, "strict_violations", ()) or ())
    _require(not violations, f"normalized lane schema still breaks strict mode at {', '.join(violations)}")

    factory = backend_factory or k["create_backend"]
    reselector = k["Reselector"]([entry["id"]], project_root=str(cwd), caller="process",
                                 backend_factory=factory)
    try:
        state = reselector.select()
    except k["NoUsableRoutingTarget"] as floor:
        raise BridgeRunError(f"entry {entry['id']!r} is not serving this run: {floor}") from floor
    selection = factory(state.id, project_root=str(cwd))
    backend = selection.backend
    _require(getattr(backend, "name", None) == "codex-cli",
             f"entry {entry['id']!r} resolves to {getattr(backend, 'name', 'unknown')!r}, not a "
             "codex harness entry; the md-lane bridge runs codex entries only")

    argvs: list[list[str]] = []
    original_runner = backend.runner

    def recording_runner(argv: Any, *args: Any, **kwargs: Any) -> Any:
        argvs.append([str(part) for part in argv])
        return original_runner(argv, *args, **kwargs)

    backend = dataclasses.replace(backend, runner=recording_runner)
    options = k["BackendOptions"](
        effort=entry["effort"],
        timeout_s=timeout_s,
        cwd=cwd,
        log_prefix=f"[md-lane:{entry['id']}]",
        output_contract=contract,
        extras=dict(CODEX_EXTRAS[access]),
    )
    system = SYSTEM_PROMPT + " " + ACCESS_NOTES[access]

    def call(_state: Any) -> Any:
        return backend.complete(system, payload["prompt"], model=selection.model, options=options)

    try:
        response = k["call_with_fallthrough"](reselector, call, first=state)
    except Exception as exc:  # noqa: BLE001 -- every endpoint failure ends this lane
        note = ""
        classify = getattr(backend, "classify_halt", None)
        kind = classify(exc) if callable(classify) else None
        if kind in (k["halt"].HALT_QUOTA, k["halt"].HALT_INSUFFICIENT_CREDIT):
            note = _record_quota_halt(entry["id"], exc, str(cwd))
        raise BridgeRunError(
            f"codex entry {entry['id']!r} failed: {type(exc).__name__}: {exc}{note}"
        ) from exc
    if "effort" in (getattr(response, "dropped_params", None) or ()):
        raise BridgeRunError(f"codex entry {entry['id']!r} dropped effort {entry['effort']!r}")
    structured = getattr(response, "structured", None)
    if not isinstance(structured, dict):
        raise BridgeRunError(f"codex entry {entry['id']!r} returned no structured result")
    result = strip_optional_nulls(structured, schema)
    errors = k["validate"](schema, result)
    if errors:
        shown = ", ".join(f"{pointer or '/'} ({keyword})" for pointer, keyword in errors[:10])
        raise BridgeRunError(f"codex result fails the lane schema at {shown}")
    return {
        "ok": True,
        "entry": entry,
        "access": access,
        "endpoint": getattr(selection, "endpoint", entry["id"]),
        "served_model": getattr(response, "model", None) or getattr(selection, "model", "") or "",
        "argv": argvs[-1] if argvs else [],
        "controls": list(getattr(response, "execution_controls_applied", ()) or ()),
        "digest": digest(canonical(result)),
        "result": result,
    }


def main(argv: Optional[list[str]] = None, *, stdin: Any = None, stdout: Any = None) -> int:
    """CLI: print one status line; see the module docstring for exit codes."""
    import argparse
    import sys

    out = stdout if stdout is not None else sys.stdout
    parser = argparse.ArgumentParser(
        prog="run_lane.py",
        description="Run one md-domain lane on a codex harness entry and print its result.",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input-digest", help="Digest of the payload read from stdin "
                        "(the lane script's laneDigest over the exact text it sent).")
    source.add_argument("--input-file", help="Read the payload from this file instead "
                        "(lane-prompt.cjs --out-dir writes one per lane).")
    parser.add_argument("--entry", help="Codex entry id, when the payload names none.")
    parser.add_argument("--effort", help="Effort for --entry.")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S,
                        help=f"Seconds for the codex call (default {DEFAULT_TIMEOUT_S:g}).")
    args = parser.parse_args(argv)

    def emit(status: dict) -> None:
        out.write(json.dumps(status, ensure_ascii=False) + "\n")

    entry: Optional[dict] = None
    try:
        if args.input_file:
            text = Path(args.input_file).read_text(encoding="utf-8")
            raw = json.loads(text)
            if isinstance(raw, dict) and "entry" not in raw and args.entry:
                raw["entry"] = {"id": args.entry, "effort": args.effort or ""}
            payload = parse_payload(json.dumps(raw, ensure_ascii=False), None)
        else:
            text = (stdin if stdin is not None else sys.stdin).read()
            if text.endswith("\n"):
                text = text[:-1]
            payload = parse_payload(text, args.input_digest)
        entry = payload["entry"]
        status = run_lane(payload, timeout_s=args.timeout)
    except BridgeInputError as exc:
        emit({"ok": False, "entry": entry, "error": str(exc), "exit": EXIT_BAD_INPUT})
        return EXIT_BAD_INPUT
    except LlmKitUnavailable as exc:
        emit({"ok": False, "entry": entry, "error": str(exc), "exit": EXIT_NO_LLM_KIT})
        return EXIT_NO_LLM_KIT
    except (BridgeRunError, OSError, ValueError) as exc:
        emit({"ok": False, "entry": entry, "error": str(exc), "exit": EXIT_LANE_FAILED})
        return EXIT_LANE_FAILED
    emit(status)
    return EXIT_OK
