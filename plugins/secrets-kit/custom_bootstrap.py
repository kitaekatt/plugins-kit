"""Bootstrap script for secrets-kit.

Deliberately thin. Everything of substance lives in ``lib/secrets_kit/`` and
takes plain paths; this file's whole job is to translate between ``ctx`` and
that package. That is the integration seam: when the bootstrap service model
lands and secrets folds into the engine, this adapter is what gets replaced,
and the package moves unchanged.

The pass itself is one idempotent converge (check = hash compare, fix =
decrypt + write). It is silent in the steady state, never blocks a session on
connectivity, and raises exactly one ASK -- the one-time per-machine unlock,
which is the only step a human can perform.
"""

import json
import os
import sys
from pathlib import Path
from typing import Any, List, Optional

_LIB_DIR = os.path.join(os.path.dirname(__file__), "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from secrets_kit.converge import converge  # noqa: E402

# The machine-facing declaration lives beside env.json in the private
# claude-settings repo, not in the plugin: the mechanism is public, the
# instance data is not.
CONFIG_PATH = Path.home() / ".claude" / "secrets.json"
ENV_PATH = Path.home() / ".claude" / "env.json"


def bootstrap(ctx: Any) -> None:
    """Converge this machine's secrets, or explain why it cannot."""
    data_dir = Path(ctx.data_dir)

    result = converge(
        CONFIG_PATH,
        data_dir,
        known_machines=_known_machines(),
    )

    for note in result.notes:
        ctx.log(f"secrets: {note}")

    for failure in result.failures:
        kwargs = {
            "user_msg": failure.user_msg,
            "agent_msg": failure.agent_msg,
        }
        if failure.ask_reason:
            kwargs["ask_reason"] = failure.ask_reason
        ctx.add_failure(failure.key, **kwargs)

    if result.skipped_reason and not result.failures:
        # "not configured" is the third-party default and must stay quiet:
        # a plugin nobody has declared anything for should produce no noise.
        ctx.log(result.summary())
        return

    if result.failures:
        ctx.log(result.summary())
        return

    ctx.log_ok(result.summary())


def _known_machines() -> Optional[List[str]]:
    """Machine names from env.json, for the cross-check in the pass.

    Read leniently on purpose: env.json is the ENGINE's manifest, and a plugin
    has no business failing a session over its shape. If it cannot be read,
    the cross-check is simply skipped -- the engine itself will report a
    malformed env.json far more precisely than we could.
    """
    try:
        data = json.loads(ENV_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    machines = data.get("machines")
    if not isinstance(machines, dict) or not machines:
        return None
    return sorted(machines)
