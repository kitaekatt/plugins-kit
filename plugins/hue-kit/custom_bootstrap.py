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

    ctx.add_failure(
        "hue_bridge_pairing",
        field="HUE_APP_KEY",
        user_msg=(
            "hue-kit isn't paired with a Hue bridge yet. Ask Claude to "
            "'fix-all', or run `hue-kit discover` then `hue-kit pair` (you'll "
            "press the bridge's link button)."
        ),
        agent_msg=(
            "hue-kit has no application key configured. Give the user this "
            "prepared statement, verbatim:\n\n"
            "  > hue-kit isn't set up on this machine yet. Two steps:\n"
            "  >   1. Find your bridge:   hue-kit discover\n"
            "  >   2. Pair (interactive -- press the bridge's round LINK BUTTON "
            "when asked):\n"
            "  >        ! hue-kit pair\n"
            "  > The `!` runs it in your prompt so the button-press + key "
            "creation happen locally; the minted key is stored 0600 and this "
            "nag clears.\n\n"
            "You (Claude) MAY run `hue-kit discover` yourself (non-interactive), "
            "but `hue-kit pair` needs the physical link-button press, so the "
            "USER must run the bang-prefixed form. Alternatively the user can "
            "set HUE_APP_KEY or HUE_KEY_FILE to an existing key -- either also "
            "clears this. If the user does not use hue-kit, they can ignore "
            "this nudge."
        ),
    )
    ctx.log("hue-kit: no application key -- pairing needed")
