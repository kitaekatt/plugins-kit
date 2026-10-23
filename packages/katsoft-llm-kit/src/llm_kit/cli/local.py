"""``llm-kit local``: the local llama-swap model swapper and host-neutral acceptance runs.

Carries the legacy ``swapper`` and ``acceptance`` verbs. Every exception the
swapper library raises carries its own ``exit_code`` (1, 2, 3 or 5) and the
group maps errors by reading it; ``guard-launch`` refuses with exit 3.
"""
from __future__ import annotations

import argparse
import dataclasses
import sys
from typing import Sequence

from llm_kit.adapters.swapper import (
    LISTENER_GRACE_S,
    SwapperClient,
    SwapperError,
    check_launch_allowed,
    find_strays,
    resolve_swapper_url,
    terminate_listener,
    terminate_model,
)
from llm_kit.cli._common import (
    EXIT_FAILURE,
    EXIT_OK,
    EXIT_USAGE,
    _json,
    guarded,
)


#: Bumped only if the swapper verbs' JSON shape changes incompatibly.
SWAPPER_PROTOCOL_VERSION = 1


def _swapper_error_kind(exc: SwapperError) -> str:
    """The exception's class name as a kebab-case ``error.kind`` string.

    A mechanical name transform, not a per-exception decision: it is what
    keeps the CLI from having to know each swapper exception individually.
    ``NonLocalTargetError`` -> ``non-local-target``,
    ``InspectionIndeterminate`` -> ``inspection-indeterminate``.
    """
    name = type(exc).__name__
    if name.endswith("Error"):
        name = name[: -len("Error")]
    out: list[str] = []
    for ch in name:
        if ch.isupper() and out:
            out.append("-")
        out.append(ch.lower())
    return "".join(out)


def _swapper_client(args: argparse.Namespace) -> SwapperClient:
    url = resolve_swapper_url(endpoint=args.endpoint, url=args.url)
    return SwapperClient(url)


def _cmd_swapper_running(args: argparse.Namespace) -> int:
    client = _swapper_client(args)
    models = client.running()
    if args.format == "text":
        for m in models:
            print(f"{m.model}\t{m.state}\t{m.proxy}")
    else:
        _json({
            "protocol": SWAPPER_PROTOCOL_VERSION,
            "endpoint": client.base_url,
            "models": [dataclasses.asdict(m) for m in models],
        })
    return EXIT_OK


def _cmd_swapper_terminate(args: argparse.Namespace) -> int:
    client = _swapper_client(args)
    result = terminate_model(client, args.model, grace_s=args.grace_seconds)
    if args.format == "text":
        note = " (escalated to SIGKILL)" if result.escalated else ""
        print(f"{args.model}: sent {result.signal} to pid {result.process.pid}{note}")
    else:
        _json({
            "protocol": SWAPPER_PROTOCOL_VERSION,
            "endpoint": client.base_url,
            "model": args.model,
            "pid": result.process.pid,
            "swapper_pid": result.process.swapper_pid,
            "signal": result.signal,
            "escalated": result.escalated,
        })
    return EXIT_OK


def _cmd_swapper_unload(args: argparse.Namespace) -> int:
    client = _swapper_client(args)
    unloaded = client.unload_all()
    if args.format == "text":
        if not unloaded:
            print("no models were resident")
        for m in unloaded:
            print(f"unloaded {m.model}")
    else:
        _json({
            "protocol": SWAPPER_PROTOCOL_VERSION,
            "endpoint": client.base_url,
            "unloaded": [dataclasses.asdict(m) for m in unloaded],
        })
    return EXIT_OK


def _cmd_swapper_strays(args: argparse.Namespace) -> int:
    strays = find_strays()
    if args.format == "text":
        if not strays:
            print("no strays found")
        for rec in strays:
            print(f"{rec.pid}\t{rec.name}\towner={rec.owner}")
    else:
        _json({
            "protocol": SWAPPER_PROTOCOL_VERSION,
            "strays": [dataclasses.asdict(rec) for rec in strays],
        })
    # Contract: 0 when none are found, 1 when strays are found -- the same
    # "operation failure, safety refusal, or strays found" bucket every other
    # swapper verb's EXIT_FAILURE means.
    return EXIT_FAILURE if strays else EXIT_OK


def _cmd_swapper_terminate_listener(args: argparse.Namespace) -> int:
    result = terminate_listener(args.port, grace_s=args.grace_seconds)
    if args.format == "text":
        if result.action == "none":
            print(f"port {result.port} is free; nothing to terminate")
        else:
            note = " (escalated to SIGKILL)" if result.escalated else ""
            print(f"port {result.port}: sent {result.signal} to pid {result.pid}{note}; port is free")
    else:
        _json({
            "protocol": SWAPPER_PROTOCOL_VERSION,
            "port": result.port,
            "action": result.action,
            "pid": result.pid,
            "create_time": result.create_time,
            "signal": result.signal,
            "escalated": result.escalated,
        })
    return EXIT_OK


def _cmd_swapper_guard_launch(args: argparse.Namespace) -> int:
    """Quiet on success. A refusal raises ``LaunchRefused`` (exit 3), caught
    by ``_cmd_swapper``, which is the only exit code a caller
    (a host launcher script) treats as a refusal."""
    check_launch_allowed(caller_pid=args.caller_pid)
    return EXIT_OK


def _cmd_swapper(args: argparse.Namespace) -> int:
    try:
        if args.swapper_action == "running":
            return _cmd_swapper_running(args)
        if args.swapper_action == "terminate":
            return _cmd_swapper_terminate(args)
        if args.swapper_action == "unload":
            return _cmd_swapper_unload(args)
        if args.swapper_action == "strays":
            return _cmd_swapper_strays(args)
        if args.swapper_action == "terminate-listener":
            return _cmd_swapper_terminate_listener(args)
        if args.swapper_action == "guard-launch":
            return _cmd_swapper_guard_launch(args)
    except SwapperError as exc:
        _json(
            {"error": {"kind": _swapper_error_kind(exc), "message": str(exc)}},
            stream=sys.stderr,
        )
        return exc.exit_code
    return EXIT_USAGE


def _add_swapper_target_arg(parser: argparse.ArgumentParser) -> None:
    """``--endpoint`` or ``--url``, exactly one -- the swapper this verb targets."""
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--endpoint", help="A configured model-endpoints registry entry naming the swapper.")
    target.add_argument("--url", help="The swapper's base URL directly.")


def _add_swapper_format_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--format", choices=("json", "text"), default="json",
        help="Output format (default json).",
    )


def _add_acceptance_format_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--format", choices=("text", "json"), default="text", help="Output format (default text)."
    )


def _cmd_acceptance(args: argparse.Namespace) -> int:
    from llm_kit.adapters.swapper_acceptance import AcceptanceConfigError, run_swapper_acceptance  # noqa: PLC0415

    try:
        if args.acceptance_action == "swapper":
            report = run_swapper_acceptance(
                args.url, args.rounds, settle_s=args.settle_seconds, poll_s=args.poll_seconds
            )
        else:
            from llm_kit.frontdoor.acceptance import run_frontdoor_acceptance  # noqa: PLC0415

            report = run_frontdoor_acceptance(
                args.url, args.registry, spill_group=args.spill_group, queue_group=args.queue_group,
                quick=args.quick, paid=args.paid, expect_tier1_cap=args.expect_tier1_cap,
            )
    except AcceptanceConfigError as exc:
        _json({"error": {"kind": "configuration", "message": str(exc)}}, stream=sys.stderr)
        return EXIT_USAGE
    if args.format == "json":
        _json(report.to_json())
    else:
        print(report.to_text())
    return report.exit_code


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="llm-kit local",
        description="The local llama-swap model swapper and host-neutral acceptance runs.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    swapper_cmd = sub.add_parser("swapper", help="Manage a llama-swap local model swapper.")
    swapper_sub = swapper_cmd.add_subparsers(dest="swapper_action", required=True)

    swapper_running = swapper_sub.add_parser("running", help="List models currently resident on the swapper.")
    _add_swapper_target_arg(swapper_running)
    _add_swapper_format_arg(swapper_running)

    swapper_terminate = swapper_sub.add_parser(
        "terminate", help="SIGTERM (then SIGKILL on timeout) the verified server for one model."
    )
    swapper_terminate.add_argument("model", help="The model name as reported by /running.")
    _add_swapper_target_arg(swapper_terminate)
    swapper_terminate.add_argument(
        "--grace-seconds", type=float, default=10.0,
        help="Seconds to wait after SIGTERM before escalating to SIGKILL (default 10).",
    )
    _add_swapper_format_arg(swapper_terminate)

    swapper_unload = swapper_sub.add_parser("unload", help="Unload every model resident on the swapper.")
    _add_swapper_target_arg(swapper_unload)
    swapper_unload.add_argument(
        "--all", action="store_true", required=True,
        help="Confirm unloading every resident model (required).",
    )
    swapper_unload.add_argument(
        "--accept-no-drain", action="store_true", required=True,
        help="Confirm in-flight requests are not drained before unload (required).",
    )
    _add_swapper_format_arg(swapper_unload)

    swapper_strays = swapper_sub.add_parser(
        "strays", help="Report recognized model servers with no swapper ancestor."
    )
    _add_swapper_format_arg(swapper_strays)

    swapper_terminate_listener = swapper_sub.add_parser(
        "terminate-listener",
        help=(
            "Explicit replacement: stop the one recognized model server "
            "listening on a local port so another can start."
        ),
    )
    swapper_terminate_listener.add_argument(
        "--port", type=int, required=True, help="The local TCP port to free."
    )
    swapper_terminate_listener.add_argument(
        "--accept-replace", action="store_true", required=True,
        help="Confirm the listener on the port may be terminated (required).",
    )
    swapper_terminate_listener.add_argument(
        "--grace-seconds", type=float, default=LISTENER_GRACE_S,
        help=f"Seconds to wait after SIGTERM before SIGKILL (default {LISTENER_GRACE_S:g}).",
    )
    _add_swapper_format_arg(swapper_terminate_listener)

    swapper_guard_launch = swapper_sub.add_parser(
        "guard-launch",
        help=(
            "Refuse (exit 3) a manual launch beside an active same-user "
            "swapper. Quiet on success."
        ),
    )
    swapper_guard_launch.add_argument(
        "--caller-pid", type=int, required=True, help="The launching process's PID."
    )

    acceptance = sub.add_parser(
        "acceptance",
        help="Host-neutral acceptance runs (exit 0 passed, 1 assertion failed, 2 usage/config).",
    )
    acceptance_sub = acceptance.add_subparsers(dest="acceptance_action", required=True)
    acc_swapper = acceptance_sub.add_parser(
        "swapper", help="Residency, exact answers and constructed never-evict overlap on a llama-swap."
    )
    acc_swapper.add_argument("--url", required=True, help="The swapper's base URL.")
    acc_swapper.add_argument("--rounds", type=int, required=True, help="Alternating rounds (at least 1).")
    acc_swapper.add_argument(
        "--settle-seconds", type=float, default=3.0,
        help="Seconds into a round before residency is asserted (default 3).",
    )
    acc_swapper.add_argument(
        "--poll-seconds", type=float, default=1.0, help="/running polling interval (default 1)."
    )
    _add_acceptance_format_arg(acc_swapper)
    acc_front = acceptance_sub.add_parser(
        "frontdoor", help="Fill/spill, deployment attribution and queue-not-reject on a front door."
    )
    acc_front.add_argument("--url", required=True, help="The front door's base URL.")
    acc_front.add_argument("--registry", required=True, help="The model-endpoints registry the front door serves.")
    acc_front.add_argument("--spill-group", required=True, help="Routing group used for the fill/spill legs.")
    acc_front.add_argument(
        "--queue-group", required=True,
        help="Routing group whose tiers are all capped, used for the queue-not-reject legs.",
    )
    acc_front.add_argument("--quick", action="store_true", help="Fill legs only.")
    acc_front.add_argument(
        "--paid", action="store_true",
        help="Allow legs that spill to a paid tier (a tier is paid unless the registry declares billing.mode: unmetered).",
    )
    acc_front.add_argument(
        "--expect-tier1-cap", type=int, default=None,
        help="Counterfactual: expect this many requests on the spill group's first tier; a wrong value exits 1.",
    )
    _add_acceptance_format_arg(acc_front)
    return parser


def run(argv: Sequence[str]) -> int:
    """Run one ``local`` verb and return its exit code."""
    args = _parser().parse_args(list(argv))

    def dispatch() -> int:
        if args.cmd == "swapper":
            return _cmd_swapper(args)
        if args.cmd == "acceptance":
            return _cmd_acceptance(args)
        return EXIT_USAGE

    return guarded(dispatch)
