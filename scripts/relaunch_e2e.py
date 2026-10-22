#!/usr/bin/env python3
"""macOS end-to-end test for relaunch-kit (clauder launcher + hooks module).

Drives interactive `claude` sessions under a pty. Cases:

  (a) /relaunch-check, 3 hops, through bin/clauder from zsh and from /bin/bash 3.2
  (b) the model calls request_restart, both modes, in default and auto permission
      mode, with no permission prompt
  (c) two concurrent clauder runs in one directory, each keeping its own session
  (d) plain claude: the tool answers no-launcher and nothing exits
  (e) a Bash-tool child sees no RELAUNCH_KIT_TOKEN (with a positive control)
  (f) the relaunch argv forms (`-- "/task work <ref>"`, `--resume <id>`) run, and
      `--resume` works after /cd moved the session root
  (g) H1: installed from a scratch folder marketplace at project scope, the
      module loads with no "Turn on" question

Usage:  uv run python scripts/relaunch_e2e.py [--only a,b,...] [--log-dir DIR]

Prints PASS or FAIL per case and exits 1 when any case fails or could not run.
Needs a logged-in `claude`; uses the real HOME. The /relaunch-check sessions make
no model calls; the model prompts are trivial. Child processes are tracked by pid,
every child has a hard timeout, and only processes this script started are killed.

Case (g) installs relaunch-kit from a scratch marketplace at project scope. A
`finally` uninstalls it, removes the marketplace and its orphaned cache directory,
and checks that ~/.claude/settings.json and ~/.claude/plugins/known_marketplaces.json
carry no leftover entry.
"""

from __future__ import annotations

import argparse
import atexit
import contextlib
import fcntl
import json
import os
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import uuid
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Tuple

REPO = Path(__file__).resolve().parent.parent
PLUGIN = REPO / "plugins" / "relaunch-kit"
CLAUDER = PLUGIN / "bin" / "clauder"
HOME = Path.home()
RDIR = HOME / ".claude" / "plugins" / "data" / "plugins-kit" / "relaunch-kit" / "relaunch"
MKT_NAME = "relaunch-e2e-mkt"
RUN_PREFIXES = ("cb", "cz", "bd", "ba", "fcd", "gi")

# Variables the orchestrating session exports; a nested claude must not inherit them.
INHERITED = (
    "CLAUDECODE",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_BRIDGE_SESSION_ID",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_PID",
    "CLAUDE_CODE_ENTRYPOINT",
    "RELAUNCH_KIT_TOKEN",
    "RELAUNCH_KIT_DIR",
    "RELAUNCH_KIT_CLAUDE",
    "CLAUDE_PLUGIN_TEST",
    "CLAUDE_BOOTSTRAP_DATA_ROOT",
)

ANSI = re.compile(
    rb"\x1b\[[0-9;?<>=]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][A-Z0-9]|\x1b[=>78]"
)
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
PERMISSION = (r"Doyouwantto(?:proceed|allow|use)", r"Allowthis(?:tool|command)", r"Permissionrequired")

LIVE: Dict[int, "Term"] = {}


class CaseFailure(Exception):
    """A check failed; the message is the evidence."""


class EpisodeAbort(Exception):
    """An episode stops after a recorded failure."""


def say(message: str) -> None:
    print(message, flush=True)


def child_env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in INHERITED}
    env["TERM"] = "xterm-256color"
    if extra:
        env.update(extra)
    return env


# --- pty driver -------------------------------------------------------------


class Term:
    """One command under a pty, with a hard timeout and a tracked pid."""

    def __init__(self, label: str, argv: List[str], *, cwd: Path, log_dir: Path,
                 env: Optional[Dict[str, str]] = None, hard_timeout: float = 240.0) -> None:
        self.label = label
        self.buf = b""
        self.started = time.time()
        self.hard_timeout = hard_timeout
        self.last_output = self.started
        self.ended: Optional[float] = None
        self.status: Optional[int] = None
        self.trust_answers = 0
        self._trust_at = -100.0
        self._trust_floor = 0
        self.log = open(log_dir / ("%s.pty" % label), "wb")
        environment = env if env is not None else child_env()
        pid, fd = pty.fork()
        if pid == 0:
            try:
                os.chdir(str(cwd))
                os.execvpe(argv[0], argv, environment)
            finally:
                os._exit(127)
        self.pid, self.fd = pid, fd
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 160, 0, 0))
        LIVE[pid] = self

    def text(self) -> str:
        return re.sub(r"\s+", "", ANSI.sub(b"", self.buf).decode("utf8", "replace"))

    def tail(self, n: int = 400) -> str:
        return self.text()[-n:]

    def mark(self) -> int:
        return len(self.text())

    def alive(self) -> bool:
        if self.status is not None:
            return False
        done, status = os.waitpid(self.pid, os.WNOHANG)
        if done:
            self.status = status
            self.ended = time.time()
            LIVE.pop(self.pid, None)
            return False
        return True

    def exit_code(self) -> Optional[int]:
        return None if self.status is None else os.waitstatus_to_exitcode(self.status)

    def pump(self, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end and self.status is None:
            if time.time() - self.started > self.hard_timeout:
                self.kill()
                raise CaseFailure("%s: hard timeout after %ds; tail: %s" % (self.label, self.hard_timeout, self.tail()))
            ready, _, _ = select.select([self.fd], [], [], 0.2)
            if not ready:
                self.alive()
                continue
            try:
                data = os.read(self.fd, 65536)
            except OSError:
                data = b""
            if not data:
                for _ in range(100):
                    if not self.alive():
                        return
                    time.sleep(0.1)
                self.kill()
                return
            self.buf += data
            self.log.write(data)
            self.log.flush()
            self.last_output = time.time()
            self._answer_trust()

    def _answer_trust(self) -> None:
        # The dialog is redrawn after the answer: redraws within 10 s are absorbed, not answered.
        count = self.text().count("Quicksafetycheck")
        if time.time() - self._trust_at <= 10:
            self._trust_floor = count
        elif count > self._trust_floor:
            self._trust_floor = count
            self._trust_at = time.time()
            self.trust_answers += 1
            time.sleep(0.5)
            os.write(self.fd, b"\x1b[B")  # the default is "No, exit"
            time.sleep(0.5)
            os.write(self.fd, b"\r")

    def wait_for(self, pattern: str, timeout: float, *, since: int = 0,
                 fail_on: Tuple[str, ...] = ()) -> "re.Match[str]":
        """Wait for a regex in the whitespace-stripped screen text after offset `since`."""
        rx = re.compile(pattern)
        end = time.time() + timeout
        while True:
            text = self.text()
            found = rx.search(text, since)
            if found:
                return found
            for bad in fail_on:
                hit = re.search(bad, text[since:])
                if hit:
                    raise CaseFailure("%s: saw %r while waiting for %r; tail: %s"
                                      % (self.label, hit.group(0), pattern, self.tail()))
            if self.status is not None:
                raise CaseFailure("%s: exited (%s) while waiting for %r; tail: %s"
                                  % (self.label, self.exit_code(), pattern, self.tail()))
            if time.time() > end:
                raise CaseFailure("%s: timeout %ds waiting for %r; tail: %s"
                                  % (self.label, timeout, pattern, self.tail()))
            self.pump(0.3)

    def wait_ready(self, minimum: float = 3.0, quiet: float = 2.0, timeout: float = 90.0,
                   since: Optional[float] = None) -> None:
        """The input box is up: output exists, `minimum` s since `since`, `quiet` s without output."""
        since = self.started if since is None else since
        end = time.time() + timeout
        while True:
            now = time.time()
            if self.buf and now - since >= minimum and now - self.last_output >= quiet:
                return
            if self.status is not None:
                raise CaseFailure("%s: exited before ready: %s" % (self.label, self.tail()))
            if now > end:
                raise CaseFailure("%s: never became ready; tail: %s" % (self.label, self.tail()))
            self.pump(0.3)

    def type(self, text: str) -> None:
        os.write(self.fd, text.encode())
        self.pump(0.6)
        os.write(self.fd, b"\r")

    def send(self, data: bytes) -> None:
        os.write(self.fd, data)

    def wait_exit(self, timeout: float) -> int:
        end = time.time() + timeout
        while self.alive():
            if time.time() > end:
                raise CaseFailure("%s: still running after %ds; tail: %s" % (self.label, timeout, self.tail()))
            self.pump(0.3)
        self.pump(0.3)
        code = self.exit_code()
        assert code is not None
        return code

    def kill(self) -> None:
        """Kill the process group this Term started (the pty fork is its session leader)."""
        if self.status is None and self.alive():
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.pid, signal.SIGKILL)
            try:
                _, self.status = os.waitpid(self.pid, 0)
            except ChildProcessError:
                self.status = 0
            self.ended = time.time()
            LIVE.pop(self.pid, None)

    def close(self) -> None:
        self.kill()
        with contextlib.suppress(OSError):
            os.close(self.fd)
        self.log.close()


def kill_live() -> None:
    for term in list(LIVE.values()):
        term.kill()


atexit.register(kill_live)


# --- recording --------------------------------------------------------------


class Ctx:
    def __init__(self, log_dir: Path, scratch: Path) -> None:
        self.log_dir = log_dir
        self.scratch = scratch
        self.rec: Dict[str, List[Tuple[bool, str]]] = {}
        self.done: set = set()

    def ok(self, case: str, line: str) -> None:
        self.rec.setdefault(case, []).append((True, line))

    def bad(self, case: str, line: str) -> None:
        self.rec.setdefault(case, []).append((False, line))

    @contextlib.contextmanager
    def step(self, *cases: str) -> Iterator[None]:
        """A failure inside is recorded against every named case, then the episode stops."""
        try:
            yield
        except CaseFailure as exc:
            for case in cases:
                self.bad(case, str(exc))
            raise EpisodeAbort()

    def run_episode(self, name: str, fn: Callable[["Ctx"], None]) -> None:
        if name in self.done:
            return
        self.done.add(name)
        say("... episode %s" % name)
        try:
            fn(self)
        except EpisodeAbort:
            pass
        except Exception as exc:  # a harness defect must still produce a verdict
            self.bad("?", "harness error in episode %s: %s: %s" % (name, type(exc).__name__, exc))


def token_files() -> List[str]:
    if not RDIR.is_dir():
        return []
    return sorted(p.name for p in RDIR.iterdir() if p.suffix == ".json")


def read_check_log(run: str) -> List[dict]:
    path = RDIR / ("check-%s.log" % run)
    if not path.is_file():
        raise CaseFailure("check log %s was not written" % path)
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def new_run(prefix: str) -> str:
    return "%s%s" % (prefix, uuid.uuid4().hex[:8])


def verify_chain(run: str, term: Term, rc: Optional[int]) -> str:
    """Check a finished 3-hop /relaunch-check chain; raise CaseFailure on any defect."""
    rows = read_check_log(run)
    hops = [r for r in rows if "hop" in r]
    verdicts = [r for r in rows if "verdict" in r]
    if [h["hop"] for h in hops] != [2, 1, 0]:
        raise CaseFailure("run %s: hops logged %s, wanted [2, 1, 0]; rows %s; tail: %s"
                          % (run, [h["hop"] for h in hops], rows, term.tail()))
    if not verdicts or verdicts[-1]["verdict"] != "PASS":
        raise CaseFailure("run %s: verdict %s" % (run, verdicts))
    h2, h1, h0 = hops
    if h1["session_id"] != h2["session_id"]:
        raise CaseFailure("run %s: hop 1 session %s != hop 2 session %s" % (run, h1["session_id"], h2["session_id"]))
    if h0["session_id"] == h2["session_id"]:
        raise CaseFailure("run %s: hop 0 reused session %s" % (run, h0["session_id"]))
    tokens = {h["token"] for h in hops}
    if len(tokens) != 3:
        raise CaseFailure("run %s: tokens not distinct: %s" % (run, sorted(tokens)))
    left = [t for t in token_files() if t.split(".")[0] in tokens]
    if left:
        raise CaseFailure("run %s: token files left behind: %s" % (run, left))
    if rc != 0:
        raise CaseFailure("run %s: the shell saw exit code %r, wanted 0" % (run, rc))
    text = term.text()
    if "Turnon" in text:
        raise CaseFailure("run %s: a 'Turn on' question appeared" % run)
    for needle in ("[clauder]relaunching(check):resume%s,then:/relaunch-check1%s" % (h2["session_id"], run),
                   "[clauder]relaunching(check):/relaunch-check0%s" % run):
        if needle not in text:
            raise CaseFailure("run %s: launcher line missing: %s; tail: %s" % (run, needle, term.tail()))
    return ("run %s: sessions %s -> %s -> %s, distinct tokens, no token file left, shell rc 0"
            % (run, h2["session_id"][:8], h1["session_id"][:8], h0["session_id"][:8]))


def shell_chain(label: str, shell: List[str], run: str, log_dir: Path, cwd: Path = REPO,
                plugin_dir: bool = True) -> Term:
    flag = "--plugin-dir %s " % PLUGIN if plugin_dir else ""
    command = '%s %s"/relaunch-check 2 %s"; echo SHELL_RC=$?' % (CLAUDER, flag, run)
    return Term(label, shell + ["-c", command], cwd=cwd, log_dir=log_dir, hard_timeout=170.0)


def finish_chain(term: Term, run: str) -> str:
    term.wait_for(r"SHELL_RC=\d+", 160)
    rc = int(re.search(r"SHELL_RC=(\d+)", term.text()).group(1))  # type: ignore[union-attr]
    term.wait_exit(10)
    return verify_chain(run, term, rc)


def bash_version() -> str:
    return subprocess.run(["/bin/bash", "-c", "echo $BASH_VERSION"], capture_output=True, text=True,
                          check=True).stdout.strip()


# --- episodes ---------------------------------------------------------------


def episode_concurrent(ctx: Ctx) -> None:
    """Cases (a) and (c): a zsh chain and a /bin/bash 3.2 chain, started together in one cwd."""
    version = bash_version()
    run_b, run_z = new_run("cb"), new_run("cz")
    bash = shell_chain("c-bash", ["/bin/bash", "--noprofile", "--norc"], run_b, ctx.log_dir)
    zsh = shell_chain("c-zsh", ["/bin/zsh", "-f"], run_z, ctx.log_dir)
    try:
        results: Dict[str, str] = {}
        for name, term, run in (("bash", bash, run_b), ("zsh", zsh, run_z)):
            try:
                results[name] = finish_chain(term, run)
            except CaseFailure as exc:
                results[name] = "FAILED: %s" % exc
        for name, label in (("zsh", "zsh"), ("bash", "bash %s" % version)):
            if results[name].startswith("FAILED"):
                ctx.bad("a", "%s: %s" % (label, results[name]))
            else:
                ctx.ok("a", "%s: %s" % (label, results[name]))
        if not version.startswith("3.2"):
            ctx.bad("a", "/bin/bash is %s, not the 3.2 the spec names" % version)
        for name in ("bash", "zsh"):
            if results[name].startswith("FAILED"):
                ctx.bad("c", "concurrent %s chain: %s" % (name, results[name]))
        if all(not r.startswith("FAILED") for r in results.values()):
            with ctx.step("c"):
                ids_b = {h["session_id"] for h in read_check_log(run_b) if "hop" in h}
                ids_z = {h["session_id"] for h in read_check_log(run_z) if "hop" in h}
                if ids_b & ids_z:
                    raise CaseFailure("the two chains shared a session id: %s" % sorted(ids_b & ids_z))
                overlap = min(bash.ended or 0, zsh.ended or 0) - max(bash.started, zsh.started)
                if overlap < 3.0:
                    raise CaseFailure("the two chains overlapped for only %.1f s" % overlap)
                ctx.ok("c", "two chains in one cwd ran for %.0f s at once, each kept its own session "
                       "(%d + %d ids, none shared)" % (overlap, len(ids_b), len(ids_z)))
    finally:
        bash.close()
        zsh.close()


def bash_count_probe(term: Term) -> int:
    """Ask the model to run a Bash command that counts RELAUNCH_KIT_ variables; answer Bash permission prompts."""
    start = term.mark()
    term.type("Use the Bash tool to run exactly this command and nothing else: "
              "echo E2E_ENV_COUNT=$(env | grep -c RELAUNCH_KIT_)")
    answered = 0
    deadline = time.time() + 100
    while True:
        term.pump(0.5)
        text = term.text()[start:]
        found = re.search(r"E2E_ENV_COUNT=(\d)", text)
        if found:
            return int(found.group(1))
        if term.status is not None:
            raise CaseFailure("%s: exited during the Bash probe: %s" % (term.label, term.tail()))
        if text.count("Doyouwantto") > answered and answered < 3:
            answered += 1
            term.send(b"\r")
        if time.time() > deadline:
            raise CaseFailure("%s: no Bash result; tail: %s" % (term.label, term.tail()))


def tool_prompt(mode: str, prompt: Optional[str], tag: str) -> str:
    args = 'mode "%s"' % mode
    if prompt is not None:
        args += ', prompt "%s"' % prompt
    return ('Call the tool mcp__relaunch-kit__request_restart with %s and reason "e2e %s". '
            "Make no other tool call and write nothing else." % (args, tag))


def leave(term: Term) -> int:
    """Get out of a session that is busy with a model turn (it may sit at a permission prompt).

    Escape cancels the turn or the prompt; two Ctrl+C presses close together exit.
    """
    for _ in range(3):
        term.send(b"\x1b")
        term.pump(1.5)
        term.send(b"\x03")
        term.pump(0.3)
        term.send(b"\x03")
        term.pump(2.0)
        if not term.alive():
            break
    return term.wait_exit(40)


def episode_model_default(ctx: Ctx) -> None:
    run = new_run("bd")
    ref = "e2e-no-such-ref-%s" % run
    # The user's settings may default to auto mode, so default mode is requested explicitly.
    term = Term("b-default", [str(CLAUDER), "--plugin-dir", str(PLUGIN), "--permission-mode", "default"],
                cwd=REPO, log_dir=ctx.log_dir, hard_timeout=420.0)
    try:
        with ctx.step("b", "e"):
            term.wait_ready()
        with ctx.step("e"):
            count = bash_count_probe(term)
            if count != 0:
                raise CaseFailure("the Bash tool saw %d RELAUNCH_KIT_ variables, wanted 0" % count)
            ctx.ok("e", "a Bash-tool child saw E2E_ENV_COUNT=0 under clauder with the mod loaded")
        with ctx.step("b"):
            term.pump(1.0)
            m1 = term.mark()
            term.type(tool_prompt("resume", None, "resume"))
            got = term.wait_for(r"\[clauder\]relaunching\(restart\):resume(%s)" % UUID, 100, since=m1,
                                fail_on=PERMISSION)
            ctx.ok("b", "default mode, mode resume: no permission prompt; launcher resumed %s" % got.group(1)[:8])
            m2 = term.mark()
            term.wait_ready(since=time.time(), minimum=4.0)
            if re.search(r"\[clauder\]relaunching", term.text()[m2:]):
                raise CaseFailure("the resumed session restarted again by itself")
            ctx.ok("b", "the resumed session waited (no continuation prompt, no second restart)")
        m3 = term.mark()
        with ctx.step("b", "f"):
            term.type(tool_prompt("hand-off", "/task work %s" % ref, "hand-off"))
            term.wait_for(r"\[clauder\]relaunching\(hand-off\):/taskwork%s" % re.escape(ref), 100, since=m3,
                          fail_on=PERMISSION)
            ctx.ok("b", "default mode, mode hand-off: no permission prompt; launcher relaunched with the prompt")
        m4 = term.mark()
        with ctx.step("f"):
            deadline = time.time() + 40
            while "taskwork%s" % ref not in term.text()[m4:]:
                if time.time() > deadline or term.status is not None:
                    raise CaseFailure("the hand-off session never showed the prompt '/task work %s'; tail: %s"
                                      % (ref, term.tail(600)))
                term.pump(0.5)
            ctx.ok("f", "relaunch argv `-- \"/task work <ref>\"` reached the new session as its first prompt")
            code = leave(term)
            ids = re.findall(r"Resumethissessionwith:claude--resume(%s)" % UUID, term.text())
            if len(ids) < 3 or ids[0] != ids[1] or ids[2] == ids[0]:
                raise CaseFailure("session ids across the two restarts: %s (wanted a, a, b)" % ids)
            ctx.ok("f", "`--resume <id>` run: session %s kept across the resume, the hand-off got a new one "
                   "(final exit code %d)" % (ids[0][:8], code))
    finally:
        term.close()


def episode_model_auto(ctx: Ctx) -> None:
    run = new_run("ba")
    term = Term("b-auto", [str(CLAUDER), "--plugin-dir", str(PLUGIN), "--permission-mode", "auto"], cwd=REPO,
                log_dir=ctx.log_dir, hard_timeout=240.0)
    try:
        with ctx.step("b"):
            term.wait_ready()
            m1 = term.mark()
            term.type(tool_prompt("hand-off", "/relaunch-check 0 %s" % run, "auto"))
            term.wait_for(r"\[clauder\]relaunching\(hand-off\):/relaunch-check0%s" % run, 100, since=m1,
                          fail_on=PERMISSION)
            ctx.ok("b", "auto mode, mode hand-off: no permission prompt; launcher relaunched")
            term.wait_exit(80)
            if not any(r.get("hop") == 0 for r in read_check_log(run)):
                raise CaseFailure("the hand-off session did not run /relaunch-check 0")
            ctx.ok("b", "the auto-mode hand-off session ran its prompt and exited")
    finally:
        term.close()


def episode_control(ctx: Ctx) -> None:
    """Positive control for (e): with a token in the environment and no mod, the Bash tool sees it."""
    term = Term("e-control", ["claude"], cwd=REPO, log_dir=ctx.log_dir,
                env=child_env({"RELAUNCH_KIT_TOKEN": "e2econtroltoken0123", "RELAUNCH_KIT_DIR": "/nonexistent"}),
                hard_timeout=170.0)
    try:
        with ctx.step("e"):
            term.wait_ready()
            count = bash_count_probe(term)
            if count < 2:
                raise CaseFailure("control: the probe cannot see a token that is present (saw %d)" % count)
            ctx.ok("e", "control: with no mod and a token exported the probe sees %d variables, so 0 is meaningful"
                   % count)
    finally:
        term.close()


def episode_plain(ctx: Ctx) -> None:
    before = token_files()
    term = Term("d-plain", ["claude", "--plugin-dir", str(PLUGIN)], cwd=REPO, log_dir=ctx.log_dir,
                hard_timeout=250.0)
    try:
        with ctx.step("d"):
            term.wait_ready()
            m0 = term.mark()
            term.type("/relaunch-check")
            term.wait_for(r"relaunch-check:no-launcher", 30, since=m0)
            ctx.ok("d", "/relaunch-check printed no-launcher")
            m1 = term.mark()
            term.type('Call the tool mcp__relaunch-kit__request_restart with mode "resume" and reason "e2e plain". '
                      "Then write the text STATUS= immediately followed by the value of the status field in the "
                      "tool result, and nothing else.")
            term.wait_for(r"STATUS=no-launcher", 100, since=m1, fail_on=PERMISSION)
            ctx.ok("d", "the tool answered status no-launcher")
            term.pump(8.0)
            if not term.alive():
                raise CaseFailure("plain claude exited after the no-launcher answer; tail: %s" % term.tail())
            if "Restartingin3s" in term.text()[m1:]:
                raise CaseFailure("a countdown started under plain claude")
            ctx.ok("d", "still running 8 s after the tool answered (countdown 3 s + exit delay 0.3 s)")
            term.type("/exit")
            code = term.wait_exit(40)
            if code != 0:
                raise CaseFailure("plain claude exited with %d" % code)
            if token_files() != before:
                raise CaseFailure("token files changed: %s -> %s" % (before, token_files()))
            ctx.ok("d", "no token file written; clean exit")
    finally:
        term.close()


def episode_cd(ctx: Ctx) -> None:
    """/cd moves the session root; the restart must still resume the session from the moved root."""
    target = REPO / "docs"  # inside the trusted repo, so /cd asks no trust question
    run = new_run("fcd")
    term = Term("f-cd", [str(CLAUDER), "--plugin-dir", str(PLUGIN)], cwd=REPO, log_dir=ctx.log_dir,
                hard_timeout=200.0)
    try:
        with ctx.step("f"):
            term.wait_ready()
            m0 = term.mark()
            term.type("/cd %s" % target)
            term.wait_for(r"Movedto%s" % re.escape(str(target)), 30, since=m0)
            m1 = term.mark()
            term.type("/relaunch-check 2 %s" % run)
            term.wait_for(r"\[clauder\]relaunching\(check\):resume(%s)" % UUID, 60, since=m1)
            term.wait_exit(90)
            hops = [r["hop"] for r in read_check_log(run) if "hop" in r]
            if hops != [2, 1, 0]:
                raise CaseFailure("after /cd the chain logged hops %s, wanted [2, 1, 0]; tail %s"
                                  % (hops, term.tail(500)))
            if "plugins-kit2/docs" not in term.text()[m1:]:
                raise CaseFailure("the relaunched session did not start in the moved root %s" % target)
            ctx.ok("f", "after /cd to %s, `--resume <id>` from the moved root resumed the session (hops %s)"
                   % (target.relative_to(REPO), hops))
    finally:
        term.close()


def run_cli(args: List[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["claude"] + args, cwd=str(cwd), env=child_env(), capture_output=True, text=True,
                          timeout=120)


def episode_installed(ctx: Ctx) -> None:
    mkt = ctx.scratch / "mkt"
    # A fixed path: accepting the trust dialog records the path in ~/.claude.json, so a random
    # path would add one entry per run and a fixed one adds at most one.
    proj = Path(tempfile.gettempdir()).resolve() / "relaunch-e2e-project"
    cache = HOME / ".claude" / "plugins" / "cache" / MKT_NAME
    data = HOME / ".claude" / "plugins" / "data" / MKT_NAME
    watched = (HOME / ".claude" / "settings.json", HOME / ".claude" / "plugins" / "known_marketplaces.json",
               HOME / ".claude" / "plugins" / "installed_plugins.json")
    installed = added = False
    try:
        try:
            with ctx.step("g"):
                (mkt / ".claude-plugin").mkdir(parents=True)
                shutil.rmtree(proj, ignore_errors=True)
                proj.mkdir()
                # bootstrap.json is left out of the copy: with it, the user's bootstrap would sync the copy
                # into a data directory and add a PATH line to ~/.zshrc and ~/.bashrc. The module under test
                # does not read it.
                shutil.copytree(PLUGIN, mkt / "relaunch-kit",
                                ignore=shutil.ignore_patterns("__pycache__", "bootstrap.json"))
                # relaunch-kit declares dependencies ["bootstrap"]; a stub of that name keeps the manifest
                # byte-identical and lets a one-marketplace install resolve it.
                (mkt / "bootstrap" / ".claude-plugin").mkdir(parents=True)
                (mkt / "bootstrap" / ".claude-plugin" / "plugin.json").write_text(
                    json.dumps({"name": "bootstrap", "version": "0.0.1", "description": "e2e stub"}))
                (mkt / ".claude-plugin" / "marketplace.json").write_text(json.dumps({
                    "name": MKT_NAME, "owner": {"name": "e2e"},
                    "plugins": [
                        {"name": "bootstrap", "source": "./bootstrap", "description": "e2e stub", "version": "0.0.1"},
                        {"name": "relaunch-kit", "source": "./relaunch-kit", "description": "e2e copy",
                         "version": "0.1.0"},
                    ]}))
                added = True
                add = run_cli(["plugin", "marketplace", "add", str(mkt), "--scope", "project"], proj)
                if add.returncode != 0:
                    raise CaseFailure("marketplace add failed: %s %s" % (add.stdout[-300:], add.stderr[-300:]))
                installed = True
                inst = run_cli(["plugin", "install", "relaunch-kit@%s" % MKT_NAME, "--scope", "project"], proj)
                if inst.returncode != 0 or "was not installed" in inst.stdout:
                    raise CaseFailure("install failed or skipped a dependency: %s %s"
                                      % (inst.stdout[-400:], inst.stderr[-300:]))
                ctx.ok("g", "install: " + inst.stdout.strip().splitlines()[-1][:160])
            run = new_run("gi")
            command = '%s "/relaunch-check 2 %s"; echo SHELL_RC=$?' % (CLAUDER, run)
            term = Term("g-installed", ["/bin/zsh", "-f", "-c", command], cwd=proj, log_dir=ctx.log_dir,
                        hard_timeout=200.0)
            try:
                with ctx.step("g"):
                    term.wait_for(r"SHELL_RC=\d+", 190, fail_on=(r"Turnon", r"Enablethe(?:module|mod|plugin)",
                                                                  r"Trustthis(?:module|plugin|mod)"))
                    rc = int(re.search(r"SHELL_RC=(\d+)", term.text()).group(1))  # type: ignore[union-attr]
                    term.wait_exit(10)
                    ctx.ok("g", verify_chain(run, term, rc))
                    ctx.ok("g", "H1: the project-scope installed module (no --plugin-dir) loaded and ran 3 hops "
                           "with no 'Turn on' question (trust prompts answered: %d)" % term.trust_answers)
            finally:
                term.close()
        finally:
            problems: List[str] = []
            if installed:
                r = run_cli(["plugin", "uninstall", "relaunch-kit@%s" % MKT_NAME, "--scope", "project"], proj)
                if r.returncode != 0:
                    problems.append("uninstall: %s" % (r.stdout + r.stderr)[-200:])
            if added:
                r = run_cli(["plugin", "marketplace", "remove", MKT_NAME], proj)
                if r.returncode != 0:
                    problems.append("marketplace remove: %s" % (r.stdout + r.stderr)[-200:])
            for leftover_dir in (cache, data):
                if leftover_dir.exists():
                    shutil.rmtree(leftover_dir, ignore_errors=True)
            for path in watched:
                if path.is_file() and MKT_NAME in path.read_text():
                    problems.append("%s still mentions %s" % (path, MKT_NAME))
            for leftover_dir in (cache, data):
                if leftover_dir.exists():
                    problems.append("%s still exists" % leftover_dir)
            shutil.rmtree(proj, ignore_errors=True)
            if installed or added:
                if problems:
                    ctx.bad("g", "cleanup left: %s" % "; ".join(problems))
                else:
                    ctx.ok("g", "cleanup: uninstalled, marketplace removed, orphaned cache removed, "
                           "settings.json and known_marketplaces.json have no leftover entry")
    except EpisodeAbort:
        pass


# --- runner -----------------------------------------------------------------

EPISODES: List[Tuple[str, Callable[[Ctx], None], str]] = [
    ("concurrent", episode_concurrent, "ac"),
    ("model-default", episode_model_default, "bef"),
    ("model-auto", episode_model_auto, "b"),
    ("control", episode_control, "e"),
    ("plain", episode_plain, "d"),
    ("cd", episode_cd, "f"),
    ("installed", episode_installed, "g"),
]
TITLES = {
    "a": "/relaunch-check 3 hops through bin/clauder, zsh and /bin/bash 3.2",
    "b": "the model calls request_restart: both modes, default and auto, no permission prompt",
    "c": "two concurrent clauder runs in one cwd keep their own sessions",
    "d": "plain claude: the tool answers no-launcher, nothing exits",
    "e": "a Bash-tool child sees no RELAUNCH_KIT_TOKEN",
    "f": "relaunch argv forms (/task work <ref>, --resume <id>) run; resume after /cd",
    "g": "H1: installed project-scope module loads with no 'Turn on' question",
}


def preflight() -> None:
    if sys.platform != "darwin":
        raise SystemExit("this is the macOS end-to-end test")
    for path in (CLAUDER, PLUGIN / "lib" / "relaunch.py", PLUGIN / "hooks" / "register.ts"):
        if not path.is_file():
            raise SystemExit("missing %s" % path)
    if shutil.which("claude") is None:
        raise SystemExit("claude is not on PATH")


def main() -> int:
    parser = argparse.ArgumentParser(description="macOS end-to-end test for relaunch-kit")
    parser.add_argument("--only", default="", help="comma-separated case letters, e.g. a,c")
    parser.add_argument("--log-dir", default="", help="keep pty transcripts here (default: temp dir, removed)")
    args = parser.parse_args()
    preflight()
    wanted = {c for c in args.only.split(",") if c}
    scratch = Path(tempfile.mkdtemp(prefix="relaunch-e2e-"))
    log_dir = Path(args.log_dir) if args.log_dir else scratch / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    rdir_preexisting = RDIR.exists()
    parent_preexisting = RDIR.parent.exists()
    before = set(token_files())
    ctx = Ctx(log_dir, scratch)
    try:
        for name, fn, covers in EPISODES:
            if wanted and not (wanted & set(covers)):
                continue
            ctx.run_episode(name, fn)
    finally:
        kill_live()
        if RDIR.is_dir():
            for path in RDIR.glob("check-*.log"):
                if path.name[len("check-"):].startswith(RUN_PREFIXES):
                    path.unlink()
            if not rdir_preexisting and not any(RDIR.iterdir()):
                RDIR.rmdir()
                if not parent_preexisting and not any(RDIR.parent.iterdir()):
                    RDIR.parent.rmdir()
        leftover = sorted(set(token_files()) - before)
        if leftover:
            say("note: token files left in %s: %s" % (RDIR, leftover))
        if args.log_dir:
            say("transcripts kept in %s" % log_dir)
        shutil.rmtree(scratch, ignore_errors=True)
    say("")
    failed = 0
    for letter in "abcdefg":
        if wanted and letter not in wanted:
            continue
        lines = ctx.rec.get(letter, [])
        good = bool(lines) and all(ok for ok, _ in lines)
        say("%s (%s) %s" % ("PASS" if good else "FAIL", letter, TITLES[letter]))
        if not lines:
            say("    not run: its episode stopped before reaching it")
        for ok, line in lines:
            say("    %s %s" % ("ok  " if ok else "BAD ", line))
        failed += 0 if good else 1
    for ok, line in ctx.rec.get("?", []):
        say("FAIL harness: " + line)
        failed += 1
    say("%d failed" % failed if failed else "all passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
