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

The ``protocol`` command (A-min.3) -- one JSON envelope, preferably on stdin
--------------------------------------------------------------------------------
When ``adapter`` is supplied, :func:`build_commands` additionally registers a
single ``"protocol"`` command carrying one JSON-encoded worker-protocol
envelope (``execution.protocol``'s ``{"protocol_version": ..., "verb": ...,
"payload": {...}}`` shape); its result is whatever
:func:`~content_pipeline.execution.protocol.dispatch` returns. This still
honors the placement rule -- reading the envelope plus one call to
``protocol.dispatch`` is argv-shaping, not execution logic; every verb's
actual behavior (claim math, evaluation, apply) lives in ``execution/``, same
as every other command here. ``**protocol_policy`` forwards to
:func:`~content_pipeline.execution.protocol.build_handlers` (``strategy``,
``gates``, ``freshness_of``, etc.) -- see that function's docstring for which
verbs need which of them.

**Three ways to supply the envelope, in preference order:**

1. **stdin (preferred, documented default).** ``protocol`` with no positional
   argument, or an explicit ``-``, reads the envelope from stdin as UTF-8
   bytes. This is the form to document and to use.
2. **``@<path>``.** A positional argument starting with ``@`` reads the
   envelope from that file, also as UTF-8. This is the WORKER-LANE form: a
   ``claude --bg`` worker session (``execution/drivers/claude_bg.py``'s
   ``enumerate_worker_invocations``) has no practical way to compose a shell
   redirect into stdin, but writing a small JSON file with the Write tool and
   naming it in an otherwise-constant argv string is exactly what keeps a
   pre-authorized allowlist entry possible (P5) -- see that module's
   docstring.
3. **Positional argv (discouraged, kept for back-compat).** A positional
   argument that is neither absent, ``-``, nor ``@``-prefixed is treated as
   the literal envelope JSON, exactly as A-min.3 originally shipped it in
   0.9.0. Kept working so existing callers do not break, but discouraged: see
   below for why.

**``--text-file=<path>`` (worker-lane companion to ``@<path>``).** When
present, this flag's file is read as UTF-8 and spliced into the decoded
envelope's ``payload["text"]`` BEFORE the envelope reaches
:func:`~content_pipeline.execution.protocol.dispatch` -- it never touches
``execution/protocol.py`` itself, which still knows nothing about files; this
module owns the splice, same as it owns envelope sourcing. It exists because
a worker's ``submit`` envelope carries a fencing token only known at runtime
(so it cannot be part of a pre-allowlisted, deterministic invocation string --
P5) while the answer TEXT can be arbitrarily long and is exactly the kind of
content that does not belong in a command line at all. Splitting the two --
``@<path>`` for the small, worker-authored envelope; ``--text-file=`` for the
large, freeform answer -- keeps both inputs out of argv while letting the
overall invocation string stay constant across every unit. The flag MUST use
the ``--key=value`` form (``_split_flags`` below only recognizes ``=``-joined
flags as taking a value; ``--text-file <path>`` would parse as a bare boolean
flag plus a stray positional, and the submission would see no text at all).
A file argument that cannot be read (missing, not UTF-8) returns the same
typed ``{"ok": false, "error": {...}}`` shape as a bad ``@<path>`` envelope
file, never a bare traceback.

**The spliced file is FENCED, and the fence is checked here.** The answer
path is deliberately generation-neutral (see
``execution/drivers/claude_bg.py``'s ``answer_path_for``: no ``worker_id``,
because P5 allowlisting needs the path computable before the run), so two
successive dispatches of one unit write the SAME file -- and a session left
alive by an earlier dispatch can overwrite it while a newer worker is
running. Fencing the envelope's TOKEN alone does not catch that: the newer
worker's envelope is perfectly valid, and it would splice whatever text
currently sits at the path. So the ARTIFACT declares its own generation:
its first line is ``content-pipeline-fence: <token>``, and this splice
matches that declaration against ``payload["fencing_token"]`` before
handing anything to ``protocol.dispatch``
(``claude_bg.parse_fenced_answer``). A stale artifact under a current
envelope, a current artifact under a stale envelope, and an artifact with
no fence line at all are each refused with a typed reply -- never spliced
and never treated as unfenced-and-fine. Only the first line is interpreted,
so answer text that itself contains the prefix passes through untouched.

**Why stdin is preferred, not merely tidier.** With the envelope in argv,
every unit produces a DIFFERENT command string (the JSON payload varies per
call), so a permission allowlist that must match exact command strings can
never cover it -- each unit's invocation looks like a new, unreviewed
command. On stdin, the command string invoked (mount, flags, ``protocol``,
nothing else) is CONSTANT across every unit and becomes a single allowlist
entry. Argv also breaks structurally on Windows: when a `.bat` wrapper sits
anywhere in the invocation chain, `cmd.exe` re-parses the command line, and
its quote-state tracking is confused by the escaped inner quotes JSON
requires -- a `|` inside the envelope (as YAML block scalars like
``reasoning: |`` produce) is then read as a pipe operator once the parser
believes itself outside quotes, and the rest of the JSON becomes a bogus
command (observed: exit 255, ``'\\n' is not recognized as an internal or
external command``). stdin has no such re-parsing step.

Envelope text is always decoded as UTF-8 explicitly (never the platform
default -- the Windows console default is cp1252, which corrupts or crashes
on the non-ASCII text a real envelope carries, e.g. zh-Hans payload values).
An empty stdin, malformed JSON, or a missing ``@`` file each return a typed
``{"ok": false, "error": {...}}`` reply -- the same shape
:func:`~content_pipeline.execution.protocol.dispatch` already uses for a bad
envelope -- rather than raising a bare traceback.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
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
        """Create a run. Echoes ``adapter_version`` and ``environment`` in
        its result -- a value the caller cannot see is a value it cannot
        check, and this is the surface a caller uses to confirm what was
        actually stored (see below).

        ``adapter_version`` validation (defect 2): when this mount has an
        ``adapter`` (the ``build_commands(..., adapter=...)`` case), a
        supplied ``adapter_version`` that disagrees with the mounted
        adapter's own ``adapter.adapter_version`` is REFUSED -- a run stored
        with a version the live adapter does not report would fail every
        subsequent protocol verb with ``AdapterVersionMismatchError`` (a run
        that can never be claimed). When the positional ``adapter_version``
        is omitted and an adapter is mounted, it defaults to the adapter's
        own reported ``adapter_version``, since the adapter is the
        authoritative source for its own identity. A mount with no adapter
        (``adapter=None``, the A-min.1/A-min.2 store-only shape) performs
        neither check nor default -- ``adapter_version`` stays a plain
        required positional, since there is no live adapter to validate or
        default against.

        ``environment`` (item 5, A-min.4): when this mount has an
        ``adapter``, its ``environment.snapshot()`` is taken automatically,
        RIGHT HERE, in the orchestrator's own shell -- the anchor is only
        correct taken at this point, not re-derived later by a worker. A
        declared, path-looking required var that does not resolve to
        ``os.getcwd()`` EXACTLY refuses the create entirely (DECIDED: this
        is what catches a Git Bash ``PWD`` vs native ``os.getcwd()``
        mismatch cheaply, in the human's own shell, before any worker ever
        runs -- see ``execution.adapter.require_creatable_environment``). A
        mount with no adapter records no snapshot, same as today.
        """
        positional, _flags = _split_flags(args)
        run_id = _require(positional, 0, "run_id")
        driver = _require(positional, 1, "driver")
        backend = _require(positional, 2, "backend")
        model = _require(positional, 3, "model")
        if len(positional) > 4:
            adapter_version = positional[4]
        elif adapter is not None:
            adapter_version = adapter.adapter_version
        else:
            raise ValueError("missing required argument: adapter_version")
        if adapter is not None and adapter_version != adapter.adapter_version:
            raise ValueError(
                f"adapter_version {adapter_version!r} does not match the mounted "
                f"adapter's reported version {adapter.adapter_version!r}; refusing "
                "to create a run this adapter could never claim -- see "
                "execution.adapter.require_compatible_adapter"
            )

        environment_snapshot: Optional[Dict[str, str]] = None
        if adapter is not None:
            # Deferred import: only an adapter-mounted create needs
            # execution.adapter's environment machinery at all.
            from content_pipeline.execution.adapter import require_creatable_environment

            environment_snapshot = adapter.environment.snapshot()
            require_creatable_environment(run_id, adapter.environment, environment_snapshot)

        run = store.create_run(
            run_id,
            driver=driver,
            backend=backend,
            model=model,
            adapter_version=adapter_version,
            environment=environment_snapshot,
        )
        return {
            "id": run.id,
            "driver": run.driver,
            "backend": run.backend,
            "model": run.model,
            "adapter_version": run.adapter_version,
            "environment": run.environment,
        }

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
            positional, flags = _split_flags(args)
            if not positional or positional[0] == "-":
                # Preferred form (see module docstring): stdin, decoded as
                # UTF-8 explicitly -- never the platform default, which on
                # Windows is cp1252 and corrupts/crashes on non-ASCII
                # envelope content.
                raw = sys.stdin.buffer.read()
                try:
                    envelope_text = raw.decode("utf-8")
                except UnicodeDecodeError as exc:
                    return {
                        "ok": False,
                        "error": {
                            "type": "MalformedEnvelopeError",
                            "message": f"stdin is not valid UTF-8: {exc}",
                        },
                    }
                if not envelope_text.strip():
                    return {
                        "ok": False,
                        "error": {
                            "type": "EmptyEnvelopeError",
                            "message": "no envelope on stdin (input was empty)",
                        },
                    }
            elif positional[0].startswith("@"):
                path = positional[0][1:]
                try:
                    envelope_text = Path(path).read_text(encoding="utf-8")
                except FileNotFoundError:
                    return {
                        "ok": False,
                        "error": {
                            "type": "MissingEnvelopeFileError",
                            "message": f"envelope file not found: {path!r}",
                        },
                    }
                except OSError as exc:
                    return {
                        "ok": False,
                        "error": {
                            "type": "MissingEnvelopeFileError",
                            "message": f"could not read envelope file {path!r}: {exc}",
                        },
                    }
            else:
                # Discouraged back-compat form: the literal envelope JSON as
                # the positional argv token (0.9.0's original shape). See
                # module docstring for why this is kept but not preferred.
                envelope_text = positional[0]

            try:
                envelope = json.loads(envelope_text)
            except json.JSONDecodeError as exc:
                return {
                    "ok": False,
                    "error": {"type": "MalformedEnvelopeError", "message": f"invalid JSON: {exc}"},
                }

            if "text-file" in flags:
                # Worker-lane companion to '@<path>' (see module docstring):
                # splice a UTF-8 file's content into payload["text"] BEFORE
                # dispatch. Trusted argv, never the payload -- the path comes
                # from the invocation string a mount owner pre-authorized,
                # not from anything inside the envelope itself. Must use the
                # '--text-file=<path>' form: _split_flags only recognizes an
                # '='-joined flag as carrying a value.
                text_path = flags["text-file"]
                try:
                    text_content = Path(text_path).read_text(encoding="utf-8")
                except FileNotFoundError:
                    return {
                        "ok": False,
                        "error": {
                            "type": "MissingTextFileError",
                            "message": f"text file not found: {text_path!r}",
                        },
                    }
                except OSError as exc:
                    return {
                        "ok": False,
                        "error": {
                            "type": "MissingTextFileError",
                            "message": f"could not read text file {text_path!r}: {exc}",
                        },
                    }
                except UnicodeDecodeError as exc:
                    return {
                        "ok": False,
                        "error": {
                            "type": "MalformedEnvelopeError",
                            "message": f"text file {text_path!r} is not valid UTF-8: {exc}",
                        },
                    }
                if isinstance(envelope, dict):
                    payload = envelope.get("payload")
                    if not isinstance(payload, dict):
                        payload = {}
                    # The answer artifact's own fence (see the module
                    # docstring's "--text-file=" section): the file's first
                    # line declares the fencing token its text was produced
                    # under, and it must equal the token this envelope
                    # presents. Deferred import so a mount that never uses
                    # the worker lane never pays for the driver module.
                    from content_pipeline.execution.drivers.claude_bg import (
                        AnswerFenceError,
                        parse_fenced_answer,
                    )

                    try:
                        expected_token = int(payload["fencing_token"])
                    except (KeyError, TypeError, ValueError):
                        return {
                            "ok": False,
                            "error": {
                                "type": "MissingAnswerFenceError",
                                "message": (
                                    "'--text-file=' requires the envelope to "
                                    "carry an integer payload 'fencing_token' "
                                    "to match the answer artifact's fence "
                                    "line against"
                                ),
                            },
                        }
                    try:
                        text_content = parse_fenced_answer(text_content, expected_token)
                    except AnswerFenceError as exc:
                        return {
                            "ok": False,
                            "error": {"type": type(exc).__name__, "message": str(exc)},
                        }
                    payload["text"] = text_content
                    envelope["payload"] = payload

            return protocol_dispatch(envelope, handlers)

        commands["protocol"] = Command(
            name="protocol",
            handler=protocol,
            help=(
                "Dispatch one JSON worker-protocol envelope (execution.protocol). "
                "Reads stdin by default (preferred); '@<path>' reads a file (the "
                "worker-lane form) -- optionally paired with "
                "'--text-file=<path>' to splice a UTF-8 file's content into "
                "payload['text']; a literal JSON positional argument is "
                "accepted for back-compat but discouraged -- see this "
                "module's docstring."
            ),
        )

    return commands


__all__ = ["build_commands"]
