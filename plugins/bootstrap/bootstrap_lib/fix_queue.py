"""Deferred-operation queue: harvest, serialize, launch.

The engine half of the interactive-remediation system whose executing half is
:mod:`bootstrap_lib.fix_runner` (read its module docstring first -- it explains
WHY the work is deferred and why the runner, not a generated script, executes
it).

This module:

  * :func:`queue_from_failures` harvests the ``elevation`` descriptors the
    strategies attach to their failures into a list of typed
    :class:`FixTask` s. Descriptor shapes consumed (all carry ``os``, and only
    the current OS's are collected -- a pass runs on exactly one OS):
      - ``{"method": "apt", "package": <pkg>}``      -- deferred apt package;
      - ``{"method": "command", "command": <cmd>}``  -- deferred elevated command;
      - ``{"method": "brew_installer"}``             -- Homebrew missing on macOS;
      - ``{"method": "path_prune", "entries": [...]}`` -- dead Windows User PATH
        entries to delete. The one queued operation that needs no privilege at
        all (HKCU is the user's own hive): it is here for CONSENT, because it
        deletes things, and the queue is where "needs the user's attention"
        already lives.
    A ``command`` descriptor may also carry ``cost`` (see :func:`cost_of`),
    which is what lets the queue run the cheap work first, and ``command`` /
    ``path_prune`` descriptors may carry ``opportunistic`` -- piggyback-only
    housekeeping that rides the queue but never justifies surfacing it alone
    (see :func:`has_actionable`).
  * :func:`write_or_clear_queue` regenerates ``<data_dir>/elevate/queue.json``
    each pass and DELETES a stale queue when nothing is deferred, so the fix-all
    offer disappears once its operations succeed.
  * :func:`write_shim` emits a small ``bootstrap-fix.{bat,sh}`` that invokes the
    runner, preserving the "run it yourself" affordance (double-click on
    Windows; the only path on Unix -- see below).
  * :func:`launch_fix_runner` is the "fix-all is user consent" half: on an
    INTERACTIVE fix-all run the engine launches the runner itself and waits.

Naming: this module used to be ``elevation.py`` and rendered a shell script per
pass. Both the name and the codegen were narrower than the problem -- elevation
is one reason an operation needs a console, not the only one.

Stdlib-only.
"""

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from typing import List, Optional

from .apt import sudo_noninteractive_available, windows_admin_available
from .atomic_write import write_atomic
from .tool_check import resolve_bash
from .fix_runner import COST_QUICK, COST_SLOW, QUEUE_VERSION
from .messages import item_label, numbered


def privileges_available(current_os: str) -> bool:
    """True when this process can run elevated ops WITHOUT prompting, per OS.

    Windows uses the admin-token check; every other OS uses passwordless-sudo /
    root detection. Callers use this to decide direct-execution vs deferral for
    an ``elevated: true`` command -- the privileged path runs it directly
    (unchanged behavior), the unprivileged path queues it.
    """
    if current_os == "windows":
        return windows_admin_available()
    return sudo_noninteractive_available()


@dataclass
class FixTask:
    """One deferred operation, as the runner will see it.

    ``label`` is the only field a human reads -- it appears in the runner's
    plan AND in the session message's item list, so it must stand alone
    without the surrounding prose.
    """

    id: str
    kind: str
    label: str
    elevated: bool = False
    command: Optional[str] = None
    packages: List[str] = field(default_factory=list)
    prompt: Optional[str] = None
    target: Optional[str] = None
    # How long this task may legitimately take, from the manifest entry that
    # produced it. The runner does not enforce it (the user is watching); the
    # ENGINE needs it to bound its wait honestly -- see launch_timeout_for.
    timeout: Optional[int] = None
    # COST_QUICK | COST_SLOW: whether this task fetches things over a network
    # (a package install, a multi-GB toolkit) or just touches local state (a
    # symlink, a config write). Drives ordering (order_tasks) and the runner's
    # "this will take a while" note. Not a duration -- a COARSE class, because
    # the honest input is "does it download", not a number anyone can predict.
    cost: str = COST_QUICK
    # Piggyback-only housekeeping: worth fixing, never worth its own nag. An
    # opportunistic task stays in the queue so it rides along whenever a
    # non-opportunistic task launches the runner, but a queue containing ONLY
    # opportunistic tasks surfaces nothing -- see engine._elevation_step and
    # has_actionable below.
    opportunistic: bool = False
    # path_prune only: the exact PATH entries to delete, verbatim as they appear
    # in the registry. Data rather than a re-derivation in the runner -- see
    # Runner.run_path_prune. Also what makes queue.json a real disclosure: the
    # user can read exactly what a fix-all will remove before consenting.
    entries: List[str] = field(default_factory=list)
    # path_prune only: where the runner saves the pre-prune PATH value.
    backup: Optional[str] = None

    def to_json(self) -> dict:
        # Drop unset optionals so the queue file stays readable -- it is a
        # disclosure surface a user may open, not just machine input. Explicit
        # per-field logic (not a `v not in (None, [], False)` filter): that
        # filter compares with `==`, so an int 0 matches False and a meaningful
        # `timeout: 0` would be silently dropped. Identity (`is not None`) for
        # the Optional scalars keeps a real 0 in the output.
        out = {"id": self.id, "kind": self.kind, "label": self.label}
        if self.elevated:
            out["elevated"] = self.elevated
        if self.packages:
            out["packages"] = self.packages
        if self.command is not None:
            out["command"] = self.command
        if self.prompt is not None:
            out["prompt"] = self.prompt
        if self.target is not None:
            out["target"] = self.target
        if self.timeout is not None:
            out["timeout"] = self.timeout
        # Emitted only when slow: quick is the reader's default assumption, and
        # a `"cost": "quick"` on every line is noise in a file a human may open.
        if self.cost == COST_SLOW:
            out["cost"] = self.cost
        # Same rationale as cost: emitted only when set, so the disclosure file
        # explains why THIS task was queued without ever nagging about it.
        if self.opportunistic:
            out["opportunistic"] = True
        if self.entries:
            out["entries"] = self.entries
        if self.backup is not None:
            out["backup"] = self.backup
        return out


# A declared timeout ABOVE the engine's default is a manifest author saying
# "this one is different" -- in practice, that it downloads (winget install
# Nvidia.CUDA declares 3600). Entries that take the default said nothing, so
# they read as quick. This is the fallback for descriptors with no explicit
# `cost`; it keeps every existing manifest correctly classified without an edit.
def cost_of(desc: dict) -> str:
    """Classify one ``command`` descriptor as COST_QUICK or COST_SLOW.

    Explicit `cost` wins -- it is the manifest's own statement of intent, and
    the reason the descriptor format carries the field at all. The timeout
    heuristic is only consulted when nothing was declared.
    """
    declared = desc.get("cost")
    if declared in (COST_QUICK, COST_SLOW):
        return declared
    timeout = desc.get("timeout")
    if isinstance(timeout, int) and not isinstance(timeout, bool) \
            and timeout > DEFAULT_TASK_TIMEOUT:
        return COST_SLOW
    return COST_QUICK


def order_tasks(tasks: List[FixTask]) -> List[FixTask]:
    """Front-load the quick work; leave the downloads for last.

    A stable partition, not a sort: within a cost class the incoming order is
    load-bearing and must survive. The Homebrew installer has to precede any
    brew-based install, and apt's single batched task has to precede commands
    that assume those packages -- both are COST_SLOW, so both keep their
    relative order inside the slow group.

    Why front-load at all: every task here is independent, so the order is free
    to choose, and the user is sitting in front of the window watching. Running
    the symlink and the config write first means they SEE progress and see it
    finish, instead of staring at a 3GB toolkit download wondering whether the
    quick items ran at all. It also means a walked-away user who misses the
    engine's bounded wait has already banked the cheap fixes.
    """
    return ([t for t in tasks if t.cost != COST_SLOW]
            + [t for t in tasks if t.cost == COST_SLOW])


def _command_label(desc: dict, cmd: str, index: int) -> str:
    """Collated label for a deferred `command` task.

    Author-declared ``label`` wins; the raw command is used only when it is
    short enough to sit in a collated line (`Enable Developer Mode` yes, a
    winget invocation with four `--accept-*` flags no); otherwise the
    descriptor's own id, which is a slug by construction. Never truncated --
    the full command is on the task itself and in the runner's transcript.
    """
    return item_label(desc.get("label"), cmd,
                      desc.get("id") or f"command:{index}")


def queue_from_failures(failures, current_os: str) -> List[FixTask]:
    """Harvest ``elevation`` descriptors from the pass's failures into tasks.

    Order: quick tasks first (order_tasks), then the slow ones -- and inside the
    slow group the Homebrew installer first when needed (macOS cannot run brew
    entries without it), then apt as ONE task (a single ``apt-get install``
    resolves co-dependent packages that would fail installed one at a time),
    then commands in pass order.
    """
    apt_packages: List[str] = []
    apt_ids: List[str] = []
    commands: List[FixTask] = []
    brew = False
    prune: Optional[FixTask] = None

    for f in failures:
        desc = f.get("elevation") if isinstance(f, dict) else None
        if not isinstance(desc, dict):
            continue
        if desc.get("os") != current_os:
            continue
        method = desc.get("method")
        if method == "apt":
            pkg = desc.get("package")
            if pkg:
                apt_packages.append(pkg)
                apt_ids.append(pkg)
        elif method == "command":
            cmd = desc.get("command")
            if cmd:
                commands.append(FixTask(
                    id=desc.get("id") or f"command:{len(commands)}",
                    kind="command",
                    # Falling back to the raw command is the worst collated
                    # item there is -- a full `winget install --id ... -e
                    # --accept-package-agreements` line is 100+ chars of flags.
                    # Take it only when it fits; otherwise name the operation
                    # by its id, which is a slug by construction.
                    label=_command_label(desc, cmd, len(commands)),
                    elevated=True,
                    command=cmd,
                    timeout=desc.get("timeout"),
                    cost=cost_of(desc),
                    opportunistic=bool(desc.get("opportunistic")),
                ))
        elif method == "brew_installer":
            brew = True
        elif method == "path_prune":
            entries = [e for e in (desc.get("entries") or []) if isinstance(e, str)]
            if entries:
                count = len(entries)
                prune = FixTask(
                    id=desc.get("id") or "path_prune",
                    kind="path_prune",
                    label=desc.get("label") or (
                        f"Remove {count} dead PATH entr"
                        f"{'y' if count == 1 else 'ies'}"
                    ),
                    # HKCU is the user's own hive -- no elevation needed. On
                    # Windows the runner is elevated wholesale anyway, and UAC
                    # preserves the user profile, so HKCU still resolves to this
                    # user either way.
                    elevated=False,
                    entries=entries,
                    backup=desc.get("backup"),
                    opportunistic=bool(desc.get("opportunistic")),
                )

    tasks: List[FixTask] = []
    if brew:
        tasks.append(FixTask(
            id="brew_installer", kind="brew_installer",
            label="Install Homebrew",
            # The installer elevates itself where it needs to and refuses to
            # run as root, so it must NOT be wrapped in sudo.
            elevated=False,
            # Fetches and compiles; nobody has ever called it quick.
            cost=COST_SLOW,
        ))
    if apt_packages:
        tasks.append(FixTask(
            id="apt:" + ",".join(apt_ids), kind="apt",
            label="Install " + ", ".join(apt_packages),
            elevated=True, packages=apt_packages,
            # An apt-get update + install is a network fetch by definition, so
            # this is structural rather than declared -- no manifest can make it
            # cheap.
            cost=COST_SLOW,
        ))
    tasks.extend(commands)
    if prune is not None:
        # Last of the QUICK group (it is COST_QUICK and order_tasks is stable),
        # so it runs before the slow installs rather than after. That ordering
        # is safe in either direction and needs no reasoning about what else is
        # in the queue: run_path_prune re-reads the registry at execution time
        # and rewrites only `current minus the named entries`, so an install
        # that adds a PATH entry -- before or after -- is preserved either way.
        tasks.append(prune)
    return order_tasks(tasks)


def has_actionable(tasks: List[FixTask]) -> bool:
    """True when the queue is worth surfacing to the user at all.

    A queue whose every task is opportunistic (e.g. only the dead-PATH prune)
    is housekeeping: the engine leaves it on disk so the work rides along the
    next time a real deferral needs the runner, but it must not generate an
    admin nag of its own -- that is the whole point of the flag.
    """
    return any(not t.opportunistic for t in tasks)


def task_labels(tasks: List[FixTask]) -> List[str]:
    """The human labels, for the session message's item list."""
    return [t.label for t in tasks]


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #

def elevate_dir(data_dir: str) -> str:
    return os.path.join(data_dir, "elevate")


def queue_path(data_dir: str) -> str:
    return os.path.join(elevate_dir(data_dir), "queue.json")


def shim_basename(current_os: str) -> str:
    return "bootstrap-fix.bat" if current_os == "windows" else "bootstrap-fix.sh"


def shim_path(data_dir: str, current_os: str) -> str:
    return os.path.join(elevate_dir(data_dir), shim_basename(current_os))


def transcript_path(data_dir: str) -> str:
    """Where the runner writes its transcript (fix_runner.LOG_BASENAME).

    Mirrored by name rather than imported as a constant expression to keep the
    coupling obvious: the runner derives it from the queue path it is handed,
    so both sides land in elevate_dir by construction.
    """
    from .fix_runner import LOG_BASENAME
    return os.path.join(elevate_dir(data_dir), LOG_BASENAME)


def runner_path() -> str:
    """Absolute path to fix_runner.py, resolved from this module's location.

    Not ``${CLAUDE_PLUGIN_ROOT}``: on a version update that variable still
    points at the OLD cache dir (the harvest bug), whereas __file__ is always
    the module actually running.
    """
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "fix_runner.py")


# --------------------------------------------------------------------------- #
# Serialization
# --------------------------------------------------------------------------- #

def render_queue(tasks: List[FixTask], current_os: str) -> str:
    """Serialize the queue.

    ``bash`` is baked in at write time because the runner may be launched from
    an elevated console whose PATH lacks Git's bin dir -- the engine resolves it
    here, in the session that still has a working PATH, on the machine that will
    run it.
    """
    queue = {
        "version": QUEUE_VERSION,
        "os": current_os,
        "tasks": [t.to_json() for t in tasks],
    }
    bash = resolve_bash()
    if bash:
        queue["bash"] = bash
    elif any(t.kind in ("command", "brew_installer") for t in tasks):
        raise RuntimeError(
            "cannot write the bootstrap fix queue: bash was not found at write "
            "time, and the queued command(s) are shell strings that need it. "
            "Install Git for Windows (or run the session from Git Bash) and "
            "start a new session."
        )
    return json.dumps(queue, indent=2) + "\n"


def _render_shim(current_os: str, python: str, runner: str, queue: str) -> str:
    """A minimal launcher for the run-it-yourself path.

    It carries no logic beyond locating the runner: everything the user needs to
    understand is printed BY the runner (the plan), so a shim that explained
    itself would just be a second place to keep in sync.
    """
    if current_os == "windows":
        return "\r\n".join([
            "@echo off",
            "REM Bootstrap remediation launcher (generated).",
            "REM Self-elevates via UAC, then runs the fix queue.",
            "fsutil dirty query %SystemDrive% >nul 2>&1",
            "if %errorlevel% neq 0 (",
            "  powershell -NoProfile -Command \"Start-Process -FilePath '%~f0' -Verb RunAs\" 1>nul 2>nul",
            "  exit /b",
            ")",
            f'"{python}" "{runner}" "{queue}"',
        ]) + "\r\n"
    return "\n".join([
        "#!/usr/bin/env bash",
        "# Bootstrap remediation launcher (generated).",
        "# Runs as YOU and sudo-s only the tasks that need it, so files it",
        "# creates stay yours. Do not run this whole script under sudo.",
        "set -euo pipefail",
        f'exec "{python}" "{runner}" "{queue}"',
    ]) + "\n"


def write_or_clear_queue(tasks: List[FixTask], data_dir: str,
                         current_os: str) -> Optional[str]:
    """Write queue + shim, or remove both when nothing is deferred.

    Returns the queue path when written, else None. Clearing is what makes the
    fix-all offer vanish once its operations succeed.
    """
    qpath = queue_path(data_dir)
    spath = shim_path(data_dir, current_os)
    if not tasks:
        for stale in (qpath, spath):
            try:
                os.remove(stale)
            except OSError:
                pass
        return None
    write_atomic(qpath, render_queue(tasks, current_os))
    write_atomic(
        spath,
        _render_shim(current_os, sys.executable, runner_path(), qpath),
        # .bat must be CRLF; the body is already authored with \r\n, so the
        # writer must not translate again (that yields \r\r\n).
        newline="",
    )
    if current_os != "windows":
        try:
            os.chmod(spath, 0o755)
        except OSError:
            pass
    return qpath


# --------------------------------------------------------------------------- #
# Interactive launch (fix-all only -- NEVER SessionStart)
# --------------------------------------------------------------------------- #

# Floor for the bounded wait, and the grace for answering the UAC prompt itself.
# A fix-all run happens inside Claude's Bash tool, so an unbounded (or blanket
# long) wait would hang the user's whole session on a walked-away prompt --
# hence a bound derived from what the queue actually declares, rather than one
# big number that is simultaneously too short for a 3GB install and too long for
# an ignored dialog.
LAUNCH_TIMEOUT = 600
UAC_GRACE = 120
# The runner holds its window until the user presses a key (fix_runner.main), so
# the engine's wait must cover reading the output as well as producing it. Not
# covering it would make a successful fix-all report a spurious "timed out" the
# moment the user took a minute to read what ran with admin rights on their box.
ACK_GRACE = 300
# What a task may take when its manifest entry declared nothing, mirroring the
# engine's own ENV_CHECK_DEFAULT_TIMEOUT by copy (a top-level import of
# env_features would drag heavy modules into this near-stdlib-only import graph).
# The mirror can silently skew, so a drift test in test_fix_queue.py asserts the
# two constants stay equal.
DEFAULT_TASK_TIMEOUT = 600


def launch_timeout_for(tasks: List[FixTask]) -> int:
    """Bound the engine's wait by what the queue's own entries declare.

    A queue of quick tasks stays snappy to fail; a queue containing a genuinely
    slow install (a multi-GB winget download declaring `timeout: 3600`) gets the
    room it asked for instead of a spurious "timed out" while it is still
    working. Both graces are added on top: UAC_GRACE for answering the dialog
    before the work starts, ACK_GRACE for the runner's hold after it ends.
    """
    total = sum(t.timeout or DEFAULT_TASK_TIMEOUT for t in tasks)
    return max(LAUNCH_TIMEOUT, total + UAC_GRACE) + ACK_GRACE


@dataclass
class LaunchResult:
    """Outcome of an engine-initiated runner launch."""

    launched: bool
    succeeded: bool
    detail: str


def _powershell_exe() -> str:
    """Absolute Windows PowerShell path (hook PATH can be stripped of System32)."""
    sysroot = os.environ.get("SystemRoot") or os.environ.get("SYSTEMROOT") or r"C:\Windows"
    ps = os.path.join(sysroot, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    return ps if os.path.exists(ps) else "powershell.exe"


def launch_fix_runner(queue: str, current_os: str,
                      timeout: int = LAUNCH_TIMEOUT,
                      tasks: Optional[List[FixTask]] = None) -> Optional[LaunchResult]:
    """Launch the runner interactively and wait for it (Windows).

    ONLY called on a fix-all engine run -- the user's typed 'fix-all' is the
    consent that makes the UAC prompt acceptable. SessionStart never calls this.

    Windows: ``Start-Process -Verb RunAs -Wait -PassThru`` on the engine's own
    interpreter. Launching elevated up front means the shim's self-elevation
    hop never fires, so ``-Wait`` covers the REAL process rather than an
    unelevated wrapper that relaunches itself and exits early. A declined UAC
    prompt makes ``-Verb RunAs`` throw (non-zero powershell exit); a user who
    walks away hits the bounded ``timeout``.

    Unix: returns None -- no launch is attempted. The fix-all run executes
    inside a non-interactive hook/Bash-tool subprocess with NO TTY, so neither
    a sudo password prompt nor a secret prompt could be answered. The runner
    needs a console the user is actually sitting at, which only the
    run-it-yourself shim provides.
    """
    if current_os != "windows":
        return None
    if tasks:
        timeout = launch_timeout_for(tasks)
    # Two DIFFERENT quoting layers, both required -- conflating them is a bug:
    #
    #  1. PowerShell parse: single-quoted literals, embedded quotes doubled.
    #     This is what stops an apostrophe in a path (C:\o'brien\...) from
    #     breaking out of the literal.
    #  2. The CHILD's command line: Start-Process joins -ArgumentList elements
    #     with SPACES and does NOT quote them, so each argument must carry its
    #     own double quotes or a space in the path (C:\Users\John Doe\...) splits
    #     the elevated python's argv and it exits before running the runner --
    #     indistinguishable, from out here, from the runner's own exit code 2.
    #
    # -FilePath is exempt from (2): PowerShell quotes it for us.
    ps_python = sys.executable.replace("'", "''")
    ps_args = ", ".join(
        f"'{a}'" for a in (
            '"' + runner_path().replace("'", "''") + '"',
            '"' + queue.replace("'", "''") + '"',
            "--engine",
        )
    )
    ps_cmd = (
        "$ErrorActionPreference = 'Stop'; "
        f"$p = Start-Process -FilePath '{ps_python}' -ArgumentList {ps_args} "
        "-Verb RunAs -Wait -PassThru; "
        "exit $p.ExitCode"
    )
    try:
        proc = subprocess.run(
            [_powershell_exe(), "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        # The timeout kills only the PowerShell process WAITING on the elevated
        # runner; the runner itself is a separate elevated process and keeps
        # going. We accept that orphan: the next session's re-check pass, not
        # this bounded wait, is the authority on what actually cleared.
        return LaunchResult(launched=True, succeeded=False,
                            detail=f"timed out after {timeout}s waiting for the fix runner; "
                                   f"it may still be running -- the next session's re-check "
                                   f"will pick up whatever completed")
    except OSError as e:
        return LaunchResult(launched=False, succeeded=False,
                            detail=f"could not launch: {e}")
    if proc.returncode == 0:
        return LaunchResult(launched=True, succeeded=True, detail="exit code 0")
    stderr = (proc.stderr or "").strip()
    # A declined (or unanswered -- the secure-desktop prompt auto-cancels after
    # ~2 minutes) UAC prompt surfaces as a powershell error RECORD, whose FIRST
    # line is the message ("Start-Process : This command cannot be run due to
    # the error: The operation was canceled by the user."); the trailing lines
    # are position/category noise ending in FullyQualifiedErrorId. Taking the
    # LAST line here reported that noise and hid the cause (observed live,
    # 0.50.0: an unanswered UAC read as a bare "InvalidOperationException").
    # A failed task, by contrast, surfaces as the runner's exit code with no
    # stderr (2 = at least one task did not complete).
    first = next((ln.strip() for ln in stderr.splitlines() if ln.strip()), "")
    detail = first or f"exit code {proc.returncode}"
    return LaunchResult(launched=True, succeeded=False, detail=detail)


# --------------------------------------------------------------------------- #
# The aggregated fix-all item
# --------------------------------------------------------------------------- #

def _run_instruction(current_os: str, data_dir: str) -> str:
    shim = shim_path(data_dir, current_os)
    if current_os == "windows":
        return f'double-click "{shim}" (it self-elevates via UAC)'
    return f'run `bash "{shim}"` in a terminal'


def fix_queue_failure(tasks: List[FixTask], current_os: str, data_dir: str,
                      launch_detail: Optional[str] = None) -> dict:
    """Build the aggregated item that offers fix-all and names what it covers.

    This item SPEAKS FOR the per-task ``needs_elevation`` failures it
    summarizes: the message layer suppresses their individual lines, since
    repeating the elevation rationale once per item is what made the old output
    unreadable.

    ``launch_detail`` is set when a fix-all run launched the runner but it did
    not complete (UAC declined, a task failed, the bounded wait timed out): the
    messages then lead with that outcome and fall back to the run-it-yourself
    instruction -- the engine never re-prompts in a loop.
    """
    labels = task_labels(tasks)
    listed = numbered(labels)
    # The one sentence that states the problem. Shared rather than repeated: it
    # is BOTH what the user reads and (on Windows) the text Claude must put in
    # the AskUserQuestion prompt, and those two drifting apart is how a user
    # ends up answering a question that does not match what they were told.
    intro = f"Bootstrap found issues that need admin access: {listed}."
    if current_os == "windows" and launch_detail:
        # The launch already happened and did not complete (declined UAC, a
        # failed task, a timeout). Re-offering fix-all here would either loop
        # the prompt or -- on a fix-all run, where no fix_all_cmd is attached --
        # leave the user with no path at all. The shim is the honest fallback.
        run = _run_instruction(current_os, data_dir)
        user_msg = f"{intro}\n\nTo fix them, {run}."
        agent_msg = (
            f"Bootstrap deferred {len(tasks)} operation(s) that need elevation: "
            f"{listed}. The consented launch did not complete, so tell the user "
            f"to {run}. Do NOT run it yourself -- it needs the user's "
            f"credentials. Bootstrap re-checks automatically on the next "
            f"session; there is nothing to confirm."
        )
    elif current_os == "windows":
        user_msg = (
            f"{intro}\n\n"
            f"Type 'fix-all' to fix them. You'll be asked to approve an admin "
            f"prompt."
        )
        # ASK, do not merely mention. A "type fix-all" line buried in session
        # start scrolls past unread, so the offer only ever lands if the user
        # happens to notice it -- the fix sits queued for weeks. An
        # AskUserQuestion is a decision the user actually has to see. It is
        # still only an OFFER: "Do nothing" leads, so an absent-minded Enter
        # changes nothing on their machine, and declining costs nothing because
        # the next session re-checks anyway.
        agent_msg = (
            f"Bootstrap deferred {len(tasks)} operation(s) that need a console "
            f"it does not have (elevation and/or an interactive prompt): "
            f"{listed}. Do NOT run the queued commands yourself. Typing "
            f"'fix-all' is easy to miss, so ASK with the AskUserQuestion tool "
            f'rather than only mentioning it. Question: "{intro} Fix them '
            f'now?" Exactly two options, in this order: 1. "Do nothing" '
            f"(the default -- bootstrap re-checks next session, nothing is "
            f'lost); 2. "Fix-all" (re-runs bootstrap with elevation consent: a '
            f"UAC prompt appears, the engine launches the fix runner itself, "
            f"waits for it, and re-checks in the same run). Run the fix-all "
            f'invocation ONLY if the user picks "Fix-all" or types fix-all; on '
            f'"Do nothing", say nothing further and do not re-prompt.'
        )
    else:
        # Unix fix-all has no TTY, so the honest offer is the shim, not fix-all.
        run = _run_instruction(current_os, data_dir)
        user_msg = (
            f"Bootstrap found issues that need admin access: {listed}.\n\n"
            f"To fix them, {run}. It asks for your password where needed."
        )
        agent_msg = (
            f"Bootstrap deferred {len(tasks)} operation(s) that need a terminal "
            f"it does not have: {listed}. Tell the user to {run}. Do NOT run it "
            f"yourself -- it needs the user's credentials and a real TTY, which "
            f"a Bash tool subprocess does not provide. Bootstrap re-checks "
            f"automatically on the next session; there is nothing to confirm."
        )
    if launch_detail:
        prefix = (f"fix-all launched the fix runner but it did not complete "
                  f"({launch_detail}). The runner's transcript -- written when "
                  f"it actually started -- is at "
                  f"{transcript_path(data_dir)}. ")
        user_msg = prefix + user_msg
        agent_msg = prefix + agent_msg
    return {
        "type": "elevation_script",
        "name": "elevation_script",
        "message": user_msg,
        "user_msg": user_msg,
        "agent_msg": agent_msg,
        "script_path": shim_path(data_dir, current_os),
        "queue_path": queue_path(data_dir),
        "labels": labels,
        "plugin": "bootstrap",
        "persist_across_sessions": True,
    }
