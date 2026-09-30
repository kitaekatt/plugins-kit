#!/usr/bin/env python
"""workflow-kit artifact checker: judge a script node's $OUT against its declared type.

A script node that PROVIDES a named artifact runs its command, captures the
command's exit status, and then always runs this checker on the file the
command wrote. The checker records its judgment in a verdict file; the node's
exit code is the checker's.

    <workflow-kit-venv-python> scripts/check_artifact.py \\
        --artifact <NAME> --kind {schema,opaque-file} --in <OUT> \\
        --verdict <VERDICT.json> --command-exit <CODE> \\
        [--schema <JSON> --schema-digest <HEX>] \\
        [--events <EVENTS.jsonl> --run-id <runId> --unit-id <unitId>]

``--schema`` and ``--schema-digest`` are required with ``--kind schema`` and
refused with ``--kind opaque-file``. ``--events`` requires ``--run-id`` and
``--unit-id``.

Judgment:

- ``--command-exit`` non-zero: ``missing``; the checker exits with that code
  (clamped to 1..255) without reading ``--in``.
- ``opaque-file``: ``satisfied`` when ``--in`` is a regular file, else
  ``missing``.
- ``schema``: ``missing`` when ``--in`` is not a regular file or cannot be
  read. Otherwise the bytes are decoded as strict UTF-8 and parsed as JSON
  with ``NaN``/``Infinity``/``-Infinity`` refused; a decode or parse failure is
  ``violated`` with the single error ``["", "unparseable"]``. A parsed value is
  validated with llm-scripting-kit's ``completion.json_schema.validate`` (the
  closed subset an ``OutputContract`` accepts): ``violated`` when it reports
  errors, else ``satisfied``.

Exit codes: 0 ``satisfied``; 1 ``violated``, or ``missing`` after a command
that exited 0; the command's own code when it failed; 2 a usage error, an
absent or too-old llm-scripting-kit, a schema outside the subset, a
``--schema-digest`` that does not match the schema (the schema changed in
transit), a previous verdict or events file that cannot be removed, or (with
``--events``) a bootstrap_lib without execution-event schema v3 or ids that
are not valid event identities.

The verdict file (``workflow-kit.artifact-verdict/v1``) records the artifact,
kind, verdict, the judged path, ``bytes`` and ``sha256`` of exactly the bytes
judged (null when none were read), ``schema_digest`` for kind ``schema``, and
at most 100 ``[json_pointer, keyword]`` errors -- never a payload value -- with
``errors_truncated``. Lifecycle, for every execution whose arguments parse:

1. The previous verdict at ``--verdict`` is removed first, before any probe.
   An absent file is fine; any other error exits 2 having done nothing else.
   No directory is created.
2. Probes (kind ``schema`` only): ``llm_scripting_kit.completion`` exports
   ``OutputContract`` (with ``schema_digest``) and ``POLICY_VALIDATED_RESULT``,
   and ``json_schema.validate`` binds ``(schema, value)``. An absent and a
   too-old library get different messages; both exit 2 and write nothing.
3. The artifact is judged inside one ``try``/``finally`` whose cleanup writes
   the verdict (``missing`` when the body did not reach a judgment), so every
   path after the probes leaves a verdict. The write is atomic: a uniquely
   named temp file in the verdict's directory, then ``os.replace``. A failed
   write leaves no final file and no temp file. A cleanup failure is printed
   to stderr naming its layer and makes the exit non-zero; an exception from
   the body still propagates.

Each verdict path belongs to one (runId, step, item) execution. Two concurrent
executions writing one verdict path are unsupported: the last complete write
wins, and no final verdict is ever partial.

Execution events: with ``--events`` the checker records its judgment as ONE
``contract`` event in a ``plugins-kit.execution-event/v3`` JSONL stream at that
path (``source.plugin`` ``workflow-kit``, the given run and unit ids; payload
``artifact``, ``kind``, ``verdict``, ``schema_digest`` for kind ``schema`` and
``error_count`` for ``violated`` -- never a payload value or an error pointer,
which stay in the verdict file). It emits no ``terminal``: the checker judges
the artifact; it does not own the unit's lifecycle. The stream records the
node's last execution: a previous events file at ``--events`` is removed
together with the previous verdict, before any probe, with the same error
handling. The probe of ``bootstrap_lib.execution_event`` (schema v3 among
``SUPPORTED_SCHEMAS``, and the exact ``Emitter(..., schema=<v3>)``,
``make_event`` and ``JsonlSink(..., mode="truncate")`` calls) follows the
llm-scripting-kit probe; absent, supports-v1-but-not-v3 and otherwise too old
get three different messages, and every refusal exits 2 having written
nothing. The ``contract`` event is attempted exactly once, from the same
cleanup as the verdict, as an independent layer after it: it is recorded
unless the events sink itself raises, in which case the error is printed to
stderr and the exit is non-zero.

This module also owns the verdict writer, the cleanup layers, the
output-contract probe and the contract-event probe and payload that
``openrouter_run.py --provides`` uses, so both node runners write the same
files the same way.

Run it with workflow-kit's own venv interpreter, which bootstrap links
``llm_scripting_kit`` onto.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import inspect
import json
import os
import secrets
import sys
from pathlib import Path

#: The verdict file's format literal. Frozen.
VERDICT_SCHEMA = "workflow-kit.artifact-verdict/v1"

KIND_SCHEMA = "schema"
KIND_OPAQUE = "opaque-file"
KINDS = (KIND_SCHEMA, KIND_OPAQUE)

SATISFIED = "satisfied"
VIOLATED = "violated"
MISSING = "missing"

#: At most this many errors are recorded; ``errors_truncated`` says when more existed.
MAX_ERRORS = 100

#: The execution-event schema a stream holding a ``contract`` event is written
#: under. workflow-kit's own literal, never read from the (possibly stale)
#: linked module.
CONTRACT_EVENT_SCHEMA = "plugins-kit.execution-event/v3"

#: The first execution-event schema; a module that supports it but not
#: :data:`CONTRACT_EVENT_SCHEMA` gets its own message.
_EVENT_SCHEMA_V1 = "plugins-kit.execution-event/v1"

#: ``source.plugin`` of every event workflow-kit records.
EVENT_PLUGIN = "workflow-kit"

#: The one error a payload that is not UTF-8 JSON records.
UNPARSEABLE_ERRORS = (("", "unparseable"),)

_LSK_ABSENT = (
    "llm_scripting_kit not importable. Enable the llm-scripting-kit plugin and run this "
    "with workflow-kit's venv interpreter (bootstrap links llm_scripting_kit onto it via "
    "the shared-libs .pth)."
)


class ContractRefusal(Exception):
    """A schema or digest that must be refused before any judgment (exit 2)."""


def _declarations():
    """``workflow_kit_lib.declarations``: workflow-kit's own version constants."""
    try:
        from workflow_kit_lib import declarations  # noqa: PLC0415
    except ImportError:
        # workflow_kit_lib ships beside this script; use that copy when the
        # interpreter has none installed.
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from workflow_kit_lib import declarations  # noqa: PLC0415
    return declarations


def output_contract_lsk() -> str:
    """workflow-kit's own constant for the llm-scripting-kit release the contract calls need."""
    return _declarations().OUTPUT_CONTRACT_LSK


def contract_event_bootstrap() -> str:
    """workflow-kit's own constant for the bootstrap release a contract event needs."""
    return _declarations().CONTRACT_EVENT_BOOTSTRAP


# --------------------------------------------------------------------------- #
# The verdict file
# --------------------------------------------------------------------------- #
def _invalidate(path, what: str) -> str | None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"cannot invalidate the previous {what} at {path}: {exc}"
    return None


def invalidate_verdict(path) -> str | None:
    """Remove a previous verdict. None on success (an absent file included), else a message.

    Creates nothing: no directory, no file.
    """
    return _invalidate(path, "verdict")


def invalidate_events(path) -> str | None:
    """Remove a previous events file, exactly as :func:`invalidate_verdict` does."""
    return _invalidate(path, "events file")


def verdict_record(*, artifact, kind, verdict, path, data=None, schema_digest=None, errors=()):
    """The ``workflow-kit.artifact-verdict/v1`` object: pointers and keywords, never values."""
    pairs = [[str(pointer), str(keyword)] for pointer, keyword in errors]
    record = {
        "schema": VERDICT_SCHEMA,
        "artifact": artifact,
        "kind": kind,
        "verdict": verdict,
        "path": str(path),
        "bytes": None if data is None else len(data),
        "sha256": None if data is None else hashlib.sha256(data).hexdigest(),
    }
    if kind == KIND_SCHEMA:
        record["schema_digest"] = schema_digest
    record["errors"] = pairs[:MAX_ERRORS]
    record["errors_truncated"] = len(pairs) > MAX_ERRORS
    return record


def _serialize(record, fh) -> None:
    json.dump(record, fh, ensure_ascii=True, allow_nan=False)
    fh.write("\n")


def _temp_path(final: Path) -> Path:
    """A temp sibling unique per process (pid) and per call (random suffix)."""
    return final.with_name(f".{final.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")


def write_verdict(path, record) -> None:
    """Write ``record`` atomically at ``path``; on failure no final or temp file remains.

    The parent directory is created here, and only here.
    """
    final = Path(path)
    final.parent.mkdir(parents=True, exist_ok=True)
    temp = _temp_path(final)
    try:
        with open(temp, "x", encoding="ascii", newline="\n") as fh:
            _serialize(record, fh)
        os.replace(temp, final)
    finally:
        try:
            os.unlink(temp)
        except OSError:
            pass  # replaced (absent), or never created


# --------------------------------------------------------------------------- #
# Cleanup layers: independent, each error collected, never raised in place
# --------------------------------------------------------------------------- #
def run_cleanup(layers) -> list:
    """Run every ``(name, callable)`` layer in order; return ``[(name, exception)]``.

    A failing layer never skips the next one.
    """
    failures = []
    for name, layer in layers:
        try:
            layer()
        except Exception as exc:  # noqa: BLE001 -- every layer error is reported, none raised
            failures.append((name, exc))
    return failures


def report_cleanup_failures(failures) -> None:
    for name, exc in failures:
        print(f"workflow-kit: the {name} cleanup layer failed: {type(exc).__name__}: {exc}",
              file=sys.stderr)


def settle(rc: int, failures) -> int:
    """The exit code after cleanup: a non-zero body outcome wins; a cleanup failure is never 0."""
    if failures and rc == 0:
        return 1
    return rc


# --------------------------------------------------------------------------- #
# The output-contract probe and construction
# --------------------------------------------------------------------------- #
class ContractAPI:
    """The llm-scripting-kit symbols a schema-typed artifact uses."""

    def __init__(self, output_contract, policy, validate=None):
        self.OutputContract = output_contract
        self.policy = policy
        self.validate = validate


def probe_output_contract(*, need_validate: bool):
    """Return ``(ContractAPI, None)`` when usable, else ``(None, message)``.

    Absent and too-old are different repairs (install vs. update), so they get
    different messages. The version named is workflow-kit's own constant.
    """
    try:
        import llm_scripting_kit  # noqa: F401, PLC0415
    except ImportError:
        return None, _LSK_ABSENT
    reason = None
    validate = None
    try:
        from llm_scripting_kit.completion import (  # noqa: PLC0415
            POLICY_VALIDATED_RESULT,
            OutputContract,
        )
    except ImportError:
        reason = "no completion.OutputContract / POLICY_VALIDATED_RESULT"
    if reason is None:
        if not callable(OutputContract):
            reason = "OutputContract is not callable"
        else:
            try:
                inspect.signature(OutputContract).bind(
                    id="x", policy=POLICY_VALIDATED_RESULT, schema={}
                )
            except (TypeError, ValueError):
                reason = "OutputContract(id=, policy=, schema=) does not bind"
    if reason is None:
        try:
            fields = {f.name for f in dataclasses.fields(OutputContract)}
        except TypeError:
            fields = set()
        if "schema_digest" not in fields:
            reason = "OutputContract has no schema_digest"
    if reason is None and need_validate:
        try:
            from llm_scripting_kit.completion import json_schema  # noqa: PLC0415

            validate = json_schema.validate
            inspect.signature(validate).bind({}, None)
        except (ImportError, AttributeError, TypeError, ValueError):
            reason = "completion.json_schema.validate(schema, value) is missing or does not bind"
    if reason is None:
        return ContractAPI(OutputContract, POLICY_VALIDATED_RESULT, validate), None
    return None, (
        f"the linked llm_scripting_kit predates the output-contract calls a schema-typed "
        f"artifact needs ({reason}); this requires llm-scripting-kit >= {output_contract_lsk()}. "
        "Run `claude plugin update llm-scripting-kit@plugins-kit` and restart so bootstrap "
        "re-links the newer shared lib onto workflow-kit's venv."
    )


def _refuse_constant(name):
    raise ValueError(f"{name} is not JSON")


def build_contract(api: ContractAPI, artifact: str, schema_text: str, expected_digest: str):
    """``(OutputContract, parsed schema)``; raises :class:`ContractRefusal` (exit 2)."""
    try:
        schema = json.loads(schema_text, parse_constant=_refuse_constant)
    except ValueError as exc:
        raise ContractRefusal(f"--schema is not JSON: {exc}") from exc
    try:
        contract = api.OutputContract(
            id=f"workflow-kit.artifact.{artifact}", policy=api.policy, schema=schema
        )
    except (TypeError, ValueError) as exc:
        raise ContractRefusal(
            f"--schema for artifact {artifact!r} is outside what llm-scripting-kit's "
            f"validated-result contract accepts: {exc}"
        ) from exc
    if contract.schema_digest != expected_digest:
        raise ContractRefusal(
            f"--schema-digest {expected_digest} does not match the schema's digest "
            f"{contract.schema_digest}: the schema changed in transit"
        )
    return contract, schema


# --------------------------------------------------------------------------- #
# The contract event (execution-event schema v3)
# --------------------------------------------------------------------------- #
_BOOTSTRAP_ABSENT = (
    "--events needs bootstrap_lib.execution_event, but bootstrap_lib is not "
    "importable: the plugins-kit:bootstrap plugin has not provisioned "
    "workflow-kit here. Run `claude plugin install bootstrap@plugins-kit`, start "
    "a new session, and run this with workflow-kit's venv python."
)


def probe_contract_events():
    """Return ``(execution_event module, None)`` when usable, else ``(None, message)``.

    Three states get three messages: bootstrap_lib absent (install); a module
    that supports schema v1 but not v3 (update, naming what v3 is for); any
    other stale module, including an ``Emitter`` without ``schema=`` (update).
    The version named is workflow-kit's own constant.
    """
    try:
        import bootstrap_lib  # noqa: F401, PLC0415
    except ImportError:
        return None, _BOOTSTRAP_ABSENT
    try:
        from bootstrap_lib import execution_event  # noqa: PLC0415
    except ImportError:
        execution_event = None
    supported = getattr(execution_event, "SUPPORTED_SCHEMAS", None) or ()
    if _EVENT_SCHEMA_V1 in supported and CONTRACT_EVENT_SCHEMA not in supported:
        return None, (
            f"the linked bootstrap_lib.execution_event supports {_EVENT_SCHEMA_V1} but not "
            "/v3, which workflow-kit needs to record contract events: update the bootstrap "
            f"plugin to >= {contract_event_bootstrap()}. Run `claude plugin update "
            "bootstrap@plugins-kit` and restart so bootstrap re-links the newer shared lib "
            "onto workflow-kit's venv."
        )
    reason = None
    if execution_event is None:
        reason = "no bootstrap_lib.execution_event"
    elif CONTRACT_EVENT_SCHEMA not in supported:
        reason = f"schema {CONTRACT_EVENT_SCHEMA} is not supported"
    elif not all(
        callable(getattr(execution_event, name, None))
        for name in ("Emitter", "JsonlSink", "make_event")
    ):
        reason = "Emitter, JsonlSink or make_event is missing"
    else:
        try:
            inspect.signature(execution_event.Emitter).bind(
                EVENT_PLUGIN, "r", unit_id="u", sinks=(), schema=CONTRACT_EVENT_SCHEMA
            )
            inspect.signature(execution_event.make_event).bind(
                seq=0, run_id="r", event="contract", plugin=EVENT_PLUGIN, unit_id="u",
                payload={}, schema=CONTRACT_EVENT_SCHEMA,
            )
            inspect.signature(execution_event.JsonlSink).bind("p", mode="truncate")
        except (TypeError, ValueError):
            reason = (
                "Emitter(..., schema=), make_event(..., schema=) or "
                "JsonlSink(..., mode='truncate') does not bind"
            )
    if reason is None:
        return execution_event, None
    return None, (
        f"the linked bootstrap_lib predates the execution-event calls a contract event needs "
        f"({reason}); this requires bootstrap >= {contract_event_bootstrap()}. Run `claude "
        "plugin update bootstrap@plugins-kit` and restart so bootstrap re-links the newer "
        "shared lib onto workflow-kit's venv."
    )


def contract_payload(*, artifact, kind, verdict, schema_digest=None, errors=()):
    """The closed ``contract`` payload: identity, kind and verdict; never content."""
    payload = {"artifact": artifact, "kind": kind, "verdict": verdict}
    if kind == KIND_SCHEMA:
        payload["schema_digest"] = schema_digest
    if verdict == VIOLATED:
        payload["error_count"] = len(tuple(errors))
    return payload


def check_contract_event(execution_event, run_id, unit_id, payload) -> str | None:
    """None when a ``contract`` event with these ids and ``payload`` is valid, else a message.

    Builds (and discards) one v3 event before anything is written, so a bad
    run id, unit id or artifact name is refused up front rather than lost from
    the stream after the judgment.
    """
    try:
        execution_event.make_event(
            seq=0, run_id=run_id, event="contract", plugin=EVENT_PLUGIN, unit_id=unit_id,
            payload=payload, schema=CONTRACT_EVENT_SCHEMA,
        )
    except ValueError as exc:  # EventError is a ValueError
        return f"--run-id/--unit-id or the artifact name are not valid event identities: {exc}"
    return None


def open_contract_stream(execution_event, path, run_id, unit_id):
    """A v3 ``Emitter`` writing a fresh stream at ``path`` (its directory created here)."""
    events = Path(path)
    events.parent.mkdir(parents=True, exist_ok=True)
    return execution_event.Emitter(
        EVENT_PLUGIN,
        run_id,
        unit_id=unit_id,
        sinks=[execution_event.JsonlSink(events, mode="truncate")],
        schema=CONTRACT_EVENT_SCHEMA,
    )


# --------------------------------------------------------------------------- #
# The checker
# --------------------------------------------------------------------------- #
def judge(kind: str, in_path, schema=None, validate=None):
    """``(verdict, judged bytes or None, errors)``. An OSError while reading propagates."""
    path = Path(in_path)
    if not path.is_file():
        return MISSING, None, ()
    data = path.read_bytes()
    if kind == KIND_OPAQUE:
        return SATISFIED, data, ()
    try:
        value = json.loads(data.decode("utf-8"), parse_constant=_refuse_constant)
    except (ValueError, RecursionError):  # UnicodeDecodeError is a ValueError
        return VIOLATED, data, UNPARSEABLE_ERRORS
    errors = tuple(validate(schema, value))
    return (VIOLATED if errors else SATISFIED), data, errors


def _clamp_exit(code: int) -> int:
    return max(1, min(255, code))


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="workflow-kit: judge a script node's $OUT against its declared artifact type."
    )
    ap.add_argument("--artifact", required=True, help="the provided artifact's name")
    ap.add_argument("--kind", required=True, choices=KINDS, help="the artifact's kind")
    ap.add_argument("--in", dest="in_path", required=True, help="the file to judge ($OUT)")
    ap.add_argument("--verdict", required=True, help="write the verdict JSON here")
    ap.add_argument("--command-exit", type=int, required=True,
                    help="the exit status of the command that wrote --in")
    ap.add_argument("--schema", help="the artifact's JSON Schema, as JSON text (kind schema)")
    ap.add_argument("--schema-digest",
                    help="sha256 of the schema's canonical JSON (kind schema)")
    ap.add_argument("--events",
                    help="record one contract event in a plugins-kit.execution-event/v3 "
                    "JSONL stream here (replaced on each run); requires --run-id and --unit-id")
    ap.add_argument("--run-id", help="the workflow run id the event carries (with --events)")
    ap.add_argument("--unit-id", help="the node's unit id the event carries (with --events)")
    return ap


def main(argv=None) -> int:
    ap = _parser()
    args = ap.parse_args(argv)
    if not args.artifact:
        ap.error("--artifact needs a non-empty name")
    if args.kind == KIND_SCHEMA and not (args.schema and args.schema_digest):
        ap.error("--kind schema requires --schema and --schema-digest")
    if args.kind == KIND_OPAQUE and (args.schema is not None or args.schema_digest is not None):
        ap.error("--kind opaque-file takes no --schema or --schema-digest")
    if args.events and not (args.run_id and args.unit_id):
        ap.error("--events requires --run-id and --unit-id")

    # First action of a parsed execution: no refusal below may leave a
    # previous execution's verdict or events stream on disk.
    message = invalidate_verdict(args.verdict)
    if message is None and args.events:
        message = invalidate_events(args.events)
    if message is not None:
        print(message, file=sys.stderr)
        return 2

    contract = None
    schema = None
    validate = None
    if args.kind == KIND_SCHEMA:
        api, message = probe_output_contract(need_validate=True)
        if message is not None:
            print(message, file=sys.stderr)
            return 2
        try:
            contract, schema = build_contract(api, args.artifact, args.schema, args.schema_digest)
        except ContractRefusal as exc:
            print(str(exc), file=sys.stderr)
            return 2
        validate = api.validate

    outcome = {"verdict": MISSING, "data": None, "errors": ()}

    def event_payload():
        return contract_payload(
            artifact=args.artifact,
            kind=args.kind,
            verdict=outcome["verdict"],
            schema_digest=None if contract is None else contract.schema_digest,
            errors=outcome["errors"],
        )

    execution_event = None
    if args.events:
        # Before any directory or write: a refusal leaves nothing on disk.
        execution_event, message = probe_contract_events()
        if message is None:
            message = check_contract_event(
                execution_event, args.run_id, args.unit_id, event_payload()
            )
        if message is not None:
            print(message, file=sys.stderr)
            return 2

    def write_layer():
        write_verdict(args.verdict, verdict_record(
            artifact=args.artifact,
            kind=args.kind,
            verdict=outcome["verdict"],
            path=args.in_path,
            data=outcome["data"],
            schema_digest=None if contract is None else contract.schema_digest,
            errors=outcome["errors"],
        ))

    layers = [("verdict", write_layer)]
    if execution_event is not None:
        def contract_layer():
            # Exactly one attempt, after the verdict layer, whatever it did.
            open_contract_stream(
                execution_event, args.events, args.run_id, args.unit_id
            ).emit("contract", payload=event_payload())

        layers.append(("contract", contract_layer))

    rc = 1
    failures = []
    try:
        if args.command_exit != 0:
            rc = _clamp_exit(args.command_exit)
        else:
            try:
                verdict, data, errors = judge(args.kind, args.in_path, schema, validate)
            except OSError as exc:
                print(f"cannot read {args.in_path}: {exc}", file=sys.stderr)
                verdict, data, errors = MISSING, None, ()
            outcome.update(verdict=verdict, data=data, errors=errors)
            rc = 0 if verdict == SATISFIED else 1
    finally:
        failures = run_cleanup(layers)
        report_cleanup_failures(failures)
    return settle(rc, failures)


if __name__ == "__main__":
    raise SystemExit(main())
