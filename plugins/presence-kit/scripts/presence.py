"""presence-kit CLI: config, state get|set, recap-done and the Stop hook verb.

Stdlib only; sibling imports. Contract: dev/tasks/presence-kit/contracts.md
sections 1, 2, 5 and 11. Every failure of `config`, `state` and `recap-done`
prints ONE object `{"error": class, "kind": kind, "message": text}` on stdout
and the same text on stderr (usage errors exit 2, other failures exit 1).
`stop` never prints on failure: stderr only, empty stdout, exit 1.

The `prompt` verb (UserPromptSubmit hook) is unit K3 and is not part of this
CLI yet: it is an unknown verb here (usage error, exit 2).
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import traceback
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

sys.path.insert(0, str(Path(__file__).resolve().parent))

import presence_config as pc  # noqa: E402
import presence_state as ps  # noqa: E402
import presence_stop as pstop  # noqa: E402

PROG = "presence.py"
ATTENDED_ENV = "CLAUDE_CODE_SESSION_ATTENDED"
SESSION_ENV = "CLAUDE_CODE_SESSION_ID"
PROJECT_ENV = "CLAUDE_PROJECT_DIR"
EXIT_FAILURE = 1
EXIT_USAGE = 2


class CliError(Exception):
    """A failure with its section-2 object fields and exit code."""

    def __init__(self, error: str, kind: str, message: str, exit_code: int = EXIT_FAILURE) -> None:
        super().__init__(message)
        self.error = error
        self.kind = kind
        self.message = message
        self.exit_code = exit_code


@dataclass(frozen=True)
class _Ctx:
    stdin: TextIO
    stdout: TextIO
    stderr: TextIO
    env: Mapping[str, str]
    now: datetime


# ----------------------------------------------------------------------- parser

class _Parser(argparse.ArgumentParser):
    """argparse whose usage errors become the section-2 usage error object."""

    def error(self, message: str):  # type: ignore[override]
        raise CliError("usage", "bad-argv", message, EXIT_USAGE)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog=PROG,
        allow_abbrev=False,
        description="presence-kit CLI. The Stop hook verb `stop` reads a Stop payload on stdin and takes no arguments.",
    )
    verbs = parser.add_subparsers(dest="verb", required=True, metavar="VERB")

    config = verbs.add_parser("config", allow_abbrev=False, help="print the resolved configuration")
    config.add_argument("--project-dir", metavar="DIR")

    state = verbs.add_parser("state", allow_abbrev=False, help="read or write the session state")
    actions = state.add_subparsers(dest="action", required=True, metavar="ACTION")
    get = actions.add_parser("get", allow_abbrev=False, help="print the session state")
    get.add_argument("--session", metavar="ID")
    set_ = actions.add_parser("set", allow_abbrev=False, help="set afk, autonomous or normal")
    targets = set_.add_subparsers(dest="target", required=True, metavar="TARGET")
    for name in ("afk", "autonomous"):
        sub = targets.add_parser(name, allow_abbrev=False, help=f"enter {name}")
        sub.add_argument("--by", required=True, choices=ps.SET_BYS)
        if name == "autonomous":
            sub.add_argument("--goal", required=True, metavar="TEXT")
        sub.add_argument("--project-dir", metavar="DIR")
        sub.add_argument("--session", metavar="ID")
    normal = targets.add_parser("normal", allow_abbrev=False, help="revoke the granted goal")
    normal.add_argument("--session", metavar="ID")

    recap = verbs.add_parser("recap-done", allow_abbrev=False, help="record a finished recap")
    recap.add_argument("trigger", choices=ps.RECAP_TRIGGERS)
    recap.add_argument("prompt_id", metavar="PID")
    recap.add_argument("--session", metavar="ID")

    # Listed for --help only: `stop` is dispatched before argparse (no argv, section 2).
    verbs.add_parser("stop", add_help=False, help="Stop hook: payload on stdin, no arguments")
    return parser


# ---------------------------------------------------------------------- helpers

def _emit(ctx: _Ctx, obj: Mapping[str, Any]) -> None:
    ctx.stdout.write(json.dumps(obj) + "\n")


def _session_id(args: argparse.Namespace, env: Mapping[str, str]) -> str:
    sid = args.session if args.session is not None else (env.get(SESSION_ENV) or None)
    return ps.check_session_id(sid)


def _project_dir(explicit: str | None, env: Mapping[str, str]) -> Path:
    """`--project-dir`, else CLAUDE_PROJECT_DIR, else the process cwd (config and `--by model`)."""
    return Path(explicit or env.get(PROJECT_ENV) or Path.cwd())


def _check_usage(args: argparse.Namespace) -> None:
    """Usage rules argparse cannot express."""
    if getattr(args, "target", None) == "autonomous" and not args.goal.strip():
        raise CliError("usage", "bad-argv", "argument --goal: must not be blank", EXIT_USAGE)


# ------------------------------------------------------------------------- verbs

def cmd_config(args: argparse.Namespace, ctx: _Ctx) -> int:
    cfg = pc.resolve(_project_dir(args.project_dir, ctx.env), ctx.env)
    _emit(ctx, cfg.to_json())
    return 0


def cmd_state(args: argparse.Namespace, ctx: _Ctx) -> int:
    sid = _session_id(args, ctx.env)
    if args.action == "get":
        _emit(ctx, ps.state_get(sid, ctx.env))
        return 0
    behaviors = None
    if args.target != "normal" and args.by == "model":
        behaviors = pc.resolve(_project_dir(args.project_dir, ctx.env), ctx.env).behaviors
    goal = None
    if args.target == "autonomous":
        goal = args.goal.strip()
    by = "user" if args.target == "normal" else args.by
    _emit(ctx, ps.state_set(sid, args.target, by, goal, behaviors, ctx.env, ctx.now))
    return 0


def cmd_recap_done(args: argparse.Namespace, ctx: _Ctx) -> int:
    sid = _session_id(args, ctx.env)
    _emit(ctx, ps.recap_done(sid, args.trigger, args.prompt_id, ctx.env, ctx.now))
    return 0


def _read_payload(stdin: TextIO) -> dict:
    """The Stop payload: UTF-8, one JSON object, ids present (contracts.md section 5)."""
    raw = stdin.buffer.read() if hasattr(stdin, "buffer") else stdin.read()
    try:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        if not text.strip():
            raise CliError("payload", "empty", "hook payload is empty")
        payload = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CliError("payload", "malformed-json", f"hook payload is not valid JSON -- {exc}") from exc
    if not isinstance(payload, dict):
        raise CliError(
            "payload", "not-object",
            f"hook payload must be a JSON object, got {_json_type(payload)}",
        )
    for name in ("session_id", "prompt_id"):
        value = payload.get(name)
        if not isinstance(value, str) or not value:
            raise CliError(
                "payload", "missing-field",
                f"hook payload field '{name}' must be a non-empty string",
            )
    try:
        ps.check_session_id(payload["session_id"])
        ps.check_prompt_id(payload["prompt_id"])
    except ps.StateError as exc:
        raise CliError("payload", exc.kind, exc.text) from exc
    return payload


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def cmd_stop(argv: Sequence[str], ctx: _Ctx) -> int:
    """argv check, ATTENDED gate, payload, config, evaluate (contracts.md section 2)."""
    if argv:
        raise CliError("usage", "extra-argv", f"stop takes no arguments; got {' '.join(argv)}")
    if ctx.env.get(ATTENDED_ENV) != "1":
        return 0
    payload = _read_payload(ctx.stdin)
    cwd = payload.get("cwd")
    project = Path(
        ctx.env.get(PROJECT_ENV) or (cwd if isinstance(cwd, str) and cwd else None) or Path.cwd()
    )
    try:
        cfg = pc.resolve(project, ctx.env)
    except pc.ConfigError as err:
        decision = pstop.config_error_decision(err, payload)
    else:
        decision = pstop.evaluate(payload, cfg, ctx.env, ctx.now)
    output = decision.to_output()
    if output is not None:
        _emit(ctx, output)
    return 0


# ------------------------------------------------------------------------- main

_HANDLERS = {"config": cmd_config, "state": cmd_state, "recap-done": cmd_recap_done}


def _failure(err: CliError, ctx: _Ctx, *, hook: bool) -> int:
    """Report a failure: object plus text for CLI verbs, text only for the hook verb."""
    if not hook:
        _emit(ctx, {"error": err.error, "kind": err.kind, "message": err.message})
    ctx.stderr.write(err.message + "\n")
    return err.exit_code


def main(
    argv: Sequence[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    env: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> int:
    args_in = list(sys.argv[1:] if argv is None else argv)
    ctx = _Ctx(
        stdin=sys.stdin if stdin is None else stdin,
        stdout=sys.stdout if stdout is None else stdout,
        stderr=sys.stderr if stderr is None else stderr,
        env=os.environ if env is None else env,
        now=ps.now_utc() if now is None else now,
    )
    hook = bool(args_in) and args_in[0] == "stop"
    try:
        if hook:
            return cmd_stop(args_in[1:], ctx)
        try:
            with contextlib.redirect_stdout(ctx.stdout):
                args = build_parser().parse_args(args_in)
        except SystemExit as done:  # --help
            return int(done.code or 0)
        _check_usage(args)
        return _HANDLERS[args.verb](args, ctx)
    except CliError as err:
        return _failure(err, ctx, hook=hook)
    except pc.ConfigError as err:
        return _failure(CliError("config", err.kind, err.text), ctx, hook=hook)
    except ps.StateError as err:
        return _failure(CliError("state", err.kind, err.text), ctx, hook=hook)
    except Exception as exc:  # noqa: BLE001 -- the contract maps every unexpected exception to exit 1
        ctx.stderr.write(traceback.format_exc())
        return _failure(CliError("internal", "unexpected", f"{type(exc).__name__}: {exc}"), ctx, hook=hook)


if __name__ == "__main__":
    sys.exit(main())
