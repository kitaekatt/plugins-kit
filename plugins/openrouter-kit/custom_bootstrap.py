"""Bootstrap script for openrouter-kit.

Single entry point ``bootstrap(ctx)`` runs at session start and:

1. Resolves the OpenRouter API key from env var, per-project .env, or the
   user-scoped credential file under ``ctx.data_dir``.
2. If the key is missing, autodetects it from legacy locations (the env var
   itself, and loc-ops's ``<project>/.local-data/loc/.env``). When a legacy
   key is found, it is migrated into the canonical user-scoped .env.
3. If still missing, registers a fix-all failure so Claude prompts the user
   to obtain a key from openrouter.ai/keys and run ``openrouter-kit set-key``.
4. If present, validates the key against ``GET /auth/key``. Failures (401,
   402) become fix-all entries with specific remediation. Successful checks
   are content-hashed into ``last_validated.sha256`` (hash + validation
   timestamp) so subsequent sessions skip the network round-trip when the
   key has not changed -- but only for ~7 days, so revocation or credit
   exhaustion is re-detected without a key change.

The script is stdlib-only -- it imports ``openrouter_kit`` from ``lib/``
beside this file. No venv required.
"""

import hashlib
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional, Tuple

# Make the bundled lib/ importable. Mirrors the unreal-kit and p4-kit pattern.
_LIB_DIR = os.path.join(os.path.dirname(__file__), "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from openrouter_kit.api_key import get_api_key  # noqa: E402
from openrouter_kit.account import AccountCheckError, check_account  # noqa: E402
from openrouter_kit.constants import API_KEY_ENV, USER_ENV_FILE  # noqa: E402
from openrouter_kit.env_file import read_env_file, write_env_file  # noqa: E402


# Where loc-ops historically stored the key on this project. Honored once,
# at autodetect time, so existing developers do not have to re-enter the key
# after installing openrouter-kit.
_LEGACY_LOC_OPS_RELATIVE = Path(".local-data") / "loc" / ".env"

# A successful /auth/key validation is cached against the key's hash, but never
# forever: past this age the key is re-validated so revocation or credit
# exhaustion is detected even when the key text has not changed.
_REVALIDATE_AFTER_SECONDS = 7 * 24 * 60 * 60  # ~7 days


def bootstrap(ctx: Any) -> None:
    """Validate or initialize the OpenRouter credential.

    openrouter_kit is published as a shared library to the standalone Python (and
    consuming plugin venvs) declaratively via the ``shared_libs`` key in this
    plugin's bootstrap.json -- the bootstrap engine handles the .pth registration,
    so this script only manages the API credential.
    """
    project_root = _resolve_project_root(ctx)

    # 1. Try the canonical resolution path (env -> project .env -> user .env).
    lookup = get_api_key(project_root)

    # 2. If missing, scan legacy locations and migrate if found.
    if lookup.key is None:
        migrated = _try_legacy_migration(ctx, project_root)
        if migrated:
            lookup = get_api_key(project_root)

    # 3. Still missing -> fix-all entry.
    if lookup.key is None:
        ctx.add_failure(
            "openrouter_credential",
            field=API_KEY_ENV,
            user_msg=(
                "openrouter-kit needs an OpenRouter API key. Ask Claude to "
                "'fix-all' and Claude will walk you through it."
            ),
            agent_msg=(
                "openrouter-kit needs an OpenRouter API key. Give the user "
                "this prepared statement, verbatim:\n\n"
                "  > openrouter-kit needs an API key. Two ways to set it:\n"
                "  >   1. (preferred -- key stays out of the transcript) Type "
                "this in the prompt with the leading `!`:\n"
                "  >        ! openrouter-kit set-key\n"
                "  >      It'll prompt for the key with a hidden input. "
                "Paste from https://openrouter.ai/keys (starts with `sk-or-v1-`).\n"
                "  >   2. If you'd rather paste the key here and have me set "
                "it for you, paste it. WARNING: the key will be visible in "
                "the transcript, so prefer option 1 unless you don't mind.\n\n"
                "If the user picks option 2 and pastes a key, run:\n"
                "  openrouter-kit set-key --key <THE_KEY>\n"
                "It validates against GET /auth/key before writing to "
                f"{USER_ENV_FILE}. Do NOT run `openrouter-kit set-key` "
                "without --key yourself -- it requires an interactive hidden "
                "prompt you cannot provide; it must be the user who runs the "
                "bang-prefixed form."
            ),
        )
        ctx.log("openrouter: no API key found")
        return

    # 4. Have a key. Validate against /auth/key, with content-hash caching.
    # The cache is honored only while fresh (< _REVALIDATE_AFTER_SECONDS); a
    # matching hash with a missing/old timestamp (incl. the legacy hash-only
    # format) re-validates so revocation / credit exhaustion is re-detected.
    cache_file = Path(ctx.data_dir) / "last_validated.sha256"
    key_hash = hashlib.sha256(lookup.key.encode("utf-8")).hexdigest()
    cached_hash, cached_epoch = _read_cache(cache_file)

    if (
        cached_hash == key_hash
        and cached_epoch is not None
        and (time.time() - cached_epoch) < _REVALIDATE_AFTER_SECONDS
    ):
        ctx.log_ok(f"openrouter: key validated (cached, source={lookup.source})")
        return

    try:
        status = check_account(lookup.key)
    except AccountCheckError as e:
        # Network failure / unexpected status. Don't block bootstrap on
        # transient connectivity issues -- log and continue. The next
        # session-start retry validates again because no cache was written.
        ctx.log(f"openrouter: validation skipped ({e})")
        return

    if status.ok:
        _write_cache(cache_file, key_hash)
        label = status.label or "<unlabeled>"
        ctx.log(f"openrouter: key validated ({label}, source={lookup.source})")
        return

    if status.failure_reason == "auth":
        ctx.add_failure(
            "openrouter_credential",
            field=API_KEY_ENV,
            user_msg=(
                "Your OpenRouter API key was rejected (HTTP 401). "
                "Generate a new one at https://openrouter.ai/keys and run "
                "`openrouter-kit set-key`."
            ),
            agent_msg=(
                f"OpenRouter rejected the API key currently in {lookup.source_path or USER_ENV_FILE} "
                f"with HTTP 401. Ask the user to generate a fresh key at "
                f"https://openrouter.ai/keys and run `openrouter-kit set-key`."
            ),
        )
        ctx.log("openrouter: key REJECTED (HTTP 401)")
        return

    if status.failure_reason == "no_credit":
        ctx.add_failure(
            "openrouter_credential",
            field=API_KEY_ENV,
            user_msg=(
                "Your OpenRouter account is out of credit (HTTP 402). "
                "Add credit at https://openrouter.ai/credits."
            ),
            agent_msg=(
                "OpenRouter returned HTTP 402 (insufficient credit). The key "
                "is valid but the account has no balance. Ask the user to "
                "add credit at https://openrouter.ai/credits."
            ),
        )
        ctx.log("openrouter: account OUT OF CREDIT (HTTP 402)")
        return


def _resolve_project_root(ctx: Any) -> Path:
    """Project root for per-project .env lookup.

    ``ctx.project_dir`` is the canonical Claude Code launch CWD (may be None
    for non-project sessions). When None, falls back to the actual CWD.
    """
    if getattr(ctx, "project_dir", None):
        return Path(ctx.project_dir)
    return Path.cwd()


def _try_legacy_migration(ctx: Any, project_root: Path) -> bool:
    """Scan known legacy locations and migrate the key if found.

    Returns True if a key was migrated into USER_ENV_FILE.
    """
    legacy_path = project_root / _LEGACY_LOC_OPS_RELATIVE
    if not legacy_path.is_file():
        return False

    try:
        legacy_values = read_env_file(legacy_path)
    except ValueError:
        # Malformed legacy file -- don't silently corrupt the migration.
        return False

    legacy_key = legacy_values.get(API_KEY_ENV)
    if not legacy_key:
        return False

    # Don't overwrite an existing canonical .env. The canonical lookup
    # already confirmed the user file lacks the key (we got here via
    # lookup.key is None and env var unset), but be defensive.
    existing = read_env_file(USER_ENV_FILE)
    if API_KEY_ENV in existing:
        return False

    write_env_file(USER_ENV_FILE, {API_KEY_ENV: legacy_key})
    ctx.log(
        f"openrouter: migrated key from {legacy_path} -> {USER_ENV_FILE}"
    )
    return True


def _read_cache(path: Path) -> Tuple[Optional[str], Optional[float]]:
    """Return (key_hash, validated_epoch) from the cache file.

    File format: hash on line 1, epoch seconds on line 2. The legacy hash-only
    format yields (hash, None), which callers treat as stale.
    """
    try:
        lines = path.read_text(encoding="utf-8").split()
    except OSError:
        return (None, None)
    if not lines:
        return (None, None)
    epoch: Optional[float] = None
    if len(lines) > 1:
        try:
            epoch = float(lines[1])
        except ValueError:
            epoch = None
    return (lines[0], epoch)


def _write_cache(path: Path, value: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{value}\n{int(time.time())}\n", encoding="utf-8")
    except OSError:
        pass
