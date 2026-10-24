"""Protocol 2 execution: resolve a project-file row, enforce consent, run one call, take receipts.

:func:`invoke` is the engine behind ``llm-kit completion complete --request-file -``
with ``"protocol": 2`` (the pure request/result half is
:mod:`llm_kit.completion.invoke_protocol`). It makes ONE call, in this order, and
every step before the call can refuse; a refusal is an
:class:`~llm_kit.completion.invoke_protocol.InvokeRefusal` raised with nothing
spawned:

1. read and validate the project model file, confined to the project root;
2. resolve the selection or row to its endpoint and exact wire model; a
   ``selection`` is read through the personal layer (``<stem>.local.yaml``
   beside the tracked file), which wins per key over the tracked default, and
   a refusal that comes from that file names it;
3. hold the effort to the row's narrowed, exact-model menu (an explicit request
   effort overrides the selection's and is revalidated) and refuse an effort the
   endpoint cannot put on the wire;
4. build the backend and refuse a mode its adapter does not honor
   (:func:`~llm_kit.completion.capabilities.require_call_mode`);
5. confine ``working_dir`` and refuse a contract the adapter cannot deliver;
6. classify the cost of the entry (always reported with its reason) and, when
   the request opted in to the optional consent layer (``consent``), refuse it
   unless that policy allows its class -- BEFORE any scan, argv or runner call.
   A claude or codex entry's class depends on the CLI's authentication, learned
   by a probe that costs seconds, so only an opted-in request probes; without
   the opt-in the class is reported as ``null`` with an "not probed" reason
   (as ``models catalog`` does without ``--probe-auth``);
7. for ``workspace``, take the selected filesystem snapshot; run the call; take it again.

The cost class comes from :func:`llm_kit.registry.cost.classify`, the rule the
``allow`` filter of ``declaration.describe`` applies, and the policy from
:func:`llm_kit.registry.consent.policy`. The consent layer is optional: a request
without the ``consent`` field does not opt in and is
never refused for its cost class. This path does not go through
``describe``: that ranks a declaration and probes reachability, and a project
selection names exactly one entry, so a probe would only add a network or
process call that the real call answers anyway.

No harness is assumed: the only subprocesses are the ones the chosen adapter
itself runs, and nothing here reads or creates a path under ``~/.claude``.
"""
from __future__ import annotations

import os
import stat
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional

from llm_kit.completion.capabilities import (
    FILESYSTEM_WRITE,
    SHELL_EXEC,
    SUBAGENT_SPAWN,
    CallModeError,
    require_call_mode,
)
from llm_kit.completion.contract import OutputContractUnsatisfiable, OutputContractViolation, prepare_contract
from llm_kit.completion.halt import AgentTimeoutError, halt_payload
from llm_kit.completion.invoke_protocol import (
    CODE_EXPECTED_FILES_MISSING,
    CODE_WORKSPACE_SCAN_FAILED,
    EFFORT_FROM_NONE,
    EFFORT_FROM_REQUEST,
    EFFORT_FROM_SELECTION,
    KIND_CONFIGURATION,
    KIND_CONSENT,
    KIND_CONTRACT,
    KIND_EFFORT,
    KIND_MODE,
    KIND_PATH,
    KIND_PROJECT_FILE,
    KIND_SELECTION,
    MODE_WORKSPACE,
    STATUS_COMPLETED,
    STATUS_FAILED,
    InvokeRefusal,
    InvokeRequest,
    parse_codex_config,
    cost_block,
    effort_block,
    result_document,
    usage_block,
)
from llm_kit.completion.requirements import arm_call
from llm_kit.completion.types import BackendOptions, LLMResponse
from llm_kit.completion.workspace_manifest import FileRecord, check_expected, diff, scan
from llm_kit.registry import project_models
from llm_kit.registry import consent as consent_store
from llm_kit.registry.cost import COST_CLASSES, HarnessAuth, classify
from llm_kit.registry.model_endpoints import EndpointRegistryError
from llm_kit.registry.models import (
    EndpointResolveError,
    ModelResolveError,
    claude_cli_alias,
    discover_model_entries,
)

#: Outcomes of :func:`invoke`, for the caller to map onto its exit codes.
OUTCOME_COMPLETED = "completed"
OUTCOME_FAILED = "failed"
OUTCOME_HALTED = "halted"

#: Harnesses whose cost class depends on how the CLI is authenticated (learned by probing it).
_AUTH_DEPENDENT_HARNESSES = ("claude", "codex")

#: The cost reason of a request that did not opt in to consent, so the auth was not probed;
#: the same wording `models catalog` reports without `--probe-auth`.
UNPROBED_REASON = "{harness} cost class depends on its authentication, which was not probed"

HarnessProbe = Callable[[str, Path], Optional[HarnessAuth]]

#: ``(scope) -> Policy``: how the consent layer's policy is read; injectable for tests.
PolicyLoader = Callable[[Optional[str]], consent_store.Policy]


@dataclass(frozen=True)
class InvokeOutcome:
    """The result document and how the call ended (``completed``, ``failed`` or ``halted``)."""

    document: Dict[str, Any]
    outcome: str


def _probe_harness(harness: str, cwd: Path) -> Optional[HarnessAuth]:
    from llm_kit.adapters.harness_auth import probe_harness_auth  # noqa: PLC0415

    return probe_harness_auth([harness], cwd=cwd).get(harness.lower())


def _confine_directory(root: Path, name: str) -> Path:
    """``name`` as an existing directory beneath ``root``, reached through no symbolic link."""
    candidate = Path(name)
    lexical = Path(os.path.normpath(candidate if candidate.is_absolute() else root / candidate))
    if not lexical.is_relative_to(root) and candidate.is_absolute():
        lexical = Path(os.path.realpath(lexical.parent)) / lexical.name
    if not lexical.is_relative_to(root):
        raise InvokeRefusal(KIND_PATH, f"working_dir {name!r} resolves outside the project root {root}")
    relative = lexical.relative_to(root)
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise InvokeRefusal(
                KIND_PATH, f"working_dir passes through a symbolic link ({cursor}); it must be reached without one"
            )
    if not lexical.is_dir():
        raise InvokeRefusal(KIND_PATH, f"working_dir {name!r} does not exist as a directory under {root}")
    return lexical


def _confine_regular_file(root: Path, name: str) -> Path:
    """Resolve an existing regular file beneath root without following symlinks."""
    relative = Path(name)
    parts = relative.parts
    cursor = root.joinpath(*parts)
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    no_follow_flag = getattr(os, "O_NOFOLLOW", 0)
    if os.open not in os.supports_dir_fd:
        checked = root
        for index, part in enumerate(parts):
            checked = checked / part
            try:
                mode = checked.lstat().st_mode
            except OSError as exc:
                raise InvokeRefusal(KIND_PATH, f"image {name!r} is not an existing regular file: {exc}") from None
            if stat.S_ISLNK(mode):
                raise InvokeRefusal(KIND_PATH, f"image {name!r} passes through a symbolic link")
            if index < len(parts) - 1 and not stat.S_ISDIR(mode):
                raise InvokeRefusal(KIND_PATH, f"image {name!r} has a non-directory parent")
        if not stat.S_ISREG(mode):
            raise InvokeRefusal(KIND_PATH, f"image {name!r} is not a regular file")
        try:
            descriptor = os.open(checked, os.O_RDONLY | no_follow_flag)
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise InvokeRefusal(KIND_PATH, f"image {name!r} is not a regular file")
            finally:
                os.close(descriptor)
        except InvokeRefusal:
            raise
        except OSError as exc:
            raise InvokeRefusal(KIND_PATH, f"image {name!r} cannot be opened safely: {exc}") from None
        return checked
    root_fd = os.open(root, os.O_RDONLY | directory_flag | no_follow_flag)
    parent_fd = root_fd
    descriptor = -1
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | directory_flag | no_follow_flag, dir_fd=parent_fd)
            if parent_fd != root_fd:
                os.close(parent_fd)
            parent_fd = next_fd
        descriptor = os.open(parts[-1], os.O_RDONLY | no_follow_flag, dir_fd=parent_fd)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise InvokeRefusal(KIND_PATH, f"image {name!r} is not a regular file")
    except InvokeRefusal:
        raise
    except OSError as exc:
        raise InvokeRefusal(KIND_PATH, f"image {name!r} cannot be opened safely: {exc}") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if parent_fd != root_fd:
            os.close(parent_fd)
        os.close(root_fd)
    return cursor


def _model_verified(requested: str, reported: Optional[str]) -> bool:
    """True only when the downstream's own statement names the requested model.

    A Claude CLI session reports a dated id for an alias request, so the alias
    the dated id belongs to counts as the same model.
    """
    if not reported:
        return False
    if reported == requested:
        return True
    return claude_cli_alias(reported) == requested


def invoke(
    request: InvokeRequest,
    *,
    default_project_root: Optional[str],
    backend_factory: Callable[..., Any],
    policy_loader: Optional[PolicyLoader] = None,
    harness_probe: Optional[HarnessProbe] = None,
) -> InvokeOutcome:
    """Run one protocol 2 call; see the module docstring for the order and the refusals.

    ``backend_factory`` is :func:`llm_kit.dispatch.factory.create_backend` (or a
    call-time lookup of it); ``policy_loader`` reads the consent layer's policy
    for a scope and defaults to :func:`llm_kit.registry.consent.policy`;
    ``harness_probe`` answers ``(harness, cwd) -> HarnessAuth`` and defaults to
    :func:`llm_kit.adapters.harness_auth.probe_harness_auth`.
    """
    root_arg = request.project_root if request.project_root is not None else default_project_root

    # 1. the project file ----------------------------------------------------
    try:
        located = project_models.resolve_project_file(root_arg, request.file)
        stored = project_models.read_project_models(project_root=root_arg, file=request.file)
    except project_models.ProjectModelsError as exc:
        raise InvokeRefusal(KIND_PROJECT_FILE, f"{exc.code}: {exc}") from None
    except EndpointRegistryError as exc:
        raise InvokeRefusal(KIND_CONFIGURATION, str(exc)) from None
    root = located.root
    document = stored.document

    # 2. selection / row ------------------------------------------------------
    selected_effort: Optional[str] = None
    if request.selection is not None:
        try:
            local = project_models.read_local_overrides(stored, project_root=root_arg, file=request.file)
        except project_models.ProjectModelsError as exc:
            # an invalid layer is a selection problem (its message names the local file); a path problem is a file problem
            kind = KIND_SELECTION if exc.code == project_models.CODE_INVALID else KIND_PROJECT_FILE
            raise InvokeRefusal(kind, f"{exc.code}: {exc}") from None
        resolved_selections = project_models.resolve_models(stored, local).selections
        chosen = (resolved_selections.get(request.selection) or {}).get("effective")
        if chosen is None:
            known = ", ".join(resolved_selections) or "<none>"
            raise InvokeRefusal(
                KIND_SELECTION,
                f"selection {request.selection!r} is in neither {stored.path} nor {local.path} (known: {known})",
            )
        row_id = chosen["model"]
        selected_effort = chosen.get("effort")
    else:
        row_id = request.model or ""
    row = next((r for r in document["catalog"] if r["id"] == row_id), None)
    if row is None:
        known = ", ".join(r["id"] for r in document["catalog"]) or "<none>"
        raise InvokeRefusal(KIND_SELECTION, f"model {row_id!r} is not a catalogue id in {stored.path} (known: {known})")
    endpoints = project_models._Endpoints(str(root))  # noqa: SLF001 -- the registry lookup validation used
    info = endpoints.info(row["endpoint"])
    if info is None:
        raise InvokeRefusal(KIND_CONFIGURATION, f"endpoint {row['endpoint']!r} of row {row_id!r} no longer resolves")

    # 3. effort ---------------------------------------------------------------
    if request.effort_given:
        requested_effort, effort_source = request.effort, EFFORT_FROM_REQUEST
    elif selected_effort is not None:
        requested_effort, effort_source = selected_effort, EFFORT_FROM_SELECTION
    else:
        requested_effort, effort_source = None, EFFORT_FROM_NONE
    if requested_effort is not None:
        menu = tuple(project_models.library_effort_menu(info, row["model"]))
        allowed = tuple(row["efforts"]) if "efforts" in row else menu
        if requested_effort not in allowed:
            no_dial = not allowed and (
                "efforts" in row or project_models.exact_effort_menu(info.route, row["model"]) == ()
            )
            none_text = (
                "none -- this model has no effort dial; send null"
                if no_dial
                else "none -- the library has no measured effort menu for this model; send null"
            )
            raise InvokeRefusal(
                KIND_EFFORT,
                f"effort {requested_effort!r} is not accepted for {row_id!r} (accepted: "
                f"{', '.join(allowed) or none_text}); "
                "nothing was dispatched",
            )

    # 4. backend and mode -----------------------------------------------------
    try:
        selection = backend_factory(row["endpoint"], model=row["model"], project_root=str(root))
    except (EndpointResolveError, ModelResolveError, EndpointRegistryError) as exc:
        raise InvokeRefusal(KIND_CONFIGURATION, str(exc)) from None
    backend = selection.backend
    row_extras = dict((row.get("request_defaults") or {}).get("extras") or {})
    row_codex_config = parse_codex_config(row_extras.get("codex_config"), what="request_defaults.extras.codex_config")
    effective_codex_config = {**row_codex_config, **dict(request.codex_config)}
    backend_name = getattr(backend, "name", None)
    row_codex_config_given = "codex_config" in row_extras and row_extras["codex_config"] is not None
    if (request.codex_config_given or row_codex_config_given) and backend_name != "codex-cli":
        raise InvokeRefusal(KIND_CONFIGURATION, "codex_config is supported only by the Codex adapter")
    if request.images and backend_name != "codex-cli":
        raise InvokeRefusal(KIND_MODE, f"{backend_name or 'this adapter'} does not support image input")
    if backend_name == "codex-cli":
        effective_sandbox = row_extras.get("sandbox", "workspace-write")
        if not isinstance(effective_sandbox, str) or effective_sandbox not in ("read-only", "workspace-write"):
            raise InvokeRefusal(
                KIND_CONFIGURATION,
                f"effective Codex sandbox {effective_sandbox!r} must be read-only or workspace-write",
            )
        if (
            request.mode != MODE_WORKSPACE
            and "shell_environment_policy.inherit" in effective_codex_config
        ):
            raise InvokeRefusal(
                KIND_CONFIGURATION,
                "shell_environment_policy.inherit cannot be used in Codex text-only mode",
            )
    record = getattr(selection, "capabilities", None) or getattr(backend, "capabilities", None)
    if record is not None:
        try:
            require_call_mode(record, request.mode)
        except CallModeError as exc:
            raise InvokeRefusal(KIND_MODE, str(exc)) from None
    elif request.mode == MODE_WORKSPACE:
        raise InvokeRefusal(KIND_MODE, f"{getattr(backend, 'name', 'this backend')} advertises no call modes; nothing was dispatched")

    # 5. options, working directory, contract, effort delivery -----------------
    adapter_extras = {key: value for key, value in row_extras.items() if key != "codex_config"}
    if effective_codex_config:
        adapter_extras["__llm_kit_codex_config"] = effective_codex_config
    options = BackendOptions(
        effort=requested_effort,
        extras=adapter_extras,
        output_contract=request.output_contract,
        log_prefix=f"[{selection.endpoint}]",
    )
    if request.max_tokens is not None:
        options = replace(options, max_tokens=request.max_tokens)
    if request.timeout_s is not None:
        options = replace(options, timeout_s=request.timeout_s)
    workdir: Optional[Path] = None
    if request.mode == MODE_WORKSPACE:
        workdir = _confine_directory(root, request.working_dir or "")
        options = replace(options, cwd=workdir)
    image_root = workdir or root
    image_paths = tuple(str(_confine_regular_file(image_root, name)) for name in request.images)
    if image_paths:
        options = replace(options, extras={**dict(options.extras), "__llm_kit_image_paths": image_paths})
    if request.output_contract is not None and record is not None:
        try:
            prepare_contract(record, options)
        except OutputContractUnsatisfiable as exc:
            raise InvokeRefusal(KIND_CONTRACT, str(exc)) from None
    sent_effort: Optional[str] = requested_effort
    effort_plan = getattr(backend, "effort_plan", None)
    if callable(effort_plan):
        plan = effort_plan(options)
        sent_effort = plan.applied
        if requested_effort is not None and not plan.effort_delivered:
            raise InvokeRefusal(
                KIND_EFFORT,
                f"endpoint {row['endpoint']!r} cannot put effort {requested_effort!r} on the wire "
                f"({plan.outcome}); declare effort_style on the endpoint or send effort null; "
                "nothing was dispatched",
            )

    # 6. consent --------------------------------------------------------------
    entries = discover_model_entries(project_root=str(root))
    entry = entries.get(row["endpoint"])
    opted_in = request.consent is not None
    harness = (getattr(entry, "harness", None) or "").lower() if entry is not None else ""
    cost_class: Optional[str]
    if harness in _AUTH_DEPENDENT_HARNESSES and not opted_in:
        # Only the consent layer needs the answer, and asking the CLI costs seconds:
        # report the class as unprobed (null), as `models catalog` does without --probe-auth.
        cost_class = None
        cost_reason = UNPROBED_REASON.format(harness=harness)
    else:
        auth: Optional[Dict[str, HarnessAuth]] = None
        if harness in _AUTH_DEPENDENT_HARNESSES:
            probed = (harness_probe or _probe_harness)(harness, workdir or root)
            auth = {harness: probed} if probed is not None else {}
        verdict = classify(entry, entries=entries, harness_auth=auth)
        cost_class, cost_reason = verdict.cost_class, verdict.reason
        if cost_class not in COST_CLASSES:  # classify's contract; a new class must be handled, not allowed
            raise InvokeRefusal(KIND_CONSENT, f"unrecognised cost class {cost_class!r}")
    load_policy = policy_loader or consent_store.policy
    consent_report: Dict[str, Any] = {
        "opted_in": opted_in,
        "applied": opted_in,
        "scope": request.consent.scope if request.consent is not None else None,
    }
    if request.consent is not None:
        _refuse_unless_allowed(load_policy, request.consent.scope, cost_class, cost_reason, row["endpoint"])

    # arm the controls a plain completion relies on (no file writes, no shell, no subagents)
    if request.mode != MODE_WORKSPACE and record is not None:
        try:
            armed = arm_call(
                backend, options, {"guarantees": [FILESYSTEM_WRITE, SHELL_EXEC, SUBAGENT_SPAWN]}, record
            )
        except ValueError as exc:
            raise InvokeRefusal(KIND_MODE, str(exc)) from None
        backend, options = armed.backend, armed.options
    if getattr(backend, "text_only", False) and "shell_environment_policy.inherit" in effective_codex_config:
        raise InvokeRefusal(
            KIND_CONFIGURATION,
            "shell_environment_policy.inherit cannot be used in Codex text-only mode",
        )

    # 7. the call, with receipts ----------------------------------------------
    before: Optional[Mapping[str, FileRecord]] = None
    watched_paths: Optional[Tuple[str, ...]] = None
    if workdir is not None:
        if request.watch_files is not None:
            watched_paths = tuple(sorted(set(request.watch_files) | set(request.expect_files)))
        try:
            before = scan(workdir, watched_paths)
        except OSError as exc:
            raise InvokeRefusal(KIND_PATH, f"cannot scan working_dir before the call: {exc}") from None

    started = time.monotonic()
    response: Optional[LLMResponse] = None
    failure: Optional[Dict[str, Any]] = None
    halt_info: Optional[Dict[str, Any]] = None
    halted = False
    try:
        response = backend.complete(request.system, request.prompt, model=selection.model, options=options)
    except OutputContractViolation as exc:
        response = exc.response
        failure = _error_from_response(exc.response, str(exc))
    except Exception as exc:  # noqa: BLE001 -- transports raise heterogeneous types
        halt = backend.classify_halt(exc)
        halted = bool(halt)
        code = "timeout" if isinstance(exc, AgentTimeoutError) else (halt or "execution")
        failure = {"code": code, "message": str(exc)}
        if halt:
            halt_info = halt_payload(halt, exc)
    wall_ms = int((time.monotonic() - started) * 1000)

    files: Optional[list] = None
    if workdir is not None and before is not None:
        try:
            files = diff(before, scan(workdir, watched_paths))
        except OSError as exc:
            if failure is None:
                failure = {"code": CODE_WORKSPACE_SCAN_FAILED, "message": f"cannot scan working_dir after the call: {exc}"}
        if failure is None:
            missing = check_expected(workdir, request.expect_files)
            if missing:
                failure = {
                    "code": CODE_EXPECTED_FILES_MISSING,
                    "message": "expected file(s) are not regular files in working_dir: " + ", ".join(missing),
                }

    reported_model = getattr(response, "reported_model", None) if response is not None else None
    requested_model = row["model"]
    resolved = {
        "endpoint": selection.endpoint,
        "adapter": getattr(backend, "name", None),
        "requested_model": requested_model,
        "reported_model": reported_model,
        "model_verified": _model_verified(requested_model, reported_model),
        "effort": effort_block(requested_effort, sent_effort, effort_source),
    }
    cost = cost_block(
        cost_class,
        reason=cost_reason,
        usd=getattr(response, "reported_cost_usd", None),
        usd_source=getattr(response, "reported_cost_source", None),
        estimated_usd=getattr(response, "estimated_cost_usd", None),
        estimated_source=getattr(response, "estimated_cost_source", None),
    )
    output = (
        {"text": response.text, "structured": response.structured}
        if response is not None and response.status == "completed"
        else ({"text": response.text, "structured": None} if response is not None else None)
    )
    params = (
        {"dropped": list(response.dropped_params), "forwarded": list(response.forwarded_params)}
        if response is not None
        else None
    )
    status = STATUS_FAILED if failure is not None else STATUS_COMPLETED
    outcome = OUTCOME_HALTED if halted else (OUTCOME_FAILED if failure is not None else OUTCOME_COMPLETED)
    return InvokeOutcome(
        result_document(
            status=status,
            mode=request.mode,
            selection={"id": request.selection, "model": row_id},
            resolved=resolved,
            output=output,
            files=files,
            usage=usage_block(response, wall_ms),
            cost=cost,
            consent=consent_report,
            error=failure,
            halt=halt_info,
            params=params,
            manifest=(
                {"scope": "watch", "watched_paths": list(watched_paths)}
                if watched_paths is not None
                else ({"scope": "all"} if workdir is not None else None)
            ),
        ),
        outcome,
    )


def _refuse_unless_allowed(
    load_policy: PolicyLoader, scope: Optional[str], cost_class: str, reason: str, endpoint: str
) -> None:
    """Enforce the consent layer the request opted in to.

    Raises a ``consent`` refusal naming the class, the scope and the exact
    ``llm-kit consent set`` command, or a ``configuration`` refusal when the
    consent file cannot be read or is malformed; returns when the class is allowed.
    """
    try:
        policy = load_policy(scope)
    except (consent_store.ConsentConfigError, OSError, ValueError) as exc:
        raise InvokeRefusal(
            KIND_CONFIGURATION, f"the request opted in to the consent layer, and it is misconfigured: {exc}"
        ) from None
    if not policy.allows(cost_class):
        raise InvokeRefusal(
            KIND_CONSENT,
            f"{endpoint!r} is a {cost_class} entry ({reason}) and {policy.refusal(cost_class)}; "
            "nothing was dispatched",
        )


def _error_from_response(response: LLMResponse, fallback: str) -> Dict[str, Any]:
    error = response.error
    if error is not None:
        return {"code": error.code, "message": error.message}
    return {"code": "execution", "message": fallback}


__all__ = [
    "InvokeOutcome",
    "OUTCOME_COMPLETED",
    "OUTCOME_FAILED",
    "OUTCOME_HALTED",
    "invoke",
]
