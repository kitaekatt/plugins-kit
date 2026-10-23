"""``llm-kit models``: the configured endpoints, reachability, pacing and routing.

Carries the legacy ``endpoints``, ``probe``, ``usage``, ``record-halt``,
``describe``, ``seats``, ``models`` (here ``list``) and ``resolve`` verbs.
``probe`` keeps its three-way exit code (0 reachable, 1 unreachable, 5 could not
check, 2 an endpoint name that does not resolve).

``project show|validate|write|clear`` operate on the tracked project model file
(``llm_kit.registry.project_models``) and, with ``--layer local`` (``clear``
always), on the personal override file beside it (``<file-stem>.local.yaml``,
meant to be untracked; the library never edits ``.gitignore``): a refusal prints
one JSON ``error`` object (``kind`` ``project-models``, a ``code``, a
``message``) on stderr and exits 2; nothing is written on a refusal. ``show``
and ``catalog`` report each selection as ``{effective, default, source}`` under
``resolved_selections``.

``catalog`` reads that file and prints the ordered model catalogue
(``llm_kit.registry.catalog``) as one JSON document: the project's order, its
selections, each row's exact-model effort menu, modes and cost. It dispatches
nothing and probes no provider; ``--probe-auth`` opts into asking the claude and
codex CLIs how they are authenticated so a harness row gets a cost class.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

from llm_kit.adapters.capabilities import adapter_capabilities
from llm_kit.adapters.endpoint_profile import (
    profile_from_entry,
    profile_from_resolved,
    resolve_endpoint_profile,
)
from llm_kit.cli._common import (
    EXIT_FAILURE,
    EXIT_INDETERMINATE,
    EXIT_OK,
    EXIT_USAGE,
    _add_endpoint_arg,
    _add_models_arg,
    _add_project_arg,
    _add_requirements_arg,
    _backend_factory,
    _declared_models,
    _first_usable,
    _json,
    _load_requirements,
    _split_ids,
    guarded,
)
from llm_kit.cli._compose import create_backend
from llm_kit.dispatch import usage_budget
from llm_kit.dispatch.declaration import describe
from llm_kit.dispatch.reachability import (
    DEFAULT_VERIFY_TIMEOUT_S,
    STATUS_UNKNOWN,
    STATUS_UNREACHABLE,
    check_entry,
    check_many,
)
from llm_kit.dispatch.seats import discover_seats
from llm_kit.registry import catalog as model_catalog
from llm_kit.registry import project_models
from llm_kit.registry import consent as consent_store
from llm_kit.registry.effort import remap_effort
from llm_kit.registry.models import (
    EndpointResolveError,
    default_endpoint_name,
    discover_model_entries,
    load_model_config,
    resolve_endpoint,
)


def _add_describe_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--caller",
        choices=("session", "process"),
        default="session",
        help=(
            "session: an agent drives the harness itself (default); process: "
            "the completion seam drives it."
        ),
    )
    parser.add_argument("--self", dest="self_ref", help="The author's endpoint id or model id.")
    parser.add_argument(
        "--dispatchable",
        action="append",
        choices=("transport",),
        default=[],
        help=(
            "An entry kind this session caller can dispatch beyond the harness "
            "drives (repeatable). `transport`: keep OpenAI-compatible transport "
            "entries in the menu, for a caller with its own transport runner."
        ),
    )
    _add_requirements_arg(parser)
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="An id the caller has ruled out; repeatable or comma-separated.",
    )
    parser.add_argument(
        "--allow",
        action="append",
        default=None,
        help=(
            "Cost classes this caller may use, comma-separated or repeated: "
            "local, subscription, metered (e.g. `--allow local,subscription`). An "
            "entry of a class not listed is shown as switched off, never "
            "dispatched. The caller owns this filter; it does not read the consent "
            "layer. Omit the flag for no cost filtering. Not combinable with --consent."
        ),
    )
    parser.add_argument(
        "--consent",
        action="store_true",
        help=(
            "Opt in to the optional consent layer: switch off entries whose cost "
            "class the layer (`llm-kit consent show`) does not allow. A malformed "
            "consent file is a configuration error. Not combinable with --allow."
        ),
    )
    parser.add_argument(
        "--consent-scope",
        metavar="NAME",
        help="With --consent, apply the named scope's restrictions over the global policy.",
    )
    parser.add_argument("--json", action="store_true", help="Emit the ranking as JSON.")
    _add_project_arg(parser)


def _collect_endpoint_entries(
    project_root: Optional[str],
) -> "tuple[dict, Any, dict[str, dict[str, Any]]]":
    """Build the ``endpoints`` verb's entry map. Pure config, no I/O beyond disk reads.

    Shared by ``_cmd_endpoints`` and ``_cmd_probe`` so there is exactly one
    place that turns configuration into the JSON shape a reachability check
    dispatches on.
    """
    config = load_model_config(project_root=project_root)
    discovery = discover_model_entries(config=config, project_root=project_root)
    values: dict[str, dict[str, Any]] = {}
    raw = config.get("endpoints") or {}
    for name in raw:
        entry = discovery.get(str(name))
        if entry is not None:
            values[str(name)] = _entry_json(entry)
            continue
        try:
            endpoint = resolve_endpoint(str(name), config=config, project_root=project_root)
        except EndpointResolveError:
            continue
        profile = profile_from_resolved(endpoint, endpoint=str(name))
        values[str(name)] = {
            "kind": "transport", "base_url": endpoint["base_url"],
            "key_env": endpoint["key_env"], "default_model": endpoint.get("default"),
            "adapter": "openrouter",
            "reasoning_effort": profile.declared_effort,
            "effort_delivery": profile.effort.to_json(),
        }
    for name, entry in discovery.items():
        values.setdefault(name, _entry_json(entry))
    return config, discovery, values


def _cmd_endpoints(project_root: Optional[str], *, verify: bool = False, timeout_s: float = DEFAULT_VERIFY_TIMEOUT_S) -> int:
    config, discovery, values = _collect_endpoint_entries(project_root)
    if verify:
        # Opt-in only: a plain `endpoints` call must never make a network or
        # subprocess call, since callers enumerate configuration with it and
        # must not start paying for round trips silently.
        checks = check_many(values, timeout=timeout_s, project_root=project_root)
        for name, result in checks.items():
            values[name]["reachability"] = result.to_json()
    _json({
        "default": default_endpoint_name(config),
        "endpoints": values,
        # Keyed by adapter family, not by endpoint: the facts are properties of
        # adapter code, and duplicating them per endpoint would be the drift the
        # advertisement exists to remove.
        "capabilities": {
            name: cap.to_json() for name, cap in sorted(adapter_capabilities().items())
        },
        "notes": discovery.notes,
    })
    return EXIT_OK


def _cmd_probe(endpoint: Optional[str], project_root: Optional[str], timeout_s: float) -> int:
    """Exit-code check: is one configured endpoint reachable right now?

    A thin wrapper over the same :mod:`.reachability` code path `endpoints
    --verify` uses -- one implementation, two surfaces. Exit code is the
    primary interface: THREE distinguishable outcomes, not a plain
    success/failure pair -- see EXIT_INDETERMINATE for why "could not check"
    must not collapse into "checked and it is down". A short reason also goes
    to stderr on any non-zero exit, and the same reachability record rides on
    stdout as JSON for a caller that wants the detail either way.
    """
    config, _discovery, values = _collect_endpoint_entries(project_root)
    name = endpoint or default_endpoint_name(config)
    entry_json = values.get(name)
    if entry_json is None:
        raise EndpointResolveError(f"no configured endpoint named '{name}'")
    result = check_entry(entry_json, name, timeout=timeout_s, project_root=project_root)
    _json({"endpoint": name, **entry_json, "reachability": result.to_json()})
    # Exit code mapping (see EXIT_INDETERMINATE for the full rationale):
    #   0 (EXIT_OK)           reachability.STATUS_REACHABLE   -- checked, answered
    #   1 (EXIT_FAILURE)      reachability.STATUS_UNREACHABLE -- checked, did not answer
    #   5 (EXIT_INDETERMINATE) reachability.STATUS_UNKNOWN     -- could not check at all
    # 2 (EXIT_USAGE) is reserved for an endpoint NAME that does not resolve to
    # configuration at all -- raised above, before a check is ever attempted.
    if result.status == STATUS_UNREACHABLE:
        print(result.detail, file=sys.stderr)
        return EXIT_FAILURE
    if result.status == STATUS_UNKNOWN:
        print(result.detail, file=sys.stderr)
        return EXIT_INDETERMINATE
    return EXIT_OK


def _cmd_usage(as_json: bool, project_root: Optional[str], no_pin: bool) -> int:
    """Report the pacing verdict for every endpoint that opted in.

    An inspection surface for a check that is otherwise invisible: it runs
    inside `seats` and silently withholds an endpoint, so without this the
    only way to see WHY a frontier seat vanished would be to read the
    snapshot by hand.
    """
    discovery = discover_model_entries(project_root=project_root)
    opted_in = {
        entry_id: entry
        for entry_id, entry in sorted(discovery.entries.items())
        if entry.conserve_usage is not None
    }
    verdicts = {}
    for entry_id, entry in opted_in.items():
        if no_pin:
            budget = usage_budget.evaluate(entry.conserve_usage, entry.harness)
        else:
            budget = usage_budget.pinned_evaluate(
                entry_id, entry.conserve_usage, entry.harness
            )
        verdicts[entry_id] = budget
    if as_json:
        _json(
            {
                "pinned": not no_pin,
                "session_key": usage_budget.session_key(),
                "verdicts": {
                    entry_id: {
                        "harness": opted_in[entry_id].harness,
                        "conserve_usage": opted_in[entry_id].conserve_usage.to_json(),
                        "budget": budget.to_json(),
                    }
                    for entry_id, budget in verdicts.items()
                },
            }
        )
        return EXIT_OK
    if not opted_in:
        print("no endpoint declares conserve_usage")
        return EXIT_OK
    for entry_id, budget in verdicts.items():
        print(f"{entry_id}: {budget.status} -- {budget.detail}")
    return EXIT_OK


def _cmd_record_halt(
    entry_id: str, kind: str, resets_at: Optional[int], project_root: Optional[str]
) -> int:
    """Write an in-session quota/credit halt back to the pinned verdict.

    ``run()`` records the halts it observes itself. A session caller -- an
    agent driving the harness for orchestrate or a review lane -- observes the
    halt instead, and this verb is its write-back, so a later ``describe`` in
    the same session reads the entry out of quota rather than the stale
    AVAILABLE pin -- and so does every entry sharing its quota pool (same
    harness account, same pool), which ``describe`` labels "shares <pool>
    with". An entry without ``conserve_usage`` has no verdict to move
    and is a silent no-op; an unknown id is a usage error.
    """
    entries = discover_model_entries(project_root=project_root)
    entry = entries.get(entry_id)
    if entry is None:
        raise EndpointResolveError(f"unknown entry '{entry_id}'")
    result: dict[str, Any] = {"entry": entry_id, "kind": kind, "recorded": False}
    spec = getattr(entry, "conserve_usage", None)
    if spec is None:
        result["reason"] = "entry declares no conserve_usage; no verdict to record"
        _json(result)
        return EXIT_OK
    if resets_at is None:
        # The pool's own reading of the halt carries its reset time when the
        # harness recorded one (codex's "try again at" clause).
        reading = usage_budget.evaluate(spec, entry.harness)
        if reading.status == usage_budget.STATUS_OUT_OF_QUOTA:
            resets_at = reading.resets_at
    # entries: every other entry sharing this quota pool is spent too.
    budget = usage_budget.record_observed_halt(
        entry_id, spec, entries=entries, resets_at=resets_at
    )
    if budget is None:
        result["reason"] = "no session key; nothing is pinned to record against"
    else:
        result["recorded"] = True
        result["budget"] = budget.to_json()
    _json(result)
    return EXIT_OK


def _cmd_describe(ids: list[str], args: argparse.Namespace) -> int:
    """Render a declaration for ``--caller``; the floor exits EXIT_FAILURE.

    A process caller resolves ids through the completion seam's factory,
    because that is what will drive them; a session caller resolves them
    against the merged registry, because the agent drives the harness itself.
    Hidden entries are absent from the output; they surface only in the floor.
    Every rendered entry carries its cost class and the reason (``cost`` in the
    JSON), and the output carries one structured note that the consent layer is
    optional (a ``consent`` JSON key). ``--allow`` is the caller's own filter and
    ``--consent [--consent-scope X]`` the opt-in consent layer; either switches off
    entries whose cost class is not allowed, and those are SHOWN (a ``Switched
    off:`` line, a ``switched_off`` JSON key) and itemised by the floor. An unknown
    class, or both flags together, is a usage error (exit 2); a malformed consent
    file is a configuration error (exit 2).
    """
    requirements = _load_requirements(args.requirements)
    if args.allow is not None and args.consent:
        print(
            "llm-kit models describe: --allow and --consent are alternatives: --allow is your own "
            "filter, --consent applies the optional consent layer; pass one.",
            file=sys.stderr,
        )
        return EXIT_USAGE
    if args.consent_scope is not None and not args.consent:
        print("llm-kit models describe: --consent-scope needs --consent.", file=sys.stderr)
        return EXIT_USAGE
    policy_kwargs: dict[str, Any] = {}
    if args.consent:
        policy_kwargs["consent_policy"] = consent_store.policy(args.consent_scope)
    ranking = describe(
        ids,
        project_root=args.project_root,
        caller=args.caller,
        self_ref=args.self_ref,
        requirements=requirements,
        backend_factory=_backend_factory if args.caller == "process" else None,
        exclude=_split_ids(args.exclude),
        dispatchable=tuple(args.dispatchable),
        discovery=True,
        **policy_kwargs,
        **({"allow": _split_ids(args.allow)} if args.allow is not None else {}),
    )
    if args.json:
        _json(ranking.to_json())
    else:
        print(ranking.render())
    return EXIT_OK


def _cmd_seats(
    self_ref: str,
    as_json: bool,
    project_root: Optional[str],
    timeout_s: Optional[float],
) -> int:
    """Print reachable frontier seats and preserve indeterminate probes."""
    result = discover_seats(
        self_ref, project_root=project_root, timeout=timeout_s
    )
    if as_json:
        _json(result.to_json())
    else:
        for seat in result.seats:
            print(f"{seat.relation} {seat.endpoint} ({seat.band}, {seat.harness})")
    if result.probe_unknown:
        for seat in result.probe_unknown:
            print(
                f"{seat.endpoint}: {seat.reachability.detail}", file=sys.stderr
            )
        return EXIT_INDETERMINATE
    return EXIT_OK


# Which adapter family serves an endpoint. Mirrors create_backend's harness
# dispatch; capabilities are per ADAPTER FAMILY while endpoints are per registry
# entry, and several entries share one adapter (sol and luna are both codex).
_HARNESS_ADAPTERS = model_catalog.ADAPTER_BY_HARNESS


def _adapter_for(entry: Any) -> Optional[str]:
    if getattr(entry, "kind", None) == "harness":
        return _HARNESS_ADAPTERS.get((getattr(entry, "harness", "") or "").lower())
    return "openrouter"


def _entry_json(entry: Any) -> dict[str, Any]:
    result = {"kind": entry.kind, "model": entry.model, "name": entry.name}
    if entry.kind == "harness":
        result.update({"harness": entry.harness, "effort": entry.effort})
        endpoint = getattr(entry, "endpoint", None)
        if endpoint is not None:
            result["endpoint"] = endpoint
    else:
        result.update({"base_url": entry.base_url, "key_env": entry.key_env})
        result["frontdoor"] = bool(getattr(entry, "frontdoor", False))
        billing_mode = getattr(entry, "billing_mode", None)
        if billing_mode is not None:
            result["billing"] = {"mode": billing_mode}
            billing_owner = getattr(entry, "billing_owner", None)
            if billing_owner is not None:
                result["billing"]["owner"] = billing_owner
        # The declared default and whether a direct call can deliver it; the
        # openrouter family record keeps `effort` dropped for an endpoint
        # nothing is known about, so this is the per-endpoint half.
        profile = profile_from_entry(entry)
        result["reasoning_effort"] = profile.declared_effort
        result["effort_delivery"] = profile.effort.to_json()
    conserve = getattr(entry, "conserve_usage", None)
    if conserve is not None:
        result["conserve_usage"] = conserve.to_json()
    adapter = _adapter_for(entry)
    if adapter is not None:
        result["adapter"] = adapter
    return result


def _cmd_models(endpoint: Optional[str], project_root: Optional[str]) -> int:
    selection = create_backend(endpoint, project_root=project_root)
    if selection.kind == "harness":
        models = {selection.endpoint: selection.model}
    else:
        config = load_model_config(project_root=project_root)
        ep = resolve_endpoint(selection.endpoint, config=config, project_root=project_root)
        models = {
            alias: value.get("slug") if isinstance(value, dict) else None
            for alias, value in (ep.get("models") or {}).items()
        }
    _json({"endpoint": selection.endpoint, "kind": selection.kind, "models": models})
    return EXIT_OK


def _effort_report(selection: Any, project_root: Optional[str]) -> "tuple[Optional[str], dict]":
    """``(delivered effort, effort_delivery)`` for a selection's declared default.

    A transport delivers through its endpoint's effort style, so the delivered
    value is post-remap and None when no style delivers it. A harness delivers
    through its adapter's ``effort`` param when the adapter advertises one.
    """
    declared = getattr(selection, "effort", None)
    if selection.kind == "harness":
        record = getattr(selection, "capabilities", None) or getattr(
            selection.backend, "capabilities", None
        )
        param = record.params.get("effort") if record is not None else None
        delivery = {
            "deliverable": param is not None,
            "emits": param.emits if param is not None else None,
            "style": None,
            "source": "adapter",
        }
        return (declared if param is not None else None), delivery
    profile_of = getattr(selection.backend, "endpoint_profile", None)
    profile = (
        profile_of()
        if callable(profile_of)
        else resolve_endpoint_profile(selection.endpoint, project_root=project_root)
    )
    delivered = None
    if declared is not None and profile.effort.deliverable:
        delivered = remap_effort(declared, profile.effort.style)
    return delivered, profile.effort.to_json()


def _cmd_resolve(
    declared: Optional[list[str]], model: Optional[str], cheap: bool, project_root: Optional[str],
    requirements: Any = None,
) -> int:
    """Resolve the first usable entry of ``--models`` (or the default endpoint).

    ``--model`` stays a per-entry override applied to whichever entry is chosen.
    ``effort`` is the value a call without its own effort would put on the
    wire (post-remap; null when the entry cannot deliver it),
    ``declared_effort`` is the registry default, and ``effort_delivery`` says
    how the endpoint takes an effort.
    """
    if requirements is not None and not declared:
        raise ValueError("--requirements needs --models: it filters a declaration")
    if not declared:
        endpoint = None
    elif requirements is None:
        endpoint = _first_usable(declared, project_root)
    else:
        endpoint = _first_usable(declared, project_root, requirements)
    selection = create_backend(endpoint, model=model, cheap=cheap, project_root=project_root)
    delivered, delivery = _effort_report(selection, project_root)
    _json({"endpoint": selection.endpoint, "kind": selection.kind, "backend": selection.backend.name,
           "model": selection.model, "effort": delivered,
           "declared_effort": selection.effort, "effort_delivery": delivery})
    return EXIT_OK


def _read_project_input() -> Any:
    """The JSON document from stdin (``--input -``), parsed strictly."""
    return project_models.parse_json_document(sys.stdin.read())


def _project_input_required(args: argparse.Namespace) -> Any:
    if args.input is None:
        raise project_models.ProjectModelsError(
            project_models.CODE_INVALID, "--input - is required (or --key and --model with --layer local)"
        )
    return _read_project_input()


def _cmd_project(args: argparse.Namespace) -> int:
    """``models project show|validate|write|clear``; a refusal is a JSON error on stderr, exit 2."""
    where = {"project_root": args.project_root, "file": args.file}
    local = getattr(args, "layer", project_models.LAYER_TRACKED) == project_models.LAYER_LOCAL
    try:
        if args.project_cmd == "show":
            result: Any = project_models.read_resolved_models(**where)
        elif args.project_cmd == "clear":
            result = project_models.clear_local_override(args.key, if_match=args.if_match, **where)
        elif args.project_cmd == "validate":
            if local:
                result = project_models.validate_local_overrides(_read_project_input(), **where)
            else:
                result = project_models.validate_project_models(_read_project_input(), **where)
        elif local and args.key is not None:
            if args.model is None or args.input is not None:
                raise project_models.ProjectModelsError(
                    project_models.CODE_INVALID, "--key needs --model and cannot be combined with --input"
                )
            result = project_models.set_local_override(
                args.key, args.model, args.effort, if_match=args.if_match, **where
            )
        elif local:
            if args.model is not None or args.effort is not None:
                raise project_models.ProjectModelsError(
                    project_models.CODE_INVALID, "--model and --effort need --key"
                )
            result = project_models.write_local_overrides(
                _project_input_required(args), if_match=args.if_match, **where
            )
        else:
            if args.key is not None or args.model is not None or args.effort is not None:
                raise project_models.ProjectModelsError(
                    project_models.CODE_INVALID, "--key, --model and --effort need --layer local"
                )
            result = project_models.write_project_models(
                _project_input_required(args), if_match=args.if_match, **where
            )
    except project_models.ProjectModelsError as exc:
        _json({"error": exc.to_json()}, stream=sys.stderr)
        return EXIT_USAGE
    _json(result.to_json())
    return EXIT_OK


def _cmd_catalog(args: argparse.Namespace) -> int:
    """``models catalog``: the project's ordered catalogue; a refusal is a JSON error, exit 2."""
    modes = {name: cap.call_modes for name, cap in adapter_capabilities().items()}
    try:
        resolved = project_models.read_resolved_models(
            project_root=args.project_root, file=args.file
        )
        project = resolved.project
        harness_auth = None
        if args.probe_auth:
            from llm_kit.adapters.harness_auth import probe_harness_auth  # noqa: PLC0415

            used = {
                row["endpoint"] for row in project.document["catalog"]
            }
            discovery = discover_model_entries(project_root=args.project_root)
            harnesses = {
                discovery[name].harness for name in used
                if name in discovery and discovery[name].kind == "harness"
            }
            harness_auth = probe_harness_auth(
                harnesses, cwd=Path(args.project_root or ".").resolve()
            )
        result = model_catalog.build_catalog(
            project,
            project_root=args.project_root,
            modes_by_adapter=modes,
            harness_auth=harness_auth,
            local=resolved.local,
        )
    except project_models.ProjectModelsError as exc:
        _json({"error": exc.to_json()}, stream=sys.stderr)
        return EXIT_USAGE
    _json(result)
    return EXIT_OK


def _add_project_file_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--project-root",
        help="The project root the file is confined to (default: the current directory).",
    )
    parser.add_argument(
        "--file",
        help=(
            f"The project model file, relative to the root or an absolute path beneath "
            f"it (default {project_models.DEFAULT_FILE}). Symlinks and paths that leave "
            "the root are refused."
        ),
    )


_IF_MATCH_HELP = "Refuse unless the stored file still has this sha256 (from `project show`)."


def _add_layer_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--layer",
        choices=(project_models.LAYER_TRACKED, project_models.LAYER_LOCAL),
        default=project_models.LAYER_TRACKED,
        help=(
            "tracked (default): the project file. local: the personal override file beside it "
            "(<file-stem>.local.yaml), validated against the tracked catalogue; an override equal "
            "to the tracked default is dropped. The library does not edit .gitignore."
        ),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="llm-kit models",
        description="Configured endpoints, reachability, pacing verdicts and routing.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    endpoints = sub.add_parser("endpoints", help="List configured transports and harnesses.")
    _add_project_arg(endpoints)
    endpoints.add_argument(
        "--verify",
        action="store_true",
        help=(
            "Check each endpoint's reachability now and add a 'reachability' "
            "field to it. Off by default: plain `endpoints` stays instant, "
            "offline config listing -- this makes network/subprocess calls."
        ),
    )
    endpoints.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_VERIFY_TIMEOUT_S,
        help=f"Per-endpoint --verify timeout in seconds (default {DEFAULT_VERIFY_TIMEOUT_S:g}).",
    )
    probe = sub.add_parser(
        "probe",
        help="Exit-code check: is one configured endpoint reachable right now?",
    )
    _add_endpoint_arg(probe)
    _add_project_arg(probe)
    probe.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_VERIFY_TIMEOUT_S,
        help=f"Reachability check timeout in seconds (default {DEFAULT_VERIFY_TIMEOUT_S:g}).",
    )
    usage = sub.add_parser(
        "usage",
        help="Show the subscription-usage pacing verdict for conserve_usage endpoints.",
    )
    _add_project_arg(usage)
    usage.add_argument("--json", action="store_true", help="Emit the verdicts as JSON.")
    usage.add_argument(
        "--no-pin",
        action="store_true",
        help=(
            "Evaluate the pools now instead of reusing this session's pinned "
            "verdict. Inspection only -- it neither reads nor writes the pin."
        ),
    )
    record_halt = sub.add_parser(
        "record-halt",
        help=(
            "Record an observed quota or credit halt: a conserve_usage entry reads "
            "out of quota for the rest of this session."
        ),
    )
    record_halt.add_argument("entry", help="The registry id whose dispatch halted.")
    record_halt.add_argument(
        "--kind", choices=("quota", "credit"), default="quota",
        help="The halt observed (default quota).",
    )
    record_halt.add_argument(
        "--resets-at", type=int, default=None,
        help=(
            "The halt's reset time, epoch seconds. Default: the reset the pool "
            "reading reports, else a five-hour latch."
        ),
    )
    _add_project_arg(record_halt)
    describe_cmd = sub.add_parser(
        "describe",
        help=(
            "Render a model declaration for this machine: the usable, "
            "out-of-quota and unreachable entries in pace order, the default, "
            "and the choice rule."
        ),
    )
    describe_cmd.add_argument("ids", nargs="+", help="Declared registry ids, in declared order.")
    _add_describe_args(describe_cmd)
    seats = sub.add_parser("seats", help="List reachable UP and BESIDE harness seats.")
    seats.add_argument("--self", dest="self_ref", required=True, help="Self endpoint or exact model id.")
    seats.add_argument("--json", action="store_true", help="Emit the structured result as JSON.")
    _add_project_arg(seats)
    seats.add_argument("--timeout", type=float, default=None, help="Per-seat probe timeout in seconds.")
    models = sub.add_parser("list", help="List models for an endpoint.")
    _add_endpoint_arg(models)
    _add_project_arg(models)
    resolve = sub.add_parser("resolve", help="Resolve an endpoint/model selection.")
    _add_models_arg(resolve)
    _add_requirements_arg(resolve)
    _add_project_arg(resolve)
    resolve.add_argument("--model")
    resolve.add_argument("--cheap", action="store_true")
    catalog = sub.add_parser(
        "catalog",
        help=(
            "Print the project's ordered model catalogue as JSON: its selections, "
            "each row's exact-model effort menu, modes and cost. Dispatches nothing."
        ),
    )
    _add_project_file_args(catalog)
    catalog.add_argument(
        "--probe-auth",
        action="store_true",
        help=(
            "Ask the claude and codex CLIs how they are authenticated, so a harness "
            "row reports a cost class. Off by default: without it that class is null."
        ),
    )
    project = sub.add_parser(
        "project",
        help=(
            "Show, validate, write or clear the project model file (catalogue and selections) "
            "and the personal override file beside it."
        ),
    )
    project_sub = project.add_subparsers(dest="project_cmd", required=True)
    show = project_sub.add_parser(
        "show",
        help=(
            "Print the stored file as JSON with its sha256 (the --if-match value), the personal "
            "override file, and per selection {effective, default, source}."
        ),
    )
    _add_project_file_args(show)
    validate = project_sub.add_parser(
        "validate", help="Validate a JSON document from stdin; nothing is written."
    )
    _add_project_file_args(validate)
    _add_layer_arg(validate)
    validate.add_argument(
        "--input", required=True, choices=("-",), help="`-`: read the JSON document from stdin."
    )
    write = project_sub.add_parser(
        "write", help="Validate a JSON document from stdin and store it atomically as canonical YAML."
    )
    _add_project_file_args(write)
    _add_layer_arg(write)
    write.add_argument("--input", choices=("-",), help="`-`: read the JSON document from stdin.")
    write.add_argument(
        "--key",
        help="With --layer local: set this one override (needs --model) instead of replacing the file.",
    )
    write.add_argument("--model", help="With --key: the catalogue id to select.")
    write.add_argument(
        "--effort", help="With --key: the effort to select; omitted, the override selects no effort (null)."
    )
    write.add_argument("--if-match", metavar="SHA256", help=_IF_MATCH_HELP)
    clear = project_sub.add_parser(
        "clear",
        help=(
            "Remove one personal override; the personal file is deleted when none remains. "
            "The tracked file is never touched."
        ),
    )
    _add_project_file_args(clear)
    clear.add_argument("--key", required=True, help="The selection key whose override to remove.")
    clear.add_argument("--if-match", metavar="SHA256", help=_IF_MATCH_HELP)
    return parser


def run(argv: Sequence[str]) -> int:
    """Run one ``models`` verb and return its exit code."""
    args = _parser().parse_args(list(argv))

    def dispatch() -> int:
        if args.cmd == "endpoints":
            return _cmd_endpoints(args.project_root, verify=args.verify, timeout_s=args.timeout)
        if args.cmd == "probe":
            return _cmd_probe(args.endpoint, args.project_root, args.timeout)
        if args.cmd == "describe":
            return _cmd_describe(list(args.ids), args)
        if args.cmd == "record-halt":
            return _cmd_record_halt(args.entry, args.kind, args.resets_at, args.project_root)
        if args.cmd == "usage":
            return _cmd_usage(args.json, args.project_root, args.no_pin)
        if args.cmd == "seats":
            return _cmd_seats(args.self_ref, args.json, args.project_root, args.timeout)
        if args.cmd == "list":
            return _cmd_models(args.endpoint, args.project_root)
        if args.cmd == "project":
            return _cmd_project(args)
        if args.cmd == "catalog":
            return _cmd_catalog(args)
        if args.cmd == "resolve":
            return _cmd_resolve(
                _declared_models(args), args.model, args.cheap, args.project_root,
                _load_requirements(args.requirements),
            )
        return EXIT_USAGE

    return guarded(dispatch)
