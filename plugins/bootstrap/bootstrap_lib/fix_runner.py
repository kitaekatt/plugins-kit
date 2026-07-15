"""Interactive remediation runner: executes a bootstrap fix queue.

Bootstrap runs as a NON-INTERACTIVE Claude Code SessionStart hook. That single
fact is the root constraint behind this module: the hook has no TTY and must
never prompt for a sudo password, trigger a UAC dialog, or block on any dialog
at all. Elevation is the first thing that ran into that wall, but it is not the
only one -- gathering a secret hits the identical wall for a different reason.
Both need the same thing: a console with the user's attention.

So the engine DEFERS such operations (recording an ``elevation`` descriptor on
the failure), serializes them into ``<data_dir>/elevate/queue.json``, and this
runner is the one place that has a TTY to execute them in.

Why a runner + data file instead of a generated script
------------------------------------------------------
This module replaces a per-pass shell/batch CODE GENERATOR. That generator
worked, but splicing commands into shell text cost two hacks that no longer
exist here:

  * it had to REJECT any command containing a double quote outright, because
    ``"<bash.exe>" -c "<cmd>"`` had no escaping rule; and
  * it had to regex-rewrite ``~``/``$HOME`` at render time, because the whole
    script ran under ``sudo`` where ``HOME=/root``. (The underlying HOME problem
    is NOT solved by dropping the codegen -- see :meth:`Runner._shell_argv`.)

Here commands are DATA. They reach bash as a single argv element (never
re-parsed by an outer shell), so quotes are unremarkable; and ``~``/``$HOME``
survive verbatim because the runner restores the invoking user's HOME *inside*
the sudo rather than rewriting the command text (see :meth:`Runner._shell_argv`
-- the home problem is real and does not disappear just because the text is no
longer spliced).

Privilege model (per-task, not per-script)
------------------------------------------
The old script ran wholesale under ``sudo``, which is more privilege than most
tasks need and actively harmful for one: a secret written under sudo lands
root-owned in the user's home, so every later unelevated write fails. Here:

  * **Unix**: the runner runs AS THE USER and wraps only ``elevated`` tasks in
    ``sudo``. Unelevated tasks -- notably secret prompts and their writes --
    stay the user's, so the files they create are the user's too.
  * **Windows**: the engine launches the whole runner elevated (one UAC hop).
    That is safe in a way the Unix case is not: UAC preserves the user profile,
    so ``HOME`` and file ownership are unchanged. ``elevated`` is therefore
    advisory on Windows -- everything already has the token it needs.

Auditability
------------
The generated script's real virtue was that the user could READ it before
approving. A data file is more opaque, so the runner prints the plan -- one
labeled line per task -- before executing. The UAC prompt (or sudo) is the
consent; the plan is the disclosure.

Narration, and the hold
-----------------------
This console is the ONLY place the user learns what bootstrap did to their
machine with admin rights: the engine's session message names the labels, not
the outcomes. So the runner narrates -- ``[n/N]`` progress, the command, and an
explicit per-task verdict -- and then HOLDS until a keypress, unconditionally.

The hold used to be skipped on the engine-launched success path, on the
reasoning that the engine was waiting and a human would not be. That reasoning
was backwards: on success the window closed instantly, which is precisely the
case where the user never got to read what ran. The engine's wait is bounded and
budgets for the acknowledgement (``fix_queue.ACK_GRACE``), so holding costs a
keypress, not correctness.

Stdlib-only, and run as a SCRIPT (``python fix_runner.py <queue.json>``), so it
must not rely on package-relative imports -- the same trap that made the harvest
silently no-op in 0.22.0.
"""

import getpass
import json
import os
import shutil
import subprocess
import sys


# Exit codes. The engine distinguishes these from a UAC decline (which never
# reaches the runner at all -- Start-Process itself throws).
EXIT_OK = 0
EXIT_TASK_FAILED = 2
EXIT_BAD_QUEUE = 3

QUEUE_VERSION = 1

# Kinds the runner knows how to execute. A queue naming anything else is a
# version skew (a newer engine wrote it) -- fail loudly rather than skip
# silently, since a skipped elevated task looks like success to the re-check.
KNOWN_KINDS = frozenset({"command", "apt", "brew_installer", "secret", "path_prune"})

# How long a task is expected to take, as declared by the engine (see
# fix_queue.COST_*). The runner uses it for one thing only: telling the user
# which step is about to sit there apparently doing nothing. A missing/unknown
# value reads as quick -- an un-annotated queue from an older engine should not
# grow scary "this may take a while" notes it never meant.
COST_QUICK = "quick"
COST_SLOW = "slow"
KNOWN_COSTS = frozenset({COST_QUICK, COST_SLOW})
SLOW_NOTE = "downloads / installs -- this can take several minutes"

# Official Homebrew installer. It prompts and may sudo on its own, which is
# exactly why the engine never runs it and it lands here instead.
HOMEBREW_INSTALLER = (
    '/bin/bash -c "$(curl -fsSL '
    'https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
)


def _bash() -> str:
    """Resolve bash for command execution.

    Unix always has it. On Windows the runner may be launched from an elevated
    console whose PATH lacks Git's bin dir, so the queue carries the absolute
    path the engine resolved at write time (`bash` key); this is only the
    fallback for a queue written without one.
    """
    found = shutil.which("bash")
    if found:
        return found
    raise RuntimeError(
        "bash not found on PATH. The queued commands are shell strings and "
        "need Git for Windows (or a system bash) to run."
    )


def is_slow(task):
    """True when the engine flagged this task as a download/install."""
    return task.get("cost") == COST_SLOW


def _strip_quotes(entry):
    """Drop one matched pair of surrounding double quotes.

    cmd.exe strips them when resolving PATH, so ``"C:\\Program Files\\Foo"`` is a
    working entry -- and quoting is mandatory for any path containing a
    semicolon. Probing the quoted string finds nothing on disk, which would
    condemn a live directory (verified: a quoted C:\\WINDOWS was classed dead).
    """
    e = entry.strip()
    if len(e) >= 2 and e[0] == '"' and e[-1] == '"':
        return e[1:-1]
    return e


def _volume_root(path):
    """The drive or UNC share `path` lives on; None when it names no volume."""
    drive, _rest = os.path.splitdrive(path)
    if not drive:
        return None
    # "C:" -> "C:\"; a bare "C:" means "cwd on C", not the root.
    if len(drive) == 2 and drive[1] == ":":
        return drive + os.sep
    return drive  # \\server\share


def is_dead(entry):
    """True when a PATH entry names a directory that is definitively not there.

    Every ambiguous case must resolve to ALIVE. The asymmetry is the whole
    safety property: a false "alive" leaves one stale entry nobody notices; a
    false "dead" silently deletes a directory the user needs.

    Getting that right is harder than it looks, and neither obvious approach
    works:

      * ``os.path.isdir`` NEVER raises -- it swallows OSError internally and
        returns False. So "unreachable" is indistinguishable from "absent", and
        an offline share reads as dead.
      * ``os.stat`` raises, but on Windows an offline UNC host and an unmapped
        drive both surface as ``FileNotFoundError`` winerror 3 -- the same class
        as a genuinely missing directory. The exception says nothing useful.

    So ask a different question: is the VOLUME reachable? An unmapped ``Z:``, a
    disconnected ``\\\\nas\\share``, an empty removable drive -- all fail there,
    and we decline to judge anything on them. Only when the drive/share IS
    present does a missing directory mean the entry is genuinely dead.

    Lives here rather than in path_prune because fix_runner is the module that
    must survive being run as a bare script with no package context, and the
    runner re-checks this at prune time (a cached verdict can be stale by then).
    path_prune imports it back -- the same direction fix_queue already borrows
    this module's constants.
    """
    try:
        expanded = os.path.expandvars(_strip_quotes(entry)).strip()
    except (TypeError, ValueError):
        return False
    if not expanded:
        return False
    # An undefined %VAR% survives expandvars verbatim. We cannot know what it
    # points at, so it is not ours to delete.
    if "%" in expanded:
        return False
    root = _volume_root(expanded)
    if root is None:
        # Driveless ("\foo\bar") resolves against whatever the current drive
        # happens to be, which is not a question with a stable answer. Keep it.
        return False
    if not os.path.isdir(root):
        return False
    return not os.path.isdir(expanded)


def _broadcast_environment_change():
    """Tell other top-level windows the environment changed (WM_SETTINGCHANGE).

    A registry PATH edit is invisible to already-running processes until they
    are told; this is what makes a new shell pick it up without a logout, and it
    matches what .NET's SetEnvironmentVariable does. Deliberately best-effort:
    the prune already succeeded by the time we get here, so failing to notify
    must not turn a completed task into a reported failure.

    Reimplemented rather than imported from path_check because this module is
    stdlib-only and runs as a script with no package context -- the same
    constraint documented at the top of the file.
    """
    try:
        import ctypes
        from ctypes import wintypes
    except (ImportError, ValueError):
        return
    try:
        HWND_BROADCAST = 0xFFFF
        WM_SETTINGCHANGE = 0x001A
        SMTO_ABORTIFHUNG = 0x0002
        send = ctypes.windll.user32.SendMessageTimeoutW
        send.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPCWSTR,
            wintypes.UINT, wintypes.UINT, ctypes.POINTER(wintypes.DWORD),
        ]
        send(HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment",
             SMTO_ABORTIFHUNG, 5000, ctypes.byref(wintypes.DWORD()))
    except (AttributeError, OSError):
        pass


def _run(argv, label):
    """Run argv, streaming output to the console the user is watching."""
    print(f"    $ {label}")
    # The child inherits this stdout and writes to the fd directly, so anything
    # still sitting in OUR buffer would surface AFTER the child's output --
    # narration printed after the thing it narrates. A TTY hides this (line
    # buffering); a pipe (the runner tee'd to a log) does not.
    sys.stdout.flush()
    try:
        proc = subprocess.run(argv)
    except OSError as e:
        print(f"    ! could not launch: {e}")
        return False
    if proc.returncode != 0:
        print(f"    ! exited {proc.returncode}")
        return False
    return True


def wait_for_key(prompt):
    """Block until the user presses Space or Enter.

    A single keypress, not a line: this is an acknowledgement, not input, and
    the window it holds open was opened FOR the user to read. Falls back to
    line-reading `input()` wherever raw-mode is unavailable (no TTY, a piped
    stdin, a console host without the platform module) -- there the hold still
    works, it just wants Enter.
    """
    print(prompt, end="", flush=True)
    try:
        _read_one_key()
    except (EOFError, KeyboardInterrupt, OSError, ImportError, ValueError):
        # A hold is a courtesy; it must never be the thing that fails the run.
        pass
    print()


def _accepted(ch):
    return ch in (" ", "\r", "\n")


def _read_one_key():
    # The TTY check comes FIRST, before any platform branch: msvcrt reads the
    # console directly rather than stdin, so on Windows it would happily block
    # forever in a context that has no console at all (a test runner, a piped
    # invocation) -- a hang, not a fallback. No TTY means no raw mode anywhere.
    if not sys.stdin.isatty():
        input()
        return
    if os.name == "nt":
        import msvcrt
        while True:
            ch = msvcrt.getwch()
            if ch == "\x03":
                raise KeyboardInterrupt
            if _accepted(ch):
                return
    import termios
    import tty
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        while True:
            ch = sys.stdin.read(1)
            if ch == "\x03":
                raise KeyboardInterrupt
            if ch == "":
                raise EOFError
            if _accepted(ch):
                return
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


class Runner:
    """Executes one queue. Holds the resolved bash + OS so tasks stay dumb."""

    def __init__(self, queue):
        self.os = queue.get("os") or ""
        self.bash = queue.get("bash") or _bash()
        self.tasks = queue.get("tasks") or []
        # The runner runs as the invoking user, so this is the user's home --
        # captured before any sudo, which is the whole point (see _shell_argv).
        self.home = os.path.expanduser("~")

    @property
    def _is_windows(self):
        return self.os == "windows"

    def _shell_argv(self, command, elevated):
        """Build argv for a shell command, elevating only when asked.

        The command is passed as ONE argv element to `bash -c`, so it is parsed
        exactly once (by that bash) and never by an intermediate shell. This is
        what removes the old renderer's double-quote ban.

        ``env HOME=`` is not incidental. sudo's default ``env_reset`` sets HOME
        to the TARGET user's home, so a queued fix spelling ``~`` or ``$HOME``
        -- the documented env_check form, e.g.
        ``bash ~/.claude/scripts/env/sudoers.sh fix`` -- would resolve against
        /root and fail. The deleted renderer solved this by rewriting the
        command TEXT at render time; fixing the ENVIRONMENT instead keeps the
        command verbatim data (the point of the queue) and puts the correction
        where the privilege change actually happens. ``env`` runs inside the
        sudo'd process, after env_reset, so it cannot be undone by sudoers.

        On Windows the runner is already elevated as a whole (UAC has no
        per-command granularity) and preserves the user profile, so `elevated`
        needs no action here and HOME is already correct.
        """
        argv = [self.bash, "-c", command]
        if elevated and not self._is_windows:
            # -n would fail outright with no TTY; the runner HAS a TTY (the user
            # started it), so an interactive password prompt is correct here.
            argv = ["sudo", "env", f"HOME={self.home}"] + argv
        return argv

    def run_command(self, task):
        return _run(self._shell_argv(task["command"], task.get("elevated", False)),
                    task["command"])

    def run_apt(self, task):
        packages = task.get("packages") or []
        if not packages:
            return True
        # Fresh machines can have stale/empty package lists, so refresh first --
        # an install against a stale list fails on a package that exists.
        if not _run(["sudo", "apt-get", "update"], "apt-get update"):
            return False
        return _run(["sudo", "apt-get", "install", "-y"] + packages,
                    "apt-get install -y " + " ".join(packages))

    def run_brew_installer(self, task):
        # Never sudo: the Homebrew installer refuses to run as root and asks
        # for elevation itself where it needs it.
        return _run([self.bash, "-c", HOMEBREW_INSTALLER], "install Homebrew")

    def run_secret(self, task):
        """Prompt for a secret and write it to a file, owned by the user.

        The value is read with echo off and written 0600. It deliberately never
        passes through the engine, the hook output, or the Claude transcript --
        the console is the only place it exists. This is why the task must NOT
        be elevated on Unix: a root-owned secret file breaks every later
        unelevated write.
        """
        target = os.path.expanduser(task["target"])
        value = getpass.getpass(f"  {task.get('prompt') or task['label']}: ")
        if not value:
            print("  ! empty value, skipped")
            return False
        parent = os.path.dirname(target)
        if parent:
            os.makedirs(parent, exist_ok=True)
        # Create with 0600 from the outset rather than chmod-after-write, which
        # would leave the secret world-readable for the width of the write.
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(value)
        print(f"  wrote {target}")
        return True

    def run_path_prune(self, task):
        """Remove named entries from the Windows User PATH.

        The engine decided WHAT is dead (bootstrap_lib.path_prune) and put the
        exact strings in the task; this only removes them. That split is not
        incidental: re-deriving deadness here would duplicate the
        expand-before-testing rule that keeps `%JAVA_HOME%\\bin` from being
        condemned, and two copies of that rule is one copy too many. It also
        makes the queue file honest -- the user can read queue.json and see
        precisely which entries a fix-all will delete, before consenting.

        Removal is BY TEXT and only for entries still present, so a PATH that
        changed between detection and consent loses nothing it did not name:
        anything added meanwhile is simply untouched.
        """
        entries = task.get("entries") or []
        if not entries:
            return True
        try:
            import winreg
        except ImportError:
            print("    ! winreg unavailable (not Windows)")
            return False

        targets = {self._norm_path_entry(e) for e in entries}
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, "Environment", 0,
                winreg.KEY_READ | winreg.KEY_WRITE,
            ) as key:
                try:
                    current, value_type = winreg.QueryValueEx(key, "Path")
                except FileNotFoundError:
                    print("    ! no User PATH value to prune")
                    return False

                kept, removed, revived = [], [], []
                for entry in current.split(";"):
                    if not entry.strip():
                        continue
                    if self._norm_path_entry(entry) not in targets:
                        kept.append(entry)
                    elif is_dead(entry):
                        removed.append(entry)
                    else:
                        # RE-CHECKED, not trusted. Detection may be sessions old
                        # (the finding persists until the user consents), and
                        # deadness is a property of the FILESYSTEM while the
                        # engine's cache keys on the PATH TEXT. Uninstall a tool
                        # (entry -> dead), decline the prune, reinstall to the
                        # same place: the installer sees its PATH entry already
                        # present and changes nothing, so the text -- and the
                        # hash -- never move, and the stale verdict would delete
                        # a live directory. The queue says what to consider; the
                        # filesystem, now, says what to do.
                        revived.append(entry)
                        kept.append(entry)

                for entry in revived:
                    print(f"    ~ {entry} exists again -- keeping")
                if not removed:
                    print("    nothing to remove (PATH changed since detection)")
                    return True

                backup = task.get("backup")
                if backup:
                    # Destructive and not obviously reversible from memory --
                    # 30 entries is not something a user reconstructs by hand.
                    with open(backup, "w", encoding="utf-8") as fh:
                        fh.write(current)
                    print(f"    backed up previous PATH -> {backup}")

                for entry in removed:
                    print(f"    - {entry}")
                # Preserve the value TYPE: a User PATH is normally
                # REG_EXPAND_SZ, and rewriting it as REG_SZ would stop Windows
                # expanding every %VAR% in it -- breaking entries this prune was
                # careful not to even touch.
                winreg.SetValueEx(key, "Path", 0, value_type, ";".join(kept))
        except OSError as e:
            print(f"    ! could not update the User PATH: {e}")
            return False

        print(f"    removed {len(removed)} dead entr"
              f"{'y' if len(removed) == 1 else 'ies'}, kept {len(kept)}")
        _broadcast_environment_change()
        return True

    @staticmethod
    def _norm_path_entry(entry):
        """Compare-key for a PATH entry: case- and trailing-slash-insensitive.

        Windows paths are case-insensitive and `C:\\x` / `C:\\x\\` are the same
        directory, so a prune must match them as one. Matches the normalization
        path_check uses when deciding an entry is already present.
        """
        return entry.strip().rstrip("\\/").lower()

    def dispatch(self, task):
        kind = task["kind"]
        if kind == "command":
            return self.run_command(task)
        if kind == "path_prune":
            return self.run_path_prune(task)
        if kind == "apt":
            return self.run_apt(task)
        if kind == "brew_installer":
            return self.run_brew_installer(task)
        if kind == "secret":
            return self.run_secret(task)
        raise ValueError(f"unknown task kind {kind!r}")


def validate(queue):
    """Return a list of problems; empty means the queue is executable."""
    problems = []
    if not isinstance(queue, dict):
        return ["queue is not a JSON object"]
    version = queue.get("version")
    if version != QUEUE_VERSION:
        problems.append(
            f"queue version {version!r} is not {QUEUE_VERSION} (a different "
            f"bootstrap wrote it; start a new session to regenerate it)"
        )
    tasks = queue.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        problems.append("queue has no tasks")
        return problems
    for i, task in enumerate(tasks):
        if not isinstance(task, dict):
            problems.append(f"task {i} is not an object")
            continue
        kind = task.get("kind")
        if kind not in KNOWN_KINDS:
            problems.append(f"task {i}: unknown kind {kind!r}")
        if not task.get("label"):
            problems.append(f"task {i}: missing label")
        if kind == "command" and not task.get("command"):
            problems.append(f"task {i}: command task has no command")
        if kind == "secret" and not task.get("target"):
            problems.append(f"task {i}: secret task has no target")
        if kind == "path_prune":
            entries = task.get("entries")
            # An empty/missing list is a version skew or a writer bug, not a
            # no-op to shrug at: the plan would promise the user a prune and
            # then silently do nothing, which reads as success to the re-check.
            if not isinstance(entries, list) or not entries:
                problems.append(f"task {i}: path_prune task has no entries")
            elif not all(isinstance(e, str) and e.strip() for e in entries):
                problems.append(f"task {i}: path_prune entries must be non-empty strings")
        cost = task.get("cost")
        if cost is not None and cost not in KNOWN_COSTS:
            problems.append(f"task {i}: unknown cost {cost!r}")
    return problems


def print_plan(queue):
    """Disclose what is about to run, before it runs.

    Order is the engine's (quick work first -- see fix_queue.order_tasks); this
    just makes that order legible, so the user can see the cheap items clear
    before the long download starts rather than wondering if it hung.
    """
    tasks = queue["tasks"]
    print()
    print("=" * 62)
    print("  Bootstrap remediation")
    print("=" * 62)
    print()
    print("  Bootstrap runs in the background with no console, so it could not")
    print("  do the following without you. Running now, quickest first:")
    print()
    for i, task in enumerate(tasks, 1):
        mark = "admin" if task.get("elevated") else "     "
        note = f"  ({SLOW_NOTE})" if is_slow(task) else ""
        print(f"    {i}. [{mark}] {task['label']}{note}")
    print()


def run_queue(queue):
    """Execute every task, reporting per-task outcome. Returns an exit code.

    Continues past a failure rather than aborting: the tasks are independent,
    and one broken fix should not block the rest. The engine's re-check pass is
    the authority on what actually cleared, so a task that fails here simply
    stays failed there.
    """
    runner = Runner(queue)
    tasks = queue["tasks"]
    total = len(tasks)
    failed = []
    for i, task in enumerate(tasks, 1):
        print("-" * 62)
        print(f"  [{i}/{total}] {task['label']}")
        if is_slow(task):
            print(f"    ({SLOW_NOTE}; leave this window open)")
        try:
            ok = runner.dispatch(task)
        except Exception as e:  # noqa: BLE001 - one bad task must not kill the run
            print(f"    ! {e}")
            ok = False
        # An explicit verdict per step, not just noise-on-failure: a silent step
        # is indistinguishable from a skipped one to the person watching.
        print(f"    -> {'done' if ok else 'FAILED'}")
        if not ok:
            failed.append(task["label"])
        print()
    print("=" * 62)
    if failed:
        print(f"  {len(failed)} of {total} did not complete:")
        for label in failed:
            print(f"    - {label}")
        print()
        print("  Bootstrap will re-check these on your next session.")
        return EXIT_TASK_FAILED
    print(f"  All {total} completed.")
    return EXIT_OK


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: fix_runner.py <queue.json>", file=sys.stderr)
        return EXIT_BAD_QUEUE
    path = argv[0]
    # `--engine` marks an engine-initiated run. It no longer gates the hold (the
    # window always waits for the user); it only changes what the hold SAYS, so
    # the user knows whether a key resumes their Claude session or just closes a
    # window they double-clicked.
    engine_launch = "--engine" in argv[1:]
    try:
        with open(path, "r", encoding="utf-8") as fh:
            queue = json.load(fh)
    except (OSError, ValueError) as e:
        print(f"could not read queue {path}: {e}", file=sys.stderr)
        return EXIT_BAD_QUEUE
    problems = validate(queue)
    if problems:
        print(f"queue {path} is not executable:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return EXIT_BAD_QUEUE

    print_plan(queue)
    code = run_queue(queue)

    # Always hold. This window is the only account the user gets of what ran
    # elevated on their machine, and it is spawned detached -- closing it on
    # success would hide exactly the runs that had nothing to complain about.
    # The engine budgets for this wait (fix_queue.ACK_GRACE).
    print()
    wait_for_key(
        "  Press Space or Enter to continue. "
        if engine_launch else
        "  Press Space or Enter to close. "
    )
    return code


if __name__ == "__main__":
    sys.exit(main())
