"""Bootstrap script for hue-kit.

Single entry point ``bootstrap(ctx)`` runs at session start and checks whether
hue-kit has an application key configured. The key is the one thing a user must
supply -- it cannot be auto-detected (minting one requires pressing the bridge's
physical link button). The bridge IP, by contrast, is auto-discovered on first
use, so it is not gated here.

Resolution order for "is a key configured?":
  1. HUE_APP_KEY env var (the key value directly), or
  2. HUE_KEY_FILE env var (a path to a file holding it), or
  3. the paired key file the CLI writes -- ``<data_dir>/app-key.txt`` (created
     by ``hue-kit pair``).

If none is present, register a fix-all failure nudging the user through
``hue-kit discover`` + ``hue-kit pair``. Once any of the three is satisfied the
nag clears. Stdlib-only; no venv required.
"""

import os
from pathlib import Path
from typing import Any

PAIRED_KEY_FILENAME = "app-key.txt"


def bootstrap(ctx: Any) -> None:
    paired_key = Path(ctx.data_dir) / PAIRED_KEY_FILENAME
    has_key = bool(os.environ.get("HUE_APP_KEY") or os.environ.get("HUE_KEY_FILE")) \
        or paired_key.is_file()

    if has_key:
        ctx.log_ok("hue-kit: application key configured")
        return

    # DEFERRED, not a failure. Pairing needs a physical button press on a
    # bridge, so a machine that cannot reach one -- or a developer who never
    # invokes hue-kit -- can never satisfy it, and add_failure would prompt
    # them at every session start forever. This is bootstrap's stated case
    # for the deferred channel: a credential only SOME capability needs.
    # The point-of-need code finds this in deferred_requirements.json and
    # asks then, when the user has the context to decide.
    ctx.add_deferred_requirement(
        "hue_bridge_pairing",
        satisfied_by="hue-kit pair",
        user_msg="hue-kit needs to pair with your Hue bridge before it can "
                 "read or write scenes",
        agent_msg=(
            "hue-kit has no application key yet, so it needs to pair with the "
            "user's Hue bridge. Pairing needs a PHYSICAL button press, so this "
            "is an ASK -- the framework will have you confirm via AskUserQuestion "
            "first. You run the pairing yourself -- the user only presses the "
            "button. After the user agrees to the fix:\n"
            "  1. You MAY run `hue-kit discover` yourself (non-interactive) to "
            "find the bridge.\n"
            "  2. Ask via AskUserQuestion: \"Ready to pair the bridge? Confirm "
            "and you will have ~30 seconds to press the button.\" Options, in "
            "order: \"I'm ready to pair\" / \"I'm not ready to pair\".\n"
            "  3. On \"I'm ready\": start `hue-kit pair` IN THE BACKGROUND (no "
            "flags -- it goes non-interactive on its own when not run from a "
            "terminal), CONFIRM the process is still alive, and only THEN tell "
            "the user \"press the round button on top of the bridge now\". The "
            "command blocks up to 30s polling, so the instruction must not wait "
            "on it -- but a command that died instantly must not be announced "
            "as ready either, or the user presses the button for nothing.\n"
            "  4. Relay the result: paired (key stored 0600, this nudge clears) "
            "or the timeout message, in which case offer to retry from step 2.\n"
            "Alternatively the user can set HUE_APP_KEY or HUE_KEY_FILE to an "
            "existing key. If they do not use hue-kit, they can decline."
        ),
    )
    # Verbose-only: the check logged its outcome, but an unmet DEFERRED
    # requirement is not an action taken and must not be shown every session.
    ctx.log_ok("hue-kit: no application key yet; pairing deferred to first use")
