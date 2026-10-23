"""Protocol 2 of ``llm-kit completion complete --request-file``: one call from a project file.

Protocol 1 names an endpoint and a model directly. Protocol 2 names a ROW or a
SELECTION of the project's model file (``.llm-kit/models.yaml``,
:mod:`llm_kit.registry.project_models`), which carries the endpoint, the exact
wire model, the narrowed effort menu and the request defaults. This module is
the pure half of that protocol, stdlib and completion types only: it parses and
validates the request, and builds the result and error documents. Resolving the
row, enforcing consent and dispatching live in :mod:`llm_kit.dispatch.invoke`,
which is where the registry and the adapters are reachable.

Request (a JSON object; ``protocol`` and ``prompt`` are required, and exactly one
of ``selection`` or ``model``):

``protocol``     2
``project_root`` project root the model file is confined to (default: the process cwd)
``file``         the model file under that root (default ``.llm-kit/models.yaml``)
``selection``    a key of the file's ``selections``
``model``        a catalogue row id (mutually exclusive with ``selection``)
``prompt``       the user prompt, non-empty
``system``       system text (default empty)
``mode``         ``completion`` (default) or ``workspace``
``working_dir``  the directory a workspace call runs in; required for ``workspace``
                 and refused for ``completion``; must exist beneath the project root
``expect_files`` relative paths that must be regular files in ``working_dir`` after a
                 workspace call; otherwise the call is reported failed
``output_schema`` a JSON Schema the answer must satisfy (validated-result policy)
``effort``       explicit effort, or ``null`` to send none; absent means the selection's
``max_tokens``   output token cap, a positive integer
``timeout_s``    per-call wall-clock cap in seconds, a positive number

Every refusal here is an :class:`InvokeRefusal`: it happens BEFORE anything is
spawned, so retrying the same bytes cannot help, and the CLI reports it with the
protocol exit code. ``kind`` says what to fix.

Result: see :func:`result_document`. A call that ran and failed is a result with
``status: "failed"``, never a refusal.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .contract_types import POLICY_VALIDATED_RESULT, OutputContract
from .request_protocol import ProtocolError
from .workspace_manifest import FileRecord, confine_relative

PROTOCOL_VERSION = 2
"""The request/result protocol version this module speaks (protocol 1 is :mod:`.request_protocol`)."""

MODE_COMPLETION = "completion"
MODE_WORKSPACE = "workspace"
MODES = (MODE_COMPLETION, MODE_WORKSPACE)

STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"

#: ``resolved.effort.source`` values: where the effort that was asked for came from.
EFFORT_FROM_REQUEST = "request"
EFFORT_FROM_SELECTION = "selection"
EFFORT_FROM_NONE = "none"

#: ``error.kind`` values of a refusal.
KIND_PROTOCOL = "protocol"
KIND_PROJECT_FILE = "project-file"
KIND_SELECTION = "selection"
KIND_EFFORT = "effort"
KIND_MODE = "mode"
KIND_PATH = "path"
KIND_CONTRACT = "contract"
KIND_CONSENT = "consent"
KIND_CONFIGURATION = "configuration"

#: ``error.code`` values of a failed result that no backend produced.
CODE_EXPECTED_FILES_MISSING = "expected-files-missing"
CODE_WORKSPACE_SCAN_FAILED = "workspace-scan-failed"

#: The output contract id a request's ``output_schema`` is declared under.
OUTPUT_CONTRACT_ID = "llm-kit.invoke.output"

_REQUEST_KEYS = (
    "protocol",
    "project_root",
    "file",
    "selection",
    "model",
    "prompt",
    "system",
    "mode",
    "working_dir",
    "expect_files",
    "output_schema",
    "effort",
    "max_tokens",
    "timeout_s",
)


class InvokeRefusal(ProtocolError):
    """A protocol 2 request was refused before any spawn; ``kind`` says what to fix."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class InvokeRequest:
    """A parsed, shape-validated protocol 2 request (nothing resolved against a project yet)."""

    project_root: Optional[str]
    file: Optional[str]
    selection: Optional[str]
    model: Optional[str]
    prompt: str
    system: str
    mode: str
    working_dir: Optional[str]
    expect_files: Tuple[str, ...]
    output_contract: Optional[OutputContract]
    effort_given: bool
    effort: Optional[str]
    max_tokens: Optional[int]
    timeout_s: Optional[float]


def _text(mapping: Mapping[str, Any], key: str, *, required: bool = False) -> Optional[str]:
    if key not in mapping or mapping[key] is None:
        if required:
            raise InvokeRefusal(KIND_PROTOCOL, f"{key} is required")
        return None
    value = mapping[key]
    if not isinstance(value, str):
        raise InvokeRefusal(KIND_PROTOCOL, f"{key} must be a string")
    return value


def parse_invoke_request(payload: Any) -> InvokeRequest:
    """Validate a decoded protocol 2 request; :class:`InvokeRefusal` on any fault."""
    if not isinstance(payload, Mapping):
        raise InvokeRefusal(KIND_PROTOCOL, f"request must be a JSON object, got {type(payload).__name__}")
    if payload.get("protocol") != PROTOCOL_VERSION or isinstance(payload.get("protocol"), bool):
        raise InvokeRefusal(KIND_PROTOCOL, f"protocol must be {PROTOCOL_VERSION}")
    unknown = sorted(set(payload) - set(_REQUEST_KEYS))
    if unknown:
        raise InvokeRefusal(
            KIND_PROTOCOL,
            "unknown request key(s): " + ", ".join(unknown) + "; known keys are " + ", ".join(sorted(_REQUEST_KEYS)),
        )
    selection = _text(payload, "selection")
    model = _text(payload, "model")
    if (selection is None) == (model is None):
        raise InvokeRefusal(KIND_SELECTION, "give exactly one of 'selection' (a selection key) or 'model' (a catalogue id)")
    for name, value in (("selection", selection), ("model", model)):
        if value is not None and not value:
            raise InvokeRefusal(KIND_SELECTION, f"{name} must be a non-empty string")
    prompt = _text(payload, "prompt", required=True)
    assert prompt is not None
    if not prompt.strip():
        raise InvokeRefusal(KIND_PROTOCOL, "prompt must not be empty")
    mode = _text(payload, "mode") or MODE_COMPLETION
    if mode not in MODES:
        raise InvokeRefusal(
            KIND_MODE,
            f"unknown mode {mode!r}; the modes are {', '.join(MODES)} (there is no image or build mode)",
        )
    working_dir = _text(payload, "working_dir")
    expect_raw = payload.get("expect_files")
    if mode == MODE_COMPLETION:
        if working_dir is not None:
            raise InvokeRefusal(KIND_PATH, "working_dir applies to mode 'workspace' only")
        if expect_raw is not None:
            raise InvokeRefusal(KIND_PATH, "expect_files applies to mode 'workspace' only")
    elif not working_dir:
        raise InvokeRefusal(KIND_PATH, "mode 'workspace' requires working_dir")
    expect: List[str] = []
    if expect_raw is not None:
        if not isinstance(expect_raw, list):
            raise InvokeRefusal(KIND_PATH, "expect_files must be a list of relative paths")
        for item in expect_raw:
            try:
                expect.append(confine_relative(item, what="expect_files entry"))
            except ValueError as exc:
                raise InvokeRefusal(KIND_PATH, str(exc)) from None
        if len(set(expect)) != len(expect):
            raise InvokeRefusal(KIND_PATH, "expect_files lists a path twice")
    contract: Optional[OutputContract] = None
    if payload.get("output_schema") is not None:
        schema = payload["output_schema"]
        if not isinstance(schema, dict):
            raise InvokeRefusal(KIND_CONTRACT, "output_schema must be a JSON Schema object")
        try:
            contract = OutputContract(id=OUTPUT_CONTRACT_ID, policy=POLICY_VALIDATED_RESULT, schema=schema)
        except ValueError as exc:
            raise InvokeRefusal(KIND_CONTRACT, f"output_schema: {exc}") from None
    effort_given = "effort" in payload
    effort = payload.get("effort")
    if effort is not None and (not isinstance(effort, str) or not effort):
        raise InvokeRefusal(KIND_EFFORT, f"effort must be a non-empty string or null, not {effort!r}")
    max_tokens = payload.get("max_tokens")
    if max_tokens is not None and (isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1):
        raise InvokeRefusal(KIND_PROTOCOL, "max_tokens must be a positive integer")
    timeout_s = payload.get("timeout_s")
    if timeout_s is not None:
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or not timeout_s > 0:
            raise InvokeRefusal(KIND_PROTOCOL, "timeout_s must be a positive number")
        timeout_s = float(timeout_s)
    return InvokeRequest(
        project_root=_text(payload, "project_root"),
        file=_text(payload, "file"),
        selection=selection,
        model=model,
        prompt=prompt,
        system=_text(payload, "system") or "",
        mode=mode,
        working_dir=working_dir,
        expect_files=tuple(expect),
        output_contract=contract,
        effort_given=effort_given,
        effort=effort,
        max_tokens=max_tokens,
        timeout_s=timeout_s,
    )


# ---------------------------------------------------------------------------
# Result documents
# ---------------------------------------------------------------------------


def effort_block(requested: Optional[str], sent: Optional[str], source: str) -> Dict[str, Any]:
    """``requested`` is the effort asked for, ``sent`` the post-remap wire value (null when none went out)."""
    return {"requested": requested, "sent": sent, "source": source}


def cost_block(
    cost_class: Optional[str],
    *,
    usd: Optional[float],
    usd_source: Optional[str],
    estimated_usd: Optional[float],
    estimated_source: Optional[str],
) -> Dict[str, Any]:
    """The cost class, the AUTHORITATIVE USD (null when unknown) and the separate estimate.

    ``usd`` is a provider- or registry-reported amount; ``estimated_usd`` is a
    backend figure that is not authoritative pricing, with its own source label.
    Null means unknown, never zero.
    """
    return {
        "class": cost_class,
        "usd": usd,
        "source": usd_source,
        "estimated_usd": estimated_usd,
        "estimated_source": estimated_source,
    }


def usage_block(response: Any, wall_ms: int) -> Dict[str, Any]:
    """Token usage as the backend reported it; every count is null when no response exists."""
    keys = ("input_tokens", "output_tokens", "cache_hit_tokens", "total_tokens")
    block: Dict[str, Any] = {key: (getattr(response, key, None) if response is not None else None) for key in keys}
    block["wall_ms"] = int(getattr(response, "wall_ms", 0) or wall_ms) if response is not None else int(wall_ms)
    return block


def result_document(
    *,
    status: str,
    mode: str,
    selection: Mapping[str, Any],
    resolved: Mapping[str, Any],
    output: Optional[Mapping[str, Any]],
    files: Optional[List[FileRecord]],
    usage: Mapping[str, Any],
    cost: Mapping[str, Any],
    error: Optional[Mapping[str, Any]] = None,
    halt: Optional[Mapping[str, Any]] = None,
    params: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """One protocol 2 result.

    ``resolved`` separates what was asked from what answered: ``requested_model``
    (the wire model the row names), ``reported_model`` (what the downstream said
    it served, null when it says nothing), ``model_verified`` (true only when the
    two agree; echoing the request never sets it) and the ``effort`` block.
    ``files`` is present for ``workspace`` calls only: the files created or
    modified under ``working_dir``, by path.
    """
    document: Dict[str, Any] = {
        "protocol": PROTOCOL_VERSION,
        "status": status,
        "mode": mode,
        "selection": dict(selection),
        "resolved": dict(resolved),
        "output": dict(output) if output is not None else None,
        "usage": dict(usage),
        "cost": dict(cost),
    }
    if files is not None:
        document["files"] = [record.to_json() for record in files]
    if params is not None:
        document["params"] = dict(params)
    if error is not None:
        document["error"] = dict(error)
    if halt is not None:
        document["halt"] = dict(halt)
    return document


def refusal_envelope(kind: str, message: str) -> Dict[str, Any]:
    """The failure shape for a protocol 2 request that never reached a backend (stderr, exit 4)."""
    return {"protocol": PROTOCOL_VERSION, "error": {"kind": kind, "message": message}}


def describe_invoke_schema() -> Dict[str, Any]:
    """The accepted protocol 2 request shape, for ``request-schema --protocol 2``."""
    return {
        "protocol": PROTOCOL_VERSION,
        "keys": {
            "protocol": f"integer, required, must equal {PROTOCOL_VERSION}",
            "project_root": "string, optional (default: the process working directory)",
            "file": "string, optional (default .llm-kit/models.yaml, confined to project_root)",
            "selection": "string, a key of the file's selections; exactly one of selection / model",
            "model": "string, a catalogue row id; exactly one of selection / model",
            "prompt": "string, required, non-empty",
            "system": "string, optional",
            "mode": f"one of {', '.join(MODES)}, optional (default {MODE_COMPLETION})",
            "working_dir": "string, required for workspace and refused for completion; must exist beneath project_root",
            "expect_files": "list of relative paths, workspace only; a missing or non-regular file fails the call",
            "output_schema": "JSON Schema object in the supported subset, optional",
            "effort": "string or null, optional; absent means the selection's effort, null sends none",
            "max_tokens": "integer >= 1, optional",
            "timeout_s": "number > 0, optional",
        },
        "result": {
            "status": f"{STATUS_COMPLETED} or {STATUS_FAILED}",
            "files": "workspace only: [{path, size, sha256}] created or modified under working_dir",
            "cost": "{class, usd, source, estimated_usd, estimated_source}; usd null means unknown, never zero",
        },
        "exit_codes": {
            "0": "completed",
            "1": "the call ran and failed (including expect_files not met)",
            "3": "the call ran and halted (quota, credit, auth, backpressure)",
            "4": "refused before any spawn; error.kind says what to fix (stderr)",
        },
    }


__all__ = [
    "CODE_EXPECTED_FILES_MISSING",
    "CODE_WORKSPACE_SCAN_FAILED",
    "EFFORT_FROM_NONE",
    "EFFORT_FROM_REQUEST",
    "EFFORT_FROM_SELECTION",
    "InvokeRefusal",
    "InvokeRequest",
    "KIND_CONFIGURATION",
    "KIND_CONSENT",
    "KIND_CONTRACT",
    "KIND_EFFORT",
    "KIND_MODE",
    "KIND_PATH",
    "KIND_PROJECT_FILE",
    "KIND_PROTOCOL",
    "KIND_SELECTION",
    "MODES",
    "MODE_COMPLETION",
    "MODE_WORKSPACE",
    "OUTPUT_CONTRACT_ID",
    "PROTOCOL_VERSION",
    "STATUS_COMPLETED",
    "STATUS_FAILED",
    "cost_block",
    "describe_invoke_schema",
    "effort_block",
    "parse_invoke_request",
    "refusal_envelope",
    "result_document",
    "usage_block",
]
