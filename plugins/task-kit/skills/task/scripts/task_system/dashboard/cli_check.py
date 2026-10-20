"""Does THIS Claude Code binary honor the denials a run emits? Observed once per binary, off the tick lock.

A Claude worker's denials reach the CLI as ONE comma-joined ``--disallowedTools`` argument: the Bash floor
patterns, then tool-level names (``Agent``, ``Workflow``, ...). Whether a build splits that argument into
rules, resolves every tool name in it and applies a pattern with spaces inside parentheses is decided by
client code in the binary, so it is observed per binary instead of assumed.

What runs, for one binary, on the observer thread (never the scheduler's tick):

1. the binary's SHA-256 digest and ``claude --version``;
2. BASELINE: the worker argv llm-kit would emit for a job whose ``disallowed_tools`` is the FULL floor minus every
   tool-level denial, run until the CLI-authored ``system``/``init`` event lists its tools, then stopped;
3. ACTUAL: the same, with the byte-identical ``disallowed_tools`` value a run emits;
4. FLOOR PARSE: the actual argv in a scratch directory, asking the model to run ``git commit --dry-run``, read
   to the ``result`` event: the Bash tool_result must be a CLI-authored permission error and the result must
   list a ``permission_denials`` entry for it.

The argv is captured from llm-kit through ``JobsEdge.claude_worker_argv`` (the only llm-kit site). The only
deviations from the worker argv are the output format (``stream-json --verbose`` so events can be read),
``--model haiku``, ``--setting-sources local`` and an empty strict MCP config (no plugin hooks or MCP servers start).
Tool filtering and rule parsing are client-side and do not depend on the model.

States: ``observed``, ``ignored`` (a listed denial is still offered, or the command ran), ``vacuous`` (the baseline
has no sub-agent tool to compare) and ``unobserved`` (no init event, a timeout, an error, or no tool call).
Only ``ignored`` refuses runs; the others proceed with the denials, which are the documented control.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import floor, proctree, redact

OBSERVED, IGNORED, VACUOUS, UNOBSERVED = "observed", "ignored", "vacuous", "unobserved"

VERSION_TIMEOUT_S = 10.0
INIT_TIMEOUT_S = 15.0
RESULT_TIMEOUT_S = 60.0
STOP_GRACE_S = 5.0
SCRATCH_DIRNAME = "cli-check"

FLOOR_PARSE_PROMPT = "Use the Bash tool to run exactly: git commit --dry-run -m probe. Then stop."
INIT_PROMPT = "Reply with the single word ok."
_PERMISSION_DENIED = re.compile(r"permission|denied|not allowed|disallowed", re.IGNORECASE)


@dataclass(frozen=True)
class ClaudeKey:
    """What identifies a binary without spawning or hashing it: its real path and a stat."""

    realpath: str
    mtime_ns: int
    size: int


@dataclass(frozen=True)
class ClaudeBinary:
    key: ClaudeKey
    path: str
    digest: str
    version: str


@dataclass(frozen=True)
class DenyObservation:
    binary: ClaudeBinary | None
    state: str  # observed | ignored | vacuous | unobserved
    tools: dict = field(default_factory=dict)
    floor_parse: str = "not-run"  # denied | executed | none | unclear | not-run
    detail: str = ""
    at: str = ""

    def summary(self) -> dict:
        """A JSON-ready view for the capabilities payload and a run's spec.json."""
        binary = None
        if self.binary is not None:
            binary = {"path": self.binary.path, "digest": self.binary.digest, "version": self.binary.version}
        return {"state": self.state, "detail": self.detail, "at": self.at, "binary": binary,
                "tools": dict(self.tools), "floor_parse": self.floor_parse}


def claude_key() -> ClaudeKey | None:
    """The identity of the ``claude`` this process would launch, or None when there is none. A which, a realpath, a stat."""
    found = shutil.which("claude")
    if not found:
        return None
    try:
        real = os.path.realpath(found)
        stat = os.stat(real)
    except OSError:
        return None
    return ClaudeKey(real, stat.st_mtime_ns, stat.st_size)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class CliCheckError(RuntimeError):
    """The observation could not be set up (a captured argv of a shape this module does not know)."""


# -- argv -------------------------------------------------------------------


def probe_argv(argv: list[str]) -> list[str]:
    """The worker argv with only the deviations the module docstring lists."""
    out = list(argv)
    try:
        at = out.index("--output-format")
        out.index("--model")
    except ValueError as exc:
        raise CliCheckError(f"the captured worker argv lacks a flag the probe rewrites ({exc})") from exc
    out[at + 1] = "stream-json"
    out.insert(at + 2, "--verbose")
    out[out.index("--model") + 1] = "haiku"
    out += ["--setting-sources", "local", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']
    return out


def disallowed_value(argv: list[str]) -> str | None:
    """The value of ``--disallowedTools`` in ``argv`` (one argument), or None."""
    try:
        return argv[argv.index("--disallowedTools") + 1]
    except (ValueError, IndexError):
        return None


# -- the stream -------------------------------------------------------------


@dataclass
class StreamRun:
    events: list[dict] = field(default_factory=list)
    init_tools: list[str] | None = None
    result: dict | None = None
    problem: str | None = None  # timeout | exited | closed | spawn: ...
    stderr_tail: str = ""  # the CLI's last non-empty stderr line (redacted, at most 300 characters), when it said anything

    def reason(self) -> str:
        """``problem`` plus the CLI's own words, for a message a person reads: ``exited; stderr: Not logged in``."""
        base = self.problem or "none seen"
        return f"{base}; stderr: {self.stderr_tail}" if self.problem and self.stderr_tail else base


def tool_names(init_event: dict) -> list[str]:
    names = []
    for entry in init_event.get("tools") or []:
        if isinstance(entry, str):
            names.append(entry)
        elif isinstance(entry, dict) and isinstance(entry.get("name"), str):
            names.append(entry["name"])
    return names


def _aliases(tool: str) -> set[str]:
    return set(floor.SUBAGENT_TOOL_NAMES) if tool in floor.SUBAGENT_TOOL_NAMES else {tool}


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(_text(part.get("text") if isinstance(part, dict) else part) for part in content)
    return "" if content is None else str(content)


def floor_parse_verdict(events: list[dict], result: dict | None) -> tuple[str, str]:
    """``(denied | executed | none | unclear, detail)`` from CLI-authored events of the floor-parse step.

    ``denied`` needs BOTH a Bash tool_result error of the permission-denied form for the ``git commit`` call and a
    ``permission_denials`` entry for it in the result event. ``executed`` is any other tool_result for it: the
    probe runs in a scratch directory, so a command the CLI let through exits non-zero (not a git repository,
    nothing to commit) and arrives as an error that is not a permission denial. ``unclear`` is only a missing
    result, or a denial-looking result the CLI did not list.
    """
    calls: dict[str, str] = {}
    results: dict[str, tuple[bool, str]] = {}
    for event in events:
        message = event.get("message") if isinstance(event, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") == "Bash":
                command = str((block.get("input") or {}).get("command", ""))
                if "git commit" in command:
                    calls[str(block.get("id"))] = command
            elif block.get("type") == "tool_result":
                results[str(block.get("tool_use_id"))] = (bool(block.get("is_error")), _text(block.get("content")))
    if not calls:
        return "none", "the model made no Bash call for the probe command"
    denials = (result or {}).get("permission_denials") or []
    listed = {str(d.get("tool_use_id")) for d in denials if isinstance(d, dict)}
    for call_id, command in calls.items():
        found = results.get(call_id)
        if found is None:
            continue
        is_error, text = found
        if not (is_error and _PERMISSION_DENIED.search(text)):
            return "executed", f"the CLI ran {command!r} (its tool_result is not a permission denial)"
        if call_id in listed:
            return "denied", f"{command!r} was refused by the CLI and listed in permission_denials"
    return "unclear", "the probe command has no tool_result, or its permission-denied result is not listed in permission_denials"


# -- the observer -----------------------------------------------------------


class CliObserver:
    """Owned by the server: one daemon thread that observes one binary at a time, never under a scheduler lock.

    ``state_for`` and ``request`` never block. A key is observed at most once per observer (a server life):
    the tools a CLI offers and how it parses an argument are decided by client code from the binary and
    the argv, both fixed for a key.
    """

    def __init__(
        self,
        *,
        root: Path,
        edge,
        load_config: Callable[[], object],
        log: Callable[[str], None] = lambda message: None,
        init_timeout_s: float = INIT_TIMEOUT_S,
        result_timeout_s: float = RESULT_TIMEOUT_S,
        version_timeout_s: float = VERSION_TIMEOUT_S,
    ) -> None:
        self.root = Path(root)
        self.edge = edge
        self._load_config = load_config
        self._log = log
        self.init_timeout_s = init_timeout_s
        self.result_timeout_s = result_timeout_s
        self.version_timeout_s = version_timeout_s
        self._lock = threading.Lock()
        self._states: dict[ClaudeKey, DenyObservation] = {}
        self._inflight: ClaudeKey | None = None
        self._pending: ClaudeKey | None = None
        self._wake = threading.Event()
        self._closed = threading.Event()
        self._thread: threading.Thread | None = None

    # -- the non-blocking surface ---------------------------------------------

    def state_for(self, key: ClaudeKey) -> DenyObservation | None:
        with self._lock:
            return self._states.get(key)

    def in_flight(self, key: ClaudeKey) -> bool:
        with self._lock:
            return self._inflight == key or self._pending == key

    def request(self, key: ClaudeKey) -> None:
        """Ask for ``key`` to be observed (a no-op when it is known, running or already asked for)."""
        with self._lock:
            if key in self._states or key == self._inflight or key == self._pending or self._closed.is_set():
                return
            self._pending = key
            if self._thread is None:
                self._thread = threading.Thread(target=self._loop, name="cli-check", daemon=True)
                self._thread.start()
        self._wake.set()

    def close(self) -> None:
        self._closed.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=STOP_GRACE_S * 3)

    # -- the thread -----------------------------------------------------------

    def _loop(self) -> None:
        while not self._closed.is_set():
            self._wake.wait()
            self._wake.clear()
            while not self._closed.is_set():
                with self._lock:
                    key, self._pending = self._pending, None
                    self._inflight = key
                if key is None:
                    break
                try:
                    observation = self._observe(key)
                except Exception as exc:  # noqa: BLE001 -- a failure ends `unobserved`, never a hung start
                    observation = DenyObservation(None, UNOBSERVED, detail=f"the observation failed: {type(exc).__name__}: {exc}",
                                                  at=_now())
                with self._lock:
                    self._states[key] = observation
                    self._inflight = None
                self._log(f"claude cli check ({key.realpath}): {observation.state}: {observation.detail}")

    def _observe(self, key: ClaudeKey) -> DenyObservation:
        from .translate import worker_job_options  # translate imports subagents, which imports this module

        config = self._load_config()
        entry, effort = config.claude_entry, config.effort
        denials = floor.deny_tools()
        baseline_value = worker_job_options("claude-cli", effort, ())["disallowed_tools"]
        actual_value = worker_job_options("claude-cli", effort, denials)["disallowed_tools"]
        base = self.root / SCRATCH_DIRNAME
        base.mkdir(parents=True, exist_ok=True)
        scratch = Path(tempfile.mkdtemp(prefix="check-", dir=base))
        floor_dir = Path(tempfile.mkdtemp(prefix="floor-", dir=base))
        try:
            baseline_argv = self.edge.claude_worker_argv(entry, effort=effort, disallowed_tools=baseline_value, cwd=scratch)
            actual_argv = self.edge.claude_worker_argv(entry, effort=effort, disallowed_tools=actual_value, cwd=scratch)
            for argv, value in ((baseline_argv, baseline_value), (actual_argv, actual_value)):
                if disallowed_value(argv) != value:
                    raise CliCheckError("the captured worker argv does not carry the requested --disallowedTools value unchanged")
            if os.path.realpath(actual_argv[0]) != key.realpath:
                return DenyObservation(None, UNOBSERVED, detail=f"the worker would launch {actual_argv[0]}, not {key.realpath}", at=_now())
            binary = ClaudeBinary(key, key.realpath, _digest(key.realpath), self._version(actual_argv[0]))

            baseline = self._stream(probe_argv(baseline_argv), INIT_PROMPT, scratch, until="init", timeout_s=self.init_timeout_s)
            actual = self._stream(probe_argv(actual_argv), INIT_PROMPT, scratch, until="init", timeout_s=self.init_timeout_s)
            for label, run in (("baseline", baseline), ("actual", actual)):
                if run.init_tools is None:
                    return DenyObservation(binary, UNOBSERVED, detail=f"no init event in the {label} run ({run.reason()})",
                                           at=_now())
            base_tools, actual_tools = set(baseline.init_tools), set(actual.init_tools)  # type: ignore[arg-type]
            if not (base_tools & set(floor.SUBAGENT_TOOL_NAMES)):
                return DenyObservation(binary, VACUOUS, tools={"baseline": sorted(base_tools)},
                                       detail="the baseline tool list has no sub-agent tool, so there is nothing to compare", at=_now())
            expected = {name for tool in denials for name in _aliases(tool) if name in base_tools}
            absent = [tool for tool in denials if not (_aliases(tool) & base_tools)]
            tools = {"baseline": len(base_tools), "actual": len(actual_tools), "removed": sorted(base_tools - actual_tools),
                     "not_offered": absent}
            still = sorted(expected & actual_tools)
            if still:
                return DenyObservation(binary, IGNORED, tools=tools, floor_parse="not-run", at=_now(),
                                       detail=f"the CLI still offers {', '.join(still)} with the denials in --disallowedTools")
            if (base_tools - actual_tools) != expected or (actual_tools - base_tools):
                return DenyObservation(binary, UNOBSERVED, tools=tools, at=_now(),
                                       detail="the tool lists differ by more than the denied tools")
            parse = self._stream(probe_argv(actual_argv), FLOOR_PARSE_PROMPT, floor_dir, until="result", timeout_s=self.result_timeout_s)
            verdict, why = floor_parse_verdict(parse.events, parse.result)
            if verdict == "executed":
                return DenyObservation(binary, IGNORED, tools=tools, floor_parse=verdict, detail=why, at=_now())
            if verdict == "denied":
                return DenyObservation(binary, OBSERVED, tools=tools, floor_parse=verdict, at=_now(),
                                       detail="every tool-level denial took effect and the Bash floor refused the probe command")
            return DenyObservation(binary, UNOBSERVED, tools=tools, floor_parse=verdict, detail=why, at=_now())
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
            shutil.rmtree(floor_dir, ignore_errors=True)

    # -- processes ------------------------------------------------------------

    def _version(self, executable: str) -> str:
        return claude_version(executable, self.version_timeout_s)

    def _stream(self, argv: list[str], prompt: str, cwd: Path, *, until: str, timeout_s: float) -> StreamRun:
        return run_stream(argv, prompt, cwd, until=until, timeout_s=timeout_s, env=floor.run_environment(),
                          stderr_path=cwd / f"stderr-{uuid.uuid4().hex[:8]}.log", closed=self._closed.is_set)


def claude_version(executable: str, timeout_s: float = VERSION_TIMEOUT_S) -> str:
    """The first line ``<executable> --version`` prints (``unknown (...)`` when it cannot be run)."""
    try:
        done = subprocess.run([executable, "--version"], capture_output=True, text=True, timeout=timeout_s,
                              stdin=subprocess.DEVNULL, env=floor.run_environment())
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"unknown ({type(exc).__name__})"
    return (done.stdout or done.stderr).strip().splitlines()[0] if (done.stdout or done.stderr).strip() else "unknown"


def identify_binary(key: ClaudeKey, executable: str | None = None) -> ClaudeBinary:
    """The identity a characterization record is bound to: the binary's SHA-256 digest and its ``--version``."""
    return ClaudeBinary(key, key.realpath, _digest(key.realpath), claude_version(executable or key.realpath))


STDERR_TAIL_CHARS = 300


def _stderr_tail(path: Path) -> str:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return redact.redact(lines[-1])[:STDERR_TAIL_CHARS] if lines else ""


def run_stream(argv: list[str], prompt: str, cwd: Path, *, until: str, timeout_s: float, env: dict,
               stderr_path: Path, closed: Callable[[], bool] = lambda: False) -> StreamRun:
    """Run ``argv`` with ``prompt`` on stdin, collecting stream-json events.

    ``until`` is ``init`` (stop at the CLI's system/init event), ``result`` (stop at the first result event) or
    ``exit`` (read to the end of the stream: a run whose sub-agents report after the first result). The process
    tree is always stopped before this returns. ``problem`` says why a run ended early: ``timeout``, ``closed``,
    ``exited`` (the stream ended before the event asked for), or ``spawn: ...``.

    The CLI's stderr goes to ``stderr_path`` (a file the caller keeps as evidence, never DEVNULL: a CLI that
    refuses to start says why only there). When the run ended early, ``stderr_tail`` holds its last line.
    """
    run = StreamRun()
    try:
        with open(stderr_path, "wb") as stderr_file:
            proc = subprocess.Popen(argv, cwd=str(cwd), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=stderr_file, text=True, encoding="utf-8", errors="replace",
                                    start_new_session=(os.name == "posix"))
    except OSError as exc:
        run.problem = f"spawn: {exc}"
        return run
    lines: queue.Queue = queue.Queue()

    def read() -> None:
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                lines.put(line)
        except (OSError, ValueError):
            pass
        lines.put(None)

    threading.Thread(target=read, name="cli-check-reader", daemon=True).start()
    try:
        try:
            proc.stdin.write(prompt)  # type: ignore[union-attr]
            proc.stdin.close()  # type: ignore[union-attr]
        except (OSError, ValueError):
            pass
        deadline = time.monotonic() + timeout_s
        while True:
            if closed():
                run.problem = "closed"
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                run.problem = "timeout"
                break
            try:
                line = lines.get(timeout=min(remaining, 0.2))
            except queue.Empty:
                continue
            if line is None:
                if until != "exit":
                    run.problem = run.problem or ("exited" if (run.init_tools is None or (until == "result" and run.result is None)) else None)
                break
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            run.events.append(event)
            if event.get("type") == "system" and event.get("subtype") == "init":
                run.init_tools = tool_names(event)
                if until == "init":
                    break
            elif event.get("type") == "result":
                run.result = event
                if until == "result":
                    break
    finally:
        _stop(proc)
    if run.problem:
        run.stderr_tail = _stderr_tail(stderr_path)
    return run


def _stop(proc: subprocess.Popen) -> None:
    ident = proctree.ident_of(proc.pid)
    if ident is not None and proc.poll() is None:
        proctree.stop_tree(ident, grace_s=STOP_GRACE_S)
    try:
        proc.wait(timeout=STOP_GRACE_S)
    except subprocess.TimeoutExpired:
        pass
    try:
        if proc.stdout is not None:
            proc.stdout.close()
    except (OSError, ValueError):
        pass


def _digest(path: str) -> str:
    sha = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            sha.update(block)
    return sha.hexdigest()
