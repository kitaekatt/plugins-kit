"""Fakes shared by background-session test suites."""

from __future__ import annotations

from typing import Any


class FakeRunner:
    """Script responses by longest matching argv prefix and log calls."""

    def __init__(self, scripts: Any = None, *, default: Any = None) -> None:
        self.scripts = dict(scripts or {})
        self.default = default
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def script(self, argv_prefix: Any, response: Any) -> None:
        self.scripts[tuple(argv_prefix)] = response

    def __call__(self, argv: Any, **kwargs: Any) -> Any:
        values = list(argv)
        self.calls.append((values, kwargs))
        matches = [
            (prefix, response)
            for prefix, response in self.scripts.items()
            if tuple(values[: len(prefix)]) == prefix
        ]
        if matches:
            response = max(matches, key=lambda item: len(item[0]))[1]
            if isinstance(response, list):
                return response.pop(0) if len(response) > 1 else response[0]
            return response
        if self.default is not None:
            return self.default
        raise AssertionError(f"FakeRunner: no script for argv {values!r}")


def _healthy_runner(*, agents_json_body: str = "[]", version: Any = ("2.1.233", "", 0)) -> FakeRunner:
    agents_help = (
        "Usage: claude agents [options] [command]\n\nManage background sessions "
        "(subcommands: stop, logs, rm, respawn)"
    )
    runner = FakeRunner()
    runner.script(("claude", "--version"), version)
    runner.script(("claude", "agents", "--json"), (agents_json_body, "", 0))
    runner.script(("claude", "agents", "--help"), (agents_help, "", 0))
    runner.script(("claude", "agents", "stop", "--help"), (agents_help, "", 0))
    for verb in ("stop", "logs", "rm", "respawn"):
        runner.script(("claude", verb, "--help"), (f"Usage: claude {verb}", "", 0))
    runner.script(("claude", "--bg", "-p"), ("", "error: option '-p' cannot be used with '--bg'", 1))
    return runner


def _bg_record(*, id: str = "a1b2c3d4", session_id: str = "sess-1", state: str = "working", **extra: Any) -> dict[str, Any]:
    record = {"kind": "background", "id": id, "sessionId": session_id, "state": state}
    record.update(extra)
    return record


def _advancing_clock_from(start: float = 1000.0, step: float = 1.0):
    ticks = {"t": start}

    def clock_fn() -> float:
        ticks["t"] += step
        return ticks["t"]

    return clock_fn
