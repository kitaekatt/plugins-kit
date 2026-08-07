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
FAILURE_DEST = "secrets_dest"


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
        # Confirm against the remote before saying so. This message asks the
        # agent to seed, and seeding is irreversible -- so it must never be
        # emitted on the strength of a checkout that last fetched before
        # someone else seeded the repo. The forced fetch costs one round trip
        # in a state that is rare and terminal anyway.
        note = repo_mod.refresh(paths["clone"], paths["fetch_stamp"], force=True)
        if note:
            result.notes.append(note)

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
                    "If `init` reports that the repo is ALREADY seeded, believe "
                    "it and stop: it asks the remote, this check only sees the "
                    "checkout. Run the `unlock` it names instead.\n\n"
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
    # Deliberately ABOVE the `unchanged` fast path, and therefore paying a git
    # query on every pass for every entry that lives inside a repo at all. The
    # steady state is otherwise free, and that was the argument for checking
    # only when about to write -- but it made the guard go silent in exactly
    # the scenario it exists for: a .gitignore edited AFTER the entry
    # converged. An exposed credential is on disk being staged by every
    # `git add -A`, and withholding some future write does nothing about it.
    # Visibility beats a zero-cost steady state when what is being made
    # visible cannot be undone. (Cost is bounded: the override skips the check
    # outright, and a dest in no repo costs one `rev-parse`.)
    if not _dest_is_writable_here(entry, dest, result, already_present=dest_sha is not None):
        return

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


def _dest_is_writable_here(
    entry: Entry, dest: Path, result: Result, *, already_present: bool
) -> bool:
    """False when ``dest`` sits unignored inside someone's git working tree.

    This re-check is not redundant with the one `add` performs. `add` can only
    validate the AUTHORING machine's answer, and the answer is per-machine in
    two ways: the variables in a dest resolve differently on every box, and
    ``dest_spec`` may be a per-OS object, so a destination that is gitignored
    where it was added can be tracked where it lands. It also covers the case
    no add-time check can: a ``.gitignore`` that changes after the entry was
    created.

    There is no warning tier here by design (see the module docstring), and
    "warn but write it anyway" would leave the credential in the tracked tree
    exactly as if nothing had checked. The other entries still converge; only
    this one is skipped.

    ``already_present`` selects the message, and getting it right matters: when
    the plaintext is ALREADY at the exposed path, "the write was withheld" is
    false and the remedy is different -- the file has to come out of the index,
    not merely stay out of it. Either way nothing here writes, tightens, or
    deletes: removing a file the user may be relying on, over a policy
    violation, would be a second unasked-for act on top of the first.
    """
    if entry.allow_tracked_dest:
        return True

    exposure = repo_mod.dest_exposure(dest)
    if exposure.undetermined:
        # Fail OPEN, deliberately: a machine that only CONSUMES secrets may
        # have no git at all, and failing closed would make the guard a new way
        # for a working machine to stop working.
        #
        # But the two causes are not the same event, so they must not read the
        # same. git being absent is systemic and expected; git being present
        # and answering something we cannot parse means the guard is silently
        # not guarding, and the raw output is the only diagnostic anyone gets.
        # Both go to result.notes, which custom_bootstrap.py forwards through
        # ctx.log -- the ALWAYS-shown channel (ctx.log_ok is the verbose-only
        # one), so no new tier is needed to make the anomaly visible.
        result.notes.append(_undetermined_note(entry, dest, exposure))
        return True
    if not exposure.exposed:
        return True

    # Rendered posix, like every other command this package prints: these
    # strings get pasted into a shell, and a mixed "C:\dev\repo/.gitignore" is
    # both ugly and, in the `git -C` line, needlessly fragile.
    shown = dest.as_posix()
    consent = (
        f"If the destination is deliberate, re-add the entry with "
        f"`{cli_command('add')} {entry.name} ... --allow-tracked-dest`, which "
        f"records the consent in the manifest for every machine. Do NOT work "
        f"around this by moving or deleting the file by hand -- the next pass "
        f"would put it back."
    )

    # Without a verified repo root there is no correct `git -C <root>` to
    # print. Say so in prose instead of interpolating a placeholder into a
    # command: bad remediation in a security tool is worse than none, because
    # the user pastes it, watches it fail, and learns to distrust the whole
    # message. (Reachable whenever _toplevel refuses to trust git's answer.)
    if exposure.toplevel:
        where = exposure.toplevel.as_posix()
        fix = exposure.gitignore_line or "(the dest path, repo-root-relative)"
        rel = exposure.repo_relative or dest.name
        located = f"inside the git repository at {where}"
        located_agent = f"inside the git working tree at {where}"
        ignore_step = f"Add this line to {where}/.gitignore and commit it:\n    {fix}"
        ignore_step_agent = f"Add this line to {where}/.gitignore and commit it --\n        {fix}"
        untrack_step = (
            f"If the file is already tracked, also take it out of the index:\n"
            f"    git -C {where} rm --cached -- {rel}"
        )
        untrack_step_agent = (
            f"If the path is already tracked, remove it from the index "
            f"(this keeps the file on disk) --\n"
            f"        git -C {where} rm --cached -- {rel}"
        )
    else:
        located = "inside a git repository whose root could not be determined"
        located_agent = "inside a git working tree whose root could not be determined"
        ignore_step = (
            "Find the repository that contains that path, add an ignore rule "
            "covering it to that repository's .gitignore, and commit the change."
        )
        ignore_step_agent = ignore_step
        untrack_step = (
            "If the file is already tracked there, untrack it as well "
            "(`git rm --cached`), which leaves it on disk."
        )
        untrack_step_agent = untrack_step

    if already_present:
        user_msg = (
            f"secrets-kit: '{entry.name}' is already materialized at {shown}, "
            f"which is {located} and is NOT gitignored. Nothing further was "
            f"written. {ignore_step}\n{untrack_step}"
        )
        agent_msg = (
            f"Entry '{entry.name}' resolves to {shown}, which is {located_agent} "
            f"and is NOT ignored -- and the plaintext is ALREADY on disk there. "
            f"This is not a withheld write; the credential is exposed right "
            f"now, and every `git add -A` in that repo stages it. A credential "
            f"pushed once survives in the object store, in every clone, and in "
            f"any fork taken meanwhile; rewriting history does not undo it.\n\n"
            f"Fix, in order:\n"
            f"  1. {ignore_step_agent}\n"
            f"  2. {untrack_step_agent}\n"
            f"  3. If it was ever COMMITTED, the value is compromised: rotate "
            f"the underlying credential. Deleting it from the tree is not "
            f"revocation.\n\n"
            f"Confirm with the user before editing another repository's "
            f".gitignore or index. This pass wrote nothing and removed "
            f"nothing.\n\n{consent}"
        )
    else:
        user_msg = (
            f"secrets-kit did NOT write '{entry.name}': its destination {shown} "
            f"is {located} and is NOT gitignored. {ignore_step}"
        )
        agent_msg = (
            f"Entry '{entry.name}' resolves to {shown}, which is {located_agent} "
            f"and is NOT ignored. The write was WITHHELD: a convergence pass "
            f"rewrites that file every session, so a routine `git add -A` would "
            f"stage a plaintext credential, and a credential pushed once "
            f"survives in the object store, in every clone, and in any fork "
            f"taken meanwhile.\n\n"
            f"Fix: {ignore_step_agent}\n"
            f"-- then the next pass materializes the secret. Confirm with the "
            f"user before editing another repository's .gitignore.\n\n{consent}"
        )

    result.failures.append(
        Failure(FAILURE_DEST, user_msg=user_msg, agent_msg=agent_msg, ask_reason="info")
    )
    return False


def _undetermined_note(entry: Entry, dest: Path, exposure) -> str:
    """One line for the always-shown log, worded by how alarming the cause is.

    The systemic case (no git) is a standing fact about the machine and reads
    as one. The anomaly gets named as an anomaly and carries git's raw output,
    which is the only diagnostic that will ever exist for it -- the state is
    not reproducible after the fact.
    """
    detail = exposure.detail or "no output"
    if exposure.anomalous:
        return (
            f"ANOMALY: git could not answer whether {dest.as_posix()} is inside "
            f"a working tree, so the tracked-tree guard did NOT run for "
            f"'{entry.name}' and the secret was materialized anyway. Check by "
            f"hand that the destination is not inside an unignored git "
            f"repository. git said: {detail}"
        )
    return (
        f"git is unavailable, so the tracked-tree check was skipped for "
        f"'{entry.name}' ({detail})"
    )


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
