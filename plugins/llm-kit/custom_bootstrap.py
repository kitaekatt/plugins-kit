"""Bootstrap script for llm-kit.

Single entry point ``bootstrap(ctx)`` runs at session start and checks the
OpenRouter credential:

1. Asks the plugin venv's ``llm-kit account status --json`` (the console script
   ``katsoft-llm-kit`` installs there) for the credential state. The script
   runs inside the bootstrap engine's process, so it imports no ``llm_kit``
   module: the library is the installed ``katsoft-llm-kit`` package in the
   plugin venv, and the check runs there as a subprocess. The library owns the
   credential lookup (the environment variable, the project ``.env``, then the
   user key file, which is ``user_env_file`` in the document: ``$LLM_KIT_ENV_FILE``,
   else ``$XDG_CONFIG_HOME/llm-kit/.env``, else the legacy file when the neutral
   one does not exist); this script reads the outcome through the CLI and seeds
   none.
2. If the key is missing (``state == "missing_key"``), records a DEFERRED
   REQUIREMENT -- never a fix-all entry. An OpenRouter key is a precondition
   only some sessions need, so bootstrap does not ask for it at session start;
   the skill that needs the key preflights and asks at the point of need. See
   the bootstrap skill's deferred-requirement pattern.
3. If present, validates the key against ``GET /auth/key`` (a second CLI call
   without ``--no-validate``). ``rejected_key`` (401) and ``no_credit`` (402)
   are deferred the same way, with specific remediation. Successful checks are
   content-hashed into ``last_validated.sha256`` (hash + validation timestamp)
   under ``ctx.data_dir`` so subsequent sessions skip the network round-trip
   when the key has not changed -- but only for about 7 days, so revocation or
   credit exhaustion is re-detected without a key change. The hash is the
   document's ``key_sha256``: the key itself never leaves the subprocess.

The state is read from the JSON document's ``state`` field only. A missing
console script, a subprocess that cannot run, a nonzero exit, output other than
the document, or a state this script does not know raises, and the engine
reports the script as FAILED.

The script is stdlib-only.
"""

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

# A successful /auth/key validation is cached against the key's hash, but never
# forever: past this age the key is re-validated so revocation or credit
# exhaustion is detected even when the key text has not changed.
_REVALIDATE_AFTER_SECONDS = 7 * 24 * 60 * 60  # ~7 days

# The endpoint whose key this plugin's bootstrap checks, and the schema of the
# document `llm-kit account status --json` prints for it.
_ENDPOINT = "openrouter"
_STATUS_SCHEMA = "llm-kit.account-status/v1"

# One status call makes at most one network request of its own (10 s socket
# timeout in the library); this bounds a wedged process, not the request.
_STATUS_TIMEOUT_SECONDS = 60

_STATES = (
    "missing_key",
    "key_present",
    "not_validated",
    "ok",
    "rejected_key",
    "no_credit",
    "check_failed",
)


def _venv_cli(data_dir: Any) -> Path:
    """The ``llm-kit`` console script in the plugin venv: ``<data_dir>/.venv`` (Windows and POSIX layouts).

    ``katsoft-llm-kit`` installs it, so its presence also shows the library is installed.
    """
    venv = Path(data_dir) / ".venv"
    windows = venv / "Scripts" / "llm-kit.exe"
    return windows if windows.is_file() else venv / "bin" / "llm-kit"


def _account_status(ctx: Any, project_root: Path, *, validate: bool) -> Dict[str, Any]:
    """Run ``llm-kit account status --json`` from the plugin venv; return its document.

    Raises ``RuntimeError`` when the console script is missing, the call fails or
    times out, or the output is not a status document with a known state. The
    document is the only thing read: prose and exit codes are not interpreted.
    """
    cli = _venv_cli(ctx.data_dir)
    if not cli.is_file():
        raise RuntimeError(
            f"openrouter credential check needs the plugin venv's llm-kit console script at {cli}, "
            "which is not provisioned"
        )
    argv = [
        str(cli), "account", "status", "--json",
        "--endpoint", _ENDPOINT, "--project-root", str(project_root),
    ]
    if not validate:
        argv.append("--no-validate")
    try:
        done = subprocess.run(
            argv, capture_output=True, text=True, timeout=_STATUS_TIMEOUT_SECONDS, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"openrouter credential check timed out after {_STATUS_TIMEOUT_SECONDS}s: {' '.join(argv)}"
        ) from exc
    if done.returncode != 0:
        raise RuntimeError(
            f"openrouter credential check exited {done.returncode}: "
            f"{(done.stderr or done.stdout).strip()[-500:]}"
        )
    try:
        document = json.loads(done.stdout)
    except ValueError as exc:
        raise RuntimeError(
            f"openrouter credential check printed no JSON document: {done.stdout.strip()[-500:]!r}"
        ) from exc
    if not isinstance(document, dict) or document.get("schema") != _STATUS_SCHEMA:
        raise RuntimeError(
            f"openrouter credential check printed a document that is not {_STATUS_SCHEMA}: "
            f"{done.stdout.strip()[-500:]!r}"
        )
    if document.get("state") not in _STATES:
        raise RuntimeError(
            f"openrouter credential check reported an unknown state: {document.get('state')!r}"
        )
    return document


def _defer(ctx: Any, document: Dict[str, Any], *, user_msg: str, agent_msg: str) -> None:
    """Record the credential requirement without escalating it.

    An OpenRouter key is a precondition only SOME sessions need -- most never
    call the API at all -- so bootstrap does not ask for it. It records the
    requirement and the skill that needs the key asks at the point of need
    (see the bootstrap skill's deferred-requirement pattern).

    Guarded on the engine actually providing the API: on a bootstrap older
    than 0.71.0 the requirement is simply dropped. Falling back to add_failure
    there would resurrect the session-start nag this exists to remove.
    """
    defer = getattr(ctx, "add_deferred_requirement", None)
    if defer is None:
        return
    defer(
        "openrouter_credential",
        user_msg=user_msg,
        agent_msg=agent_msg,
        satisfied_by=document["set_key_command"],
    )


def bootstrap(ctx: Any) -> None:
    """Validate the OpenRouter credential.

    ``llm_kit`` reaches other plugins as the installed ``katsoft-llm-kit``
    package (and, until that retirement, as the ``shared_libs`` publication in
    this plugin's bootstrap.json); this script only checks the API credential,
    through the CLI in the plugin venv.
    """
    project_root = _resolve_project_root(ctx)

    # 1. The canonical resolution path (env -> project .env -> user .env), with
    # no network request: is there a key at all, and which one (by hash)?
    lookup = _account_status(ctx, project_root, validate=False)
    set_key = lookup["set_key_command"]
    user_env_file = lookup["user_env_file"]

    # 2. Missing -> deferred requirement, NOT a fix-all entry.
    if lookup["state"] == "missing_key":
        _defer(
            ctx,
            lookup,
            user_msg=(
                "llm-kit has no OpenRouter API key yet. Nothing to do "
                "until you run something that calls the API -- it will ask then."
            ),
            agent_msg=(
                "llm-kit needs an OpenRouter API key. Give the user "
                "this prepared statement, verbatim:\n\n"
                "  > llm-kit needs an API key. Two ways to set it:\n"
                "  >   1. (preferred -- key stays out of the transcript) Type "
                "this in the prompt with the leading `!`:\n"
                f"  >        ! {set_key}\n"
                "  >      It'll prompt for the key with a hidden input. "
                "Paste from https://openrouter.ai/keys (starts with `sk-or-v1-`).\n"
                "  >   2. If you'd rather paste the key here and have me set "
                "it for you, paste it. WARNING: the key will be visible in "
                "the transcript, so prefer option 1 unless you don't mind.\n\n"
                "If the user picks option 2 and pastes a key, run:\n"
                f"  {set_key} --key <THE_KEY>\n"
                "It validates against GET /auth/key before writing to "
                f"{user_env_file}. Do NOT run `{set_key}` "
                "without --key yourself -- it requires an interactive hidden "
                "prompt you cannot provide; it must be the user who runs the "
                "bang-prefixed form."
            ),
        )
        # log_ok, not log: a missing key is the expected state on a machine
        # that has never called the API, so it belongs in verbose output only.
        ctx.log_ok("openrouter: no API key (deferred until a capability needs it)")
        return

    # 3. Have a key. Validate against /auth/key, with content-hash caching.
    # The cache is honored only while fresh (< _REVALIDATE_AFTER_SECONDS); a
    # matching hash with a missing/old timestamp (incl. a hash-only file)
    # re-validates so revocation / credit exhaustion is re-detected.
    cache_file = Path(ctx.data_dir) / "last_validated.sha256"
    key_hash = lookup["key_sha256"]
    cached_hash, cached_epoch = _read_cache(cache_file)

    if (
        cached_hash == key_hash
        and cached_epoch is not None
        and (time.time() - cached_epoch) < _REVALIDATE_AFTER_SECONDS
    ):
        ctx.log_ok(f"openrouter: key validated (cached, source={lookup['source']})")
        return

    status = _account_status(ctx, project_root, validate=True)
    state = status["state"]
    source = status["source"]

    if state == "check_failed":
        # Network failure / unexpected status. Don't block bootstrap on
        # transient connectivity issues -- log and continue. The next
        # session-start retry validates again because no cache was written.
        ctx.log(f"openrouter: validation skipped ({status['error']})")
        return

    if state == "ok":
        _write_cache(ctx, cache_file, status["key_sha256"])
        label = status["label"] or "<unlabeled>"
        ctx.log(f"openrouter: key validated ({label}, source={source})")
        return

    if state == "not_validated":
        # The endpoint declares no account check, so there is nothing to
        # cache and nothing to defer.
        ctx.log_ok(f"openrouter: key present, endpoint declares no account check (source={source})")
        return

    if state == "rejected_key":
        _defer(
            ctx,
            status,
            user_msg=(
                "Your OpenRouter API key was rejected (HTTP 401). "
                "Generate a new one at https://openrouter.ai/keys and run "
                f"`{set_key}`."
            ),
            agent_msg=(
                f"OpenRouter rejected the API key currently in {status['source_path'] or user_env_file} "
                f"with HTTP 401. Ask the user to generate a fresh key at "
                f"https://openrouter.ai/keys and run `{set_key}`."
            ),
        )
        ctx.log_ok("openrouter: key REJECTED (HTTP 401) -- deferred to point of use")
        return

    if state == "no_credit":
        _defer(
            ctx,
            status,
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
        ctx.log_ok("openrouter: account OUT OF CREDIT (HTTP 402) -- deferred to point of use")
        return

    raise RuntimeError(f"openrouter credential check reported state {state!r} after a validated call")


def _resolve_project_root(ctx: Any) -> Path:
    """Project root for per-project .env lookup.

    ``ctx.project_dir`` is the canonical Claude Code launch CWD (may be None
    for non-project sessions). When None, falls back to the actual CWD.
    """
    if getattr(ctx, "project_dir", None):
        return Path(ctx.project_dir)
    return Path.cwd()


def _read_cache(path: Path) -> Tuple[Optional[str], Optional[float]]:
    """Return (key_hash, validated_epoch) from the cache file.

    File format: hash on line 1, epoch seconds on line 2. A hash-only file
    yields (hash, None), which callers treat as stale.
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


def _write_cache(ctx: Any, path: Path, value: str) -> None:
    """Write the validation cache; an unwritable cache only costs a re-check, so say so."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{value}\n{int(time.time())}\n", encoding="utf-8")
    except OSError as e:
        ctx.log(f"openrouter: validation cache not written ({e})")
