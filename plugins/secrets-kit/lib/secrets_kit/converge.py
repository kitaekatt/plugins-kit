"""The idempotent convergence pass: one check, one fix, no third outcome.

Check = hash compare. Fix = decrypt and write. Everything is classified AUTO
(the agent can fix it now) or ASK (only the user can supply it), per the
two-outcome contract; there is no "warning" tier, because a warning about a
credential is just a failure nobody acted on.

The pass is engine-agnostic on purpose. It takes plain paths and returns a
:class:`Result`; ``custom_bootstrap.py`` is the only thing that knows about
``ctx``. That is what lets this whole package fold into the bootstrap engine
later as a file move rather than a rewrite.
"""

import os
from pathlib import Path
from typing import List, Optional

from . import DecryptError, SecretsError, cli_command
from .agefile import decrypt_with_identity
from .manifest import Config, Entry, Manifest
from .perms import open_private, tighten, tighten_dir
from . import repo as repo_mod
from .state import State, sha256_bytes, sha256_file

# Names the bootstrap failure records use. Stable strings: the engine dedupes
# and re-reports on them every session until they clear.
FAILURE_LOCKED = "secrets_locked"
FAILURE_CONFIG = "secrets_config"
FAILURE_ENTRY = "secrets_entry"


class Failure:
    """One thing that went wrong, already classified for the two-outcome model."""

    def __init__(
        self, key: str, user_msg: str, agent_msg: str, ask_reason: Optional[str] = None
    ) -> None:
        self.key = key
        self.user_msg = user_msg
        self.agent_msg = agent_msg
        self.ask_reason = ask_reason


class Result:
    def __init__(self) -> None:
        self.ok = 0
        self.written = 0
        self.removed = 0
        self.failures: List[Failure] = []
        self.notes: List[str] = []
        self.skipped_reason: Optional[str] = None

    @property
    def failed(self) -> int:
        return len(self.failures)

    def summary(self) -> str:
        if self.skipped_reason:
            return f"secrets: {self.skipped_reason}"
        parts = [f"{self.ok} ok", f"{self.written} written"]
        if self.removed:
            parts.append(f"{self.removed} removed")
        parts.append(f"{self.failed} failed")
        return "secrets: " + ", ".join(parts)


def paths_for(data_dir: Path) -> dict:
    return {
        "clone": data_dir / "repo",
        "identity": data_dir / "identity.txt",
        "state": data_dir / "state.json",
        "fetch_stamp": data_dir / "last_fetch",
    }


def converge(
    config_path: Path,
    data_dir: Path,
    *,
    known_machines: Optional[List[str]] = None,
    force_refresh: bool = False,
) -> Result:
    """Run one full pass. Never raises for expected conditions."""
    result = Result()

    config = Config.load(config_path)
    if config is None:
        result.skipped_reason = "not configured"
        return result

    machine_key = config.machine_key()
    if machine_key is None:
        result.skipped_reason = "no profiles for this host"
        return result

    # Cross-check against the engine's machines registry when we were given
    # one. secrets.json must not become a second machine list -- env.json owns
    # that, and a name that exists in only one of them is a typo with
    # consequences, not a new machine.
    if known_machines and machine_key not in known_machines:
        result.failures.append(
            Failure(
                FAILURE_CONFIG,
                user_msg=(
                    f"secrets-kit: this machine is listed in secrets.json as "
                    f"'{machine_key}' but that name is not in the env.json "
                    f"machines registry."
                ),
                agent_msg=(
                    f"secrets.json machine key '{machine_key}' is absent from "
                    f"env.json's machines registry ({', '.join(sorted(known_machines))}). "
                    f"The registry is the single machine list; secrets.json "
                    f"references it. Confirm the machine's identity with the "
                    f"user, then align the two -- do not add a machine to "
                    f"secrets.json that the registry does not know."
                ),
                ask_reason="info",
            )
        )
        return result

    paths = paths_for(data_dir)
    tighten_dir(data_dir)

    # --- repo -------------------------------------------------------------
    if not repo_mod.is_clone(paths["clone"]):
        try:
            repo_mod.clone(config.repo, paths["clone"])
            result.notes.append(f"cloned {config.repo}")
        except SecretsError as e:
            result.failures.append(
                Failure(
                    FAILURE_CONFIG,
                    user_msg=(
                        "secrets-kit could not reach the fleet secrets repo. "
                        "Credentials will not be available on this machine yet."
                    ),
                    agent_msg=(
                        f"Cloning the secrets repo failed.\n{e}\n"
                        f"Diagnose connectivity/SSH auth and retry; this "
                        f"re-runs every session until the clone lands."
                    ),
                )
            )
            return result
    else:
        note = repo_mod.refresh(
            paths["clone"], paths["fetch_stamp"], force=force_refresh
        )
        if note:
            result.notes.append(note)

    # --- not seeded yet ---------------------------------------------------
    # A cloned-but-empty repo is a legitimate, expected state (the repo exists,
    # nobody has run `init`). Reporting it as a broken manifest would send the
    # reader off diagnosing a file that was never supposed to exist yet, so it
    # gets its own message naming the actual next step.
    if not (paths["clone"] / "manifest.json").is_file():
        result.failures.append(
            Failure(
                FAILURE_CONFIG,
                user_msg=(
                    "The fleet secrets repo exists but has never been seeded, "
                    "so there is nothing to materialize yet."
                ),
                agent_msg=(
                    f"{paths['clone']} is a valid clone with no manifest.json: "
                    f"the repo has not been seeded. This is NOT a broken "
                    f"manifest -- do not try to repair or hand-write one.\n\n"
                    "Seeding runs once, on the machine holding the plaintext, "
                    "and needs the user's passphrase. ASK the user whether to "
                    "seed now; if they agree, RUN this yourself:\n\n"
                    f"    {cli_command('init --new-terminal')}\n\n"
                    "It opens a terminal window and returns immediately. The "
                    "user chooses the fleet passphrase and types it twice in "
                    "THAT window, with hidden input -- so it never reaches the "
                    "transcript. Tell them to look for the new window. Do not "
                    "run `init` without --new-terminal: age prompts on a tty "
                    "you do not have, and it will hang or fail.\n\n"
                    f"Afterwards YOU add the entries with "
                    f"`{cli_command('add')}` (public-key encryption, no "
                    f"passphrase needed)."
                ),
                ask_reason="info",
            )
        )
        result.skipped_reason = "repo not seeded yet"
        return result

    # --- manifest ---------------------------------------------------------
    try:
        manifest = Manifest.load(paths["clone"] / "manifest.json")
        variables = config.vars_for(machine_key)
        selected = manifest.select(config.profiles_for(machine_key))
    except SecretsError as e:
        result.failures.append(
            Failure(
                FAILURE_CONFIG,
                user_msg="secrets-kit found a problem in the secrets manifest.",
                agent_msg=(
                    f"The secrets manifest or secrets.json is invalid.\n{e}\n"
                    f"Manifest edits are unattended-safe; fix the file and the "
                    f"next pass converges."
                ),
            )
        )
        return result

    # --- identity ---------------------------------------------------------
    # Nothing below this line is actionable while locked, so this returns
    # rather than accumulating per-entry noise the user cannot act on.
    if not paths["identity"].is_file():
        result.failures.append(
            Failure(
                FAILURE_LOCKED,
                user_msg=(
                    "secrets-kit needs your fleet secrets passphrase to unlock "
                    "this machine (one time only)."
                ),
                agent_msg=(
                    "This machine has no unlocked secrets identity, so no "
                    "credential can be materialized. ASK the user whether to "
                    "unlock now; if they agree, RUN this yourself:\n\n"
                    f"    {cli_command('unlock --new-terminal')}\n\n"
                    "It opens a terminal window and returns immediately. The "
                    "user types their fleet passphrase in THAT window, with "
                    "hidden input. Tell them to look for the new window; "
                    "nothing is materialized until it succeeds.\n\n"
                    "Do NOT run `unlock` without --new-terminal -- age prompts "
                    "on a tty you do not have, so it will hang or fail. Do NOT "
                    "ask the user to paste the passphrase into the chat under "
                    "any framing: the transcript is written to disk, and this "
                    "is a master passphrase, not an API key. There is "
                    "deliberately no paste-it-here fallback."
                ),
                ask_reason="info",
            )
        )
        result.skipped_reason = "locked (awaiting one-time unlock)"
        return result

    # --- entries ----------------------------------------------------------
    state = State.load(paths["state"])
    selected_names = {entry.name for entry in selected}

    for entry in selected:
        try:
            _converge_entry(entry, paths, variables, state, result)
        except SecretsError as e:
            result.failures.append(_entry_failure(entry, e))
        except OSError as e:
            # One unwritable destination must not abort the pass: the other
            # entries are still materializable, and an exception escaping here
            # would propagate into the engine and fail the whole session's
            # bootstrap over a single disk/permission fault.
            result.failures.append(
                _entry_failure(entry, SecretsError(f"{type(e).__name__}: {e}"))
            )

    # --- orphans ----------------------------------------------------------
    # An entry removed upstream, or dropped from this machine's profiles, must
    # stop existing here too -- otherwise "remove a secret" is a no-op on every
    # machine that already had it, which is the opposite of what it means.
    for name in [n for n in state.rows if n not in selected_names]:
        row = state.get(name)
        dest_raw = row.get("dest")
        if dest_raw:
            try:
                Path(dest_raw).unlink()
                result.removed += 1
            except OSError:
                pass
        state.forget(name)

    state.save()
    return result


def _converge_entry(
    entry: Entry,
    paths: dict,
    variables: dict,
    state: State,
    result: Result,
) -> None:
    dest = entry.dest(variables)
    blob_path = paths["clone"] / entry.blob
    blob_sha = sha256_file(blob_path)
    if blob_sha is None:
        raise SecretsError(
            f"blob {entry.blob} is missing from the secrets repo",
            "The manifest names a blob that is not present. Either the repo "
            "is mid-rotation or the entry was added without its ciphertext.",
        )

    row = state.get(entry.name)
    dest_sha = sha256_file(dest)
    recorded_mode = row.get("mode")

    unchanged = (
        row.get("blob_sha256") == blob_sha
        and dest_sha is not None
        and dest_sha == row.get("dest_sha256")
    )
    if unchanged:
        # Cheap repair path: content is right, only the mode drifted.
        if recorded_mode != format(entry.mode, "04o"):
            tighten(dest, entry.mode)
            state.record(
                entry.name,
                blob_sha=blob_sha,
                dest_sha=dest_sha,
                mode=entry.mode,
                dest=str(dest),
            )
            result.written += 1
            return
        result.ok += 1
        return

    if not dest.parent.is_dir():
        raise SecretsError(
            f"destination directory {dest.parent} does not exist",
            "This is usually a repo that has not been cloned on this machine "
            "yet; repo-sync creates it and the next pass completes. If the "
            "path is simply wrong, fix 'dest' or the machine's vars in "
            "secrets.json.",
        )

    plaintext = decrypt_with_identity(paths["identity"], blob_path)

    if entry.newline == "lf" and b"\r\n" in plaintext:
        raise SecretsError(
            f"entry '{entry.name}' is declared newline: lf but its plaintext "
            f"contains CRLF",
            "This asserts a bad SEED rather than a bad materialization -- the "
            "file was encrypted with Windows line endings and would break the "
            "consumer (ssh keys and tokens are the usual victims). Re-add the "
            "entry from a LF copy.",
        )

    _atomic_write(dest, plaintext, entry.mode)
    state.record(
        entry.name,
        blob_sha=blob_sha,
        dest_sha=sha256_bytes(plaintext),
        mode=entry.mode,
        dest=str(dest),
    )
    result.written += 1


def _atomic_write(dest: Path, data: bytes, mode: int) -> None:
    """Write ``data`` to ``dest`` with no window at a loose mode and no torn file.

    Order matters and is the point: create the temp file in the SAME directory
    already at its final mode, write, fsync, tighten (Windows ACL), then
    rename. A crash at any point leaves either the old file or nothing -- never
    a half-written credential, and never plaintext at the default umask.
    """
    tmp = dest.with_name(f"{dest.name}.tmp-{os.getpid()}")
    fd = open_private(tmp, mode)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        tighten(tmp, mode)
        os.replace(tmp, dest)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _entry_failure(entry: Entry, error: SecretsError) -> Failure:
    if isinstance(error, DecryptError):
        return Failure(
            FAILURE_LOCKED,
            user_msg=(
                "secrets-kit could not decrypt your fleet secrets. The fleet "
                "identity was probably rotated; unlocking again fixes it."
            ),
            agent_msg=(
                f"Entry '{entry.name}' could not be decrypted with the cached "
                f"identity.\n{error}\n"
                "The usual cause is an identity rotation upstream: the new "
                "identity.age arrived with the fetch, but this machine still "
                "holds the old unlocked identity. Ask the user, then run "
                f"`{cli_command('unlock --new-terminal')}` -- they answer the "
                "passphrase prompt in the window it opens. Never request the "
                "passphrase in chat."
            ),
            ask_reason="info",
        )
    return Failure(
        FAILURE_ENTRY,
        user_msg=f"secrets-kit could not materialize '{entry.name}'.",
        agent_msg=(
            f"Entry '{entry.name}' failed to materialize.\n{error}\n"
            f"Diagnose and fix; this re-runs every session until it clears."
        ),
    )
