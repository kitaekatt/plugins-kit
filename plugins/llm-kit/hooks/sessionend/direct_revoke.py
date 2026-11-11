#!/usr/bin/env python3
"""SessionEnd hook: the session's /direct enablement ends with it.

Owner ruling 2026-11-11: "sure /clear can turn it off". Claude Code fires
SessionEnd with the ending session's id when the session exits and when the
owner runs ``/clear`` (reason ``clear``); either way this hook deletes the
session's enablement record (direct_relay.revoke), so the gate refuses relay
client calls again until the owner types /direct anew. A session that ends
without the hook running (a killed process) leaves its record behind; it names
a session id no new session receives.

Output: nothing on success. Bad hook input or a record that cannot be deleted
exits 1 with a message on stderr (SessionEnd cannot block anything).
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import direct_relay as R  # noqa: E402


def run(payload: Mapping[str, Any], env: Mapping[str, str]) -> bool:
    """Delete the ending session's record; whether one existed."""
    return R.revoke(env, payload["session_id"])


def main(argv: Optional[Sequence[str]] = None, stdin: Any = None, env: Optional[Mapping[str, str]] = None) -> int:
    import os

    stdin = sys.stdin if stdin is None else stdin
    env = os.environ if env is None else env
    try:
        run(R.read_payload(stdin), env)
    except R.HookError as exc:
        print(f"direct_revoke.py: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
