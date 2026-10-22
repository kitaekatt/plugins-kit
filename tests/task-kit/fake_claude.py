"""A stand-in for the Claude Code process, shaped like the real process tree.

Claude Code runs a Bash tool command as ``zsh -c "source ~/.claude/shell-snapshots/...; <command>"``, a
short-lived shell whose command line contains "claude", and exports ``CLAUDE_PID`` (its own process id) and
``CLAUDE_CODE_SESSION_ID`` into that shell's environment. This module reproduces that shape without Claude:

    fake claude (python, pid = CLAUDE_PID)  ->  bash -c "<snapshot comment>; <command>"  ->  <command>

The fake stays alive after the command returns, until its stdin closes, so a test can look at what the
command left behind while the "harness" is still running (a hold keyed on the dead shell would be gone).

Run as a script: ``fake_claude.py <session id> <shell command>``; it prints one JSON line (the command's
return code and output) and then waits on stdin. :class:`FakeClaude` wraps that for tests.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SNAPSHOT_COMMENT = "# source ~/.claude/shell-snapshots/snapshot-bash-0000000000-fake.sh"


def tool_shell_script(command: str) -> str:
    """The ``bash -c`` script of a Bash tool command: a snapshot line, the command, then an explicit exit.

    The trailing ``exit`` keeps bash from exec-ing a lone command (which would drop the shell from the tree).
    """
    return f"{SNAPSHOT_COMMENT}\n{command}\nstatus=$?\nexit $status\n"


def bash_available() -> bool:
    return shutil.which("bash") is not None


def _main(argv: list[str]) -> int:
    session_id, command = argv[0], argv[1]
    os.environ["CLAUDE_PID"] = str(os.getpid())
    os.environ["CLAUDE_CODE_SESSION_ID"] = session_id
    done = subprocess.run(["bash", "-c", tool_shell_script(command)], capture_output=True, text=True)
    print(json.dumps({"returncode": done.returncode, "stdout": done.stdout, "stderr": done.stderr}), flush=True)
    sys.stdin.read()
    return 0


class FakeClaude:
    """Start the fake, wait for the command to finish, keep the fake alive until :meth:`close`."""

    def __init__(self, session_id: str, command: str, *, env: dict | None = None, cwd: Path | None = None) -> None:
        self.session_id = session_id
        environment = dict(os.environ if env is None else env)
        environment.pop("CLAUDE_PID", None)
        environment.pop("CLAUDE_CODE_SESSION_ID", None)
        self.process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), session_id, command],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=environment, cwd=cwd)
        line = self.process.stdout.readline()
        if not line:
            self.close()
            raise RuntimeError("the fake Claude process exited before its command finished")
        self.result = json.loads(line)

    @property
    def pid(self) -> int:
        return self.process.pid

    def close(self) -> None:
        if self.process.poll() is None:
            try:
                self.process.stdin.close()
            except OSError:
                pass
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()

    def __enter__(self) -> "FakeClaude":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
