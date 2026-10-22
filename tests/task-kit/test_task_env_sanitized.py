"""The task-kit suite never inherits a live Claude Code session's identity.

pytest run from a Claude Code session inherits ``CLAUDE_PID`` and ``CLAUDE_CODE_SESSION_ID``; a test that ran
``task.py work`` or the handshake under them would act as that real session (register it, hold its task,
publish). ``tests/task-kit/conftest.py`` removes both at import.

A guard that only looks at its own process proves nothing when the removal is missing and the variables were
never set in the first place, so the outer test starts a CHILD pytest with both deliberately set and the inner
test, which only runs in that child, asserts they are gone.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

INNER_FLAG = "TASK_KIT_ENV_GUARD_INNER"
SESSION_VARIABLES = ("CLAUDE_PID", "CLAUDE_CODE_SESSION_ID")


@pytest.mark.skipif(INNER_FLAG not in os.environ, reason="runs only inside the child pytest the guard starts")
def test_inner_the_session_variables_are_gone_inside_the_suite():
    assert [name for name in SESSION_VARIABLES if name in os.environ] == []


def test_the_suite_removes_the_session_identity_a_parent_session_exports():
    env = dict(os.environ, CLAUDE_PID=str(os.getpid()), CLAUDE_CODE_SESSION_ID="a-real-looking-session-id")
    env[INNER_FLAG] = "1"
    node = Path(__file__).name + "::test_inner_the_session_variables_are_gone_inside_the_suite"
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:randomly", node],
        cwd=Path(__file__).parent, env=env, capture_output=True, text=True, timeout=120)
    output = done.stdout + done.stderr
    assert "1 passed" in output and "skipped" not in output, output  # it ran (a skip would be a vacuous green)
    assert done.returncode == 0, output


def test_the_outer_process_itself_has_neither_variable():
    assert [name for name in SESSION_VARIABLES if name in os.environ] == []
