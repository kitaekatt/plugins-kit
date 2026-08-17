"""Command adapters over ``execution`` -- argv parsing only, no execution logic.

Per the plan's placement rule (A-min.1: "command adapters only -- the
execution logic lives under ``execution/``"), every handler here does the
same three things and nothing else: parse ``argv`` into positional values and
``--flag=value`` options, call exactly one
:class:`~content_pipeline.execution.store.ExecutionStore` (or
:func:`~content_pipeline.execution.status.compute_status`) method, and return
a YAML-able result for ``cli.scaffold.dispatch`` to render. State machine
rules, fencing, lease math, and the status digest all live in ``execution/``;
nothing here re-implements them.

:func:`build_commands` returns a ``{name: Command}`` mapping a consumer wires
onto its own entry point via ``cli.scaffold.dispatch`` -- this module ships no
console script and no ``main()``, matching the package-wide no-console-script
boundary (``plugins/content-pipeline-kit/CLAUDE.md``).

The ``protocol`` command (A-min.3) -- an argv shell around one JSON envelope
--------------------------------------------------------------------------------
When ``adapter`` is supplied, :func:`build_commands` additionally registers a
single ``"protocol"`` command: its ONE positional argument is a JSON-encoded
worker-protocol envelope (``execution.protocol``'s ``{"protocol_version":
..., "verb": ..., "payload": {...}}`` shape), and its result is whatever
:func:`~content_pipeline.execution.protocol.dispatch` returns. This still
honors the placement rule -- ``json.loads`` plus one call to
``protocol.dispatch`` is argv-shaping, not execution logic; every verb's
actual behavior (claim math, evaluation, apply) lives in ``execution/``, same
as every other command here. ``**protocol_policy`` forwards to
:func:`~content_pipeline.execution.protocol.build_handlers` (``strategy``,
``gates``, ``freshness_of``, etc.) -- see that function's docstring for which
verbs need which of them.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from content_pipeline.cli.scaffold import Command
from content_pipeline.execution.model import UsageRecord
from content_pipeline.execution.status import compute_status
from content_pipeline.execution.store import ExecutionStore

if TYPE_CHECKING:  # pragma: no cover -- type-check only; see build_commands' deferred import
    from content_pipeline.execution.adapter import RunAdapter


def _split_flags(args: List[str]) -> Tuple[List[str], Dict[str, str]]:
    """Split ``argv`` into positional tokens and ``--key=value`` flags.

    A bare ``--flag`` (no ``=``) is recorded with value ``"1"`` (a boolean
    switch, e.g. ``--terminal``).
    """
    positional: List[str] = []
    flags: Dict[str, str] = {}
    for arg in args:
        if arg.startswith("--"):
            body = arg[2:]
            if "=" in body:
                key, _, value = body.partition("=")
                flags[key] = value
            else:
                flags[body] = "1"
        else:
            positional.append(arg)
    return positional, flags


def _require(positional: List[str], index: int, name: str) -> str:
    if index >= len(positional):
        raise ValueError(f"missing required argument: {name}")
    return positional[index]


def _usage_record(flags: Dict[str, str]) -> Optional[UsageRecord]:
    keys = ("input-tokens", "output-tokens", "cache-hit-tokens")
    if not any(k in flags for k in keys):
        return None
    return UsageRecord(
        input_tokens=int(flags["input-tokens"]) if "input-tokens" in flags else None,
        output_tokens=int(flags["output-tokens"]) if "output-tokens" in flags else None,
        cache_hit_tokens=int(flags["cache-hit-tokens"]) if "cache-hit-tokens" in flags else None,
    )


def build_commands(
    store: ExecutionStore,
    *,
    adapter: Optional["RunAdapter"] = None,
    **protocol_policy: Any,
) -> Dict[str, Command]:
    """Build the ``{name: Command}`` registry for ``cli.scaffold.dispatch``.

    ``store`` is the caller's already-open :class:`ExecutionStore`; this
    factory closes over it so each handler needs no separate wiring step.

    ``adapter`` -- an optional
    :class:`~content_pipeline.execution.adapter.RunAdapter` -- registers the
    ``"protocol"`` command when supplied (see the module docstring's
    "The ``protocol`` command" section); omitted (the default), this
    function's return value is byte-identical to A-min.1/A-min.2's
    store-only command set. ``**protocol_policy`` is forwarded to
    :func:`~content_pipeline.execution.protocol.build_handlers` and is
    ignored when ``adapter`` is ``None``.
    """

    def create_run(args: List[str]) -> Any:
        positional, _flags = _split_flags(args)
        run_id = _require(positional, 0, "run_id")
        driver = _require(positional, 1, "driver")
        backend = _require(positional, 2, "backend")
        model = _require(positional, 3, "model")
        adapter_version = _require(positional, 4, "adapter_version")
        run = store.create_run(
            run_id, driver=driver, backend=backend, model=model, adapter_version=adapter_version
        )
        return {"id": run.id, "driver": run.driver, "backend": run.backend, "model": run.model}

    def register_units(args: List[str]) -> Any:
        positional, _flags = _split_flags(args)
        run_id = _require(positional, 0, "run_id")
        unit_ids = positional[1:]
        if not unit_ids:
            raise ValueError("register-units requires at least one unit id")
        store.register_units(run_id, unit_ids)
        return {"run_id": run_id, "registered": len(unit_ids)}

    def claim(args: List[str]) -> Any:
        positional, flags = _split_flags(args)
        run_id = _require(positional, 0, "run_id")
        unit_id = _require(positional, 1, "unit_id")
        worker_id = _require(positional, 2, "worker_id")
        kwargs = {}
        if "lease-seconds" in flags:
            kwargs["lease_seconds"] = float(flags["lease-seconds"])
        result = store.claim_unit(run_id, unit_id, worker_id, **kwargs)
        return {
            "run_id": run_id,
            "unit_id": unit_id,
            "fencing_token": result.fencing_token,
            "lease_expires_at": result.lease_expires_at,
        }

    def renew(args: List[str]) -> Any:
        positional, flags = _split_flags(args)
        run_id = _require(positional, 0, "run_id")
        unit_id = _require(positional, 1, "unit_id")
        fencing_token = int(_require(positional, 2, "fencing_token"))
        kwargs = {}
        if "lease-seconds" in flags:
            kwargs["lease_seconds"] = float(flags["lease-seconds"])
        lease_expires_at = store.renew_lease(run_id, unit_id, fencing_token, **kwargs)
        return {"run_id": run_id, "unit_id": unit_id, "lease_expires_at": lease_expires_at}

    def accept(args: List[str]) -> Any:
        positional, flags = _split_flags(args)
        run_id = _require(positional, 0, "run_id")
        unit_id = _require(positional, 1, "unit_id")
        fencing_token = int(_require(positional, 2, "fencing_token"))
        store.accept_unit(run_id, unit_id, fencing_token, usage=_usage_record(flags))
        return {"run_id": run_id, "unit_id": unit_id, "state": "accepted"}

    def fail(args: List[str]) -> Any:
        positional, flags = _split_flags(args)
        run_id = _require(positional, 0, "run_id")
        unit_id = _require(positional, 1, "unit_id")
        fencing_token = int(_require(positional, 2, "fencing_token"))
        store.fail_unit(
            run_id,
            unit_id,
            fencing_token,
            error=flags.get("error", ""),
            terminal=flags.get("terminal") == "1",
            usage=_usage_record(flags),
        )
        return {
            "run_id": run_id,
            "unit_id": unit_id,
            "state": "failed" if flags.get("terminal") == "1" else "pending",
        }

    def halt(args: List[str]) -> Any:
        positional, flags = _split_flags(args)
        run_id = _require(positional, 0, "run_id")
        kind = _require(positional, 1, "kind")
        store.set_halt(run_id, kind, flags.get("detail", ""))
        return {"run_id": run_id, "halted": kind}

    def clear_halt(args: List[str]) -> Any:
        positional, _flags = _split_flags(args)
        run_id = _require(positional, 0, "run_id")
        store.clear_halt(run_id)
        return {"run_id": run_id, "halted": None}

    def status(args: List[str]) -> Any:
        positional, flags = _split_flags(args)
        run_id = _require(positional, 0, "run_id")
        kwargs = {}
        if "window" in flags:
            kwargs["throughput_window_s"] = float(flags["window"])
        if "max-failure-groups" in flags:
            kwargs["max_failure_groups"] = int(flags["max-failure-groups"])
        digest = compute_status(store, run_id, **kwargs)
        return digest.to_dict()

    commands: Dict[str, Command] = {
        "create-run": Command(name="create-run", handler=create_run, help="Create a run."),
        "register-units": Command(
            name="register-units", handler=register_units, help="Register pending units."
        ),
        "claim": Command(name="claim", handler=claim, help="Atomically claim a unit."),
        "renew": Command(name="renew", handler=renew, help="Renew a claim's lease."),
        "accept": Command(name="accept", handler=accept, help="Terminally accept a unit."),
        "fail": Command(name="fail", handler=fail, help="Record a failed attempt."),
        "halt": Command(name="halt", handler=halt, help="Halt a run (blocks new claims)."),
        "clear-halt": Command(name="clear-halt", handler=clear_halt, help="Clear a run's halt."),
        "status": Command(name="status", handler=status, help="Bounded run-status digest."),
    }

    if adapter is not None:
        # Deferred import: only the "protocol" command needs `execution.protocol`
        # (and, transitively, `execution.adapter`, `llm.platform`,
        # `pipeline.single_pass`) -- a caller with no adapter (the
        # A-min.1/A-min.2 store-only shape) never pays for that import.
        from content_pipeline.execution.protocol import build_handlers, dispatch as protocol_dispatch

        handlers = build_handlers(store, adapter, **protocol_policy)

        def protocol(args: List[str]) -> Any:
            positional, _flags = _split_flags(args)
            envelope_text = _require(positional, 0, "envelope (JSON)")
            try:
                envelope = json.loads(envelope_text)
            except json.JSONDecodeError as exc:
                return {
                    "ok": False,
                    "error": {"type": "MalformedEnvelopeError", "message": f"invalid JSON: {exc}"},
                }
            return protocol_dispatch(envelope, handlers)

        commands["protocol"] = Command(
            name="protocol",
            handler=protocol,
            help="Dispatch one JSON worker-protocol envelope (execution.protocol).",
        )

    return commands


__all__ = ["build_commands"]
