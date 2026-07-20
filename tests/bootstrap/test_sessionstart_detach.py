"""Contract tests for session-bootstrap.sh's detached-provisioning structure.

MEASURED (2026-07-20, synthetic SessionStart hooks + timed `claude -p`):
Claude Code blocks session readiness on the SessionStart hook's completion --
process exit AND stdout-pipe EOF. A background child that inherits the hook's
stdout holds the session as long as foreground work would; a child with all
fds redirected costs zero. These tests pin the structure that encodes that
finding: the foreground path ends at "gates + emit JSON", and everything else
runs inside _provision, dispatched detached with stdin/stdout/stderr
redirected. Text-level (like the rescue drift test) -- cheap, and any
restructure that breaks the contract has to touch these exact lines.
"""

import re
from pathlib import Path

HOOK = (
    Path(__file__).resolve().parents[2]
    / "plugins" / "bootstrap" / "hooks" / "sessionstart" / "session-bootstrap.sh"
)


def _text() -> str:
    return HOOK.read_text(encoding="utf-8")


class TestDetachedProvisionContract:
    def test_provision_function_defined(self):
        assert re.search(r"^_provision\(\) \{", _text(), re.M), (
            "_provision() must wrap all post-gate provisioning work"
        )

    def test_detached_dispatch_redirects_all_fds(self):
        # The load-bearing line: stdin, stdout AND stderr redirected, backgrounded.
        # Claude Code waits on stdout-pipe EOF across children, so a dispatch
        # that drops any redirection re-blocks session startup.
        assert "_provision </dev/null >/dev/null 2>&1 &" in _text()

    def test_console_mode_stays_synchronous(self):
        # Console mode must run _provision in the main shell (its engine exec
        # and inline output depend on it).
        assert re.search(
            r'if \[ -n "\$FLAG_CONSOLE" \]; then\s*\n\s*_provision\s*\n', _text()
        ), "console mode must invoke _provision synchronously"

    def test_json_emitted_before_provision_definition(self):
        # The fire-and-forget hook JSON must be emitted on the foreground path,
        # before any provisioning work.
        text = _text()
        json_pos = text.index('echo \'{"continue": true, "suppressOutput": true}\'')
        provision_pos = text.index("_provision() {")
        assert json_pos < provision_pos

    def test_gates_stay_foreground(self):
        # The cooldown/session-guard gates must run BEFORE _provision is
        # defined (i.e. on the foreground path) -- they are what keeps a
        # skipped invocation at ~zero cost and prevents concurrent passes.
        text = _text()
        cooldown_gate = text.index('if [ -f "$_COOLDOWN_FILE" ]')
        provision_pos = text.index("_provision() {")
        assert cooldown_gate < provision_pos
