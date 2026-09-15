"""secrets-kit CLI -- the authoring and unlock verbs.

Split of responsibility with the session pass: the PASS consumes (decrypt what
this machine should hold), the CLI authors (create the identity, add and
rotate secrets) and unlocks. Only three verbs ever need the passphrase --
``init``, ``unlock``, ``rotate-identity`` -- and all three are interactive by
construction, because age prompts on the terminal itself. The agent cannot run
them, and that is a feature: the passphrase has no path into a transcript.

Stdlib-only; adds the sibling lib/ to sys.path exactly like the bootstrap
script does.
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PLUGIN_ROOT / "lib"))

from secrets_kit import SecretsError, cli_command  # noqa: E402
from secrets_kit import agefile  # noqa: E402
from secrets_kit import guard  # noqa: E402
from secrets_kit.authoring import (  # noqa: E402
    AuthoringOperation,
    AuthoringRecoveryError,
    _prepare_entry,
    _prepare_rotation,
    _prepare_seed,
)
from secrets_kit import repo as repo_mod  # noqa: E402
from secrets_kit.converge import converge, paths_for  # noqa: E402
from secrets_kit.manifest import Config, Manifest, resolve_dest  # noqa: E402
from secrets_kit.perms import tighten_dir  # noqa: E402
from secrets_kit.operation_lock import operation_lock  # noqa: E402
from secrets_kit.terminal import relaunch_self  # noqa: E402

CONFIG_PATH = Path.home() / ".claude" / "secrets.json"

# Mirrors what the engine hands the bootstrap script as ctx.data_dir. Hardcoded
# here (rather than discovered) because the CLI runs outside any engine pass;
# both must agree or unlock would write where the pass does not look.
DATA_DIR = (
    Path.home()
    / ".claude"
    / "plugins"
    / "data"
    / "plugins-kit"
    / "secrets-kit"
)


def _fail(message: str) -> int:
    print(f"secrets-kit: {message}", file=sys.stderr)
    return 1


# The verbs age prompts for a passphrase on. They need a tty, so an agent runs
# them with --new-terminal and the user answers in the window that opens.
_INTERACTIVE_VERBS = ("unlock", "init", "rotate-identity")


def _handoff_to_terminal(args: argparse.Namespace) -> Optional[int]:
    """Spawn a terminal for this verb and return, or None to run inline.

    Sits in front of every interactive verb so the flag behaves identically on
    all three: the agent never has to know which of them needs special casing.
    """
    if not getattr(args, "new_terminal", False):
        return None
    extra = ["--force"] if getattr(args, "force", False) else []
    where = relaunch_self(args.command, extra)
    print(f"opened {where} for `secrets-kit {args.command}`.")
    print(
        "The passphrase prompt is in THAT window -- it is hidden input, and "
        "nothing here can see it."
    )
    return 0


def _require_config() -> Config:
    config = Config.load(CONFIG_PATH)
    if config is None:
        raise SecretsError(
            f"no configuration at {CONFIG_PATH}",
            "secrets-kit needs a secrets.json declaring the repo URL and this "
            "machine's profiles before it can do anything.",
        )
    return config


def _ensure_clone(config: Config, *, data_dir: Path, sync: bool = False) -> Path:
    """Use canonical data paths under caller-held operation ownership."""
    paths = paths_for(data_dir)
    clone = paths["clone"]
    if not repo_mod.is_clone(clone):
        print(f"cloning {config.repo} ...")
        if sync:
            repo_mod._clone_for_authoring(config.repo, clone)
        else:
            repo_mod.clone(config.repo, clone)
        return clone
    repo_mod.require_repo_binding(clone, config.repo)
    if sync:
        print("syncing with the remote ...")
        repo_mod.sync(clone)
    return clone


def _ensure_guarded(config: Config, *, data_dir: Path) -> Path:
    """Sync and guard under caller-held whole-operation ownership.

    Nested repo/key/state/publication helpers never acquire recursively.

    Every authoring verb records its recovery baseline before synchronizing,
    so each prepares its own operation rather than calling this. Two things
    have to be true before we let git record anything permanently, and neither
    is inheritable:

    - The clone must be level with the remote. The session pass fetches at most
      once every few hours, so the working tree an authoring verb would read
      its decisions from is routinely hours stale -- and "is this repo seeded?"
      answered about the past is how a second fleet identity gets generated.
    - The pre-commit guard must exist. It lives in ``.git/hooks``, which is
      untracked, so it has to be re-established locally every time.
    """
    clone = _ensure_clone(config, data_dir=data_dir, sync=True)
    note = guard.require_guard(clone)
    if note:
        print(f"pre-commit guard: {note}")
    return clone


# --------------------------------------------------------------------------
# unlock
# --------------------------------------------------------------------------

def cmd_unlock(args: argparse.Namespace) -> int:
    """Decrypt the fleet identity onto this machine. Once per machine, ever."""
    handed_off = _handoff_to_terminal(args)
    if handed_off is not None:
        return handed_off
    config = _require_config()
    with operation_lock(DATA_DIR) as data_dir:
        clone = _ensure_clone(config, data_dir=data_dir)
        # Best-effort, unlike the authoring verbs: unlock only READS the repo, so a
        # stale clone that already holds identity.age is perfectly unlockable
        # offline. But a clone last fetched before the repo was seeded would
        # otherwise report "never seeded" at the one moment the user is trying to
        # act on the seeding that already happened.
        if repo_mod.is_clone(clone):
            try:
                repo_mod.sync(clone)
            except SecretsError as e:
                print(f"note: could not sync the secrets clone ({e.message}); "
                      f"continuing on the existing checkout")

        wrapped = clone / "identity.age"
        if not wrapped.is_file():
            return _fail(
                f"no identity.age in the secrets repo ({wrapped}). "
                f"Has the repo been seeded yet? Run `{cli_command('init')}` on "
                f"the machine holding the plaintext."
            )

        paths = paths_for(data_dir)
        tighten_dir(data_dir)

        print("Enter your fleet secrets passphrase (input is hidden).")
        code = agefile.unwrap_identity(wrapped, paths["identity"])
        if code != 0:
            return _fail("incorrect passphrase (or age failed); identity cache was not replaced")

        print(
            "unlocked. Secrets will materialize on the next bootstrap pass -- "
            "restart Claude Code, or just continue: the failing check re-runs "
            "every session."
        )
        return 0


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------

def cmd_status(args: argparse.Namespace) -> int:
    """Report what this machine holds and what it is waiting on."""
    result = converge(CONFIG_PATH, DATA_DIR, force_refresh=args.refresh)
    print(result.summary())
    for note in result.notes:
        print(f"  note: {note}")
    for failure in result.failures:
        print(f"  {failure.key}: {failure.user_msg}")
    return 1 if result.failures else 0


# --------------------------------------------------------------------------
# init (seeding -- the one-time birth event)
# --------------------------------------------------------------------------

def cmd_init(args: argparse.Namespace) -> int:
    """Create the fleet identity and an empty manifest in the secrets repo.

    Run ONCE, on the machine holding the plaintext. Generates the keypair,
    passphrase-wraps the private half into the repo, records the public half in
    manifest.json, and caches the unlocked identity locally so the seeding
    machine is immediately usable.
    """
    handed_off = _handoff_to_terminal(args)
    if handed_off is not None:
        return handed_off
    config = _require_config()
    with operation_lock(DATA_DIR) as data_dir:
        clone = paths_for(data_dir)["clone"]
        try:
            operation = _prepare_seed(data_dir, clone, config.repo, force=args.force)
        except (repo_mod.RepoBindingError, AuthoringRecoveryError):
            raise
        except SecretsError as error:
            details = str(error)
            recovery = getattr(error, "authoring_recovery_error", None)
            if recovery:
                details += f"\n{recovery}"
            elif repo_mod.is_clone(clone) and repo_mod.remote_has(clone, "identity.age"):
                details += (
                    "\nThe cached remote-tracking view contains identity.age; it "
                    "does not prove why admission failed or that local history is "
                    f"disposable. To use that identity, run `{cli_command('unlock --new-terminal')}`."
                )
            return _fail(details)
        try:
            if not args.force and (repo_mod.remote_has(clone, "identity.age") or (clone / "identity.age").exists()):
                error = SecretsError(
                    "this repo is already seeded -- identity.age exists. Re-running init "
                    "would abandon the existing fleet identity. To use it, run "
                    f"`{cli_command('unlock --new-terminal')}`. Use rotate-identity to "
                    "keep the existing secrets. Pass --force only to abandon them."
                )
                operation.failure(error)
                recovery = getattr(error, "authoring_recovery_error", None)
                return _fail(f"{error}\n{recovery}" if recovery else str(error))
            print("Generating and passphrase-wrapping the fleet age identity ...")
            print("Choose a strong passphrase and save it in your password manager or other secure escrow.\n"
                  "Enter it in age's hidden-input prompt; do not put it in an agent transcript.")
            identity_text, recipient, code = operation.prepare()
            if code != 0:
                raise SecretsError("age failed to wrap the identity; seed was not submitted")
            try:
                operation.apply_and_publish(identity_text)
            except SecretsError as primary:
                phase = operation.record["phase"]
                operation.failure(primary)
                recovery = getattr(primary, "authoring_recovery_error", None)
                details = f"{primary}\n{recovery}" if recovery else str(primary)
                if phase == "definitely_rejected" and not recovery:
                    details += "\nOwned entry state restored; the generated identity was discarded."
                return _fail(details)
        except BaseException as primary:
            operation.failure(primary)
            raise

        print(f"\nseeded. recipient = {recipient}")
        print(f"Add secrets with: {cli_command('add')} <name> --file <path> --dest <dest>")
        return 0


# --------------------------------------------------------------------------
# add / remove
# --------------------------------------------------------------------------

def _refuse_exposed_dest(
    config: Config,
    name: str,
    dest_spec,
    allowed: bool,
    *,
    consent_dropped: bool = False,
) -> Optional[int]:
    """Refuse a destination that would drop plaintext into a tracked repo.

    Returns an exit code to stop on, or None to proceed. The secrets repo's own
    pre-commit guard protects the blobs; it cannot protect a CONSUMER repo the
    destination points into, and that is the one path here that can push a
    credential into public history.

    Resolution reuses the manifest's own resolver, so what gets checked is
    exactly what the convergence pass will later write to.

    A dest that does not resolve HERE is not an error. This is a multi-machine
    fleet: a dest may deliberately name a variable only the target machine
    declares, or be a per-OS object whose windows branch is unresolvable on a
    Mac. Authoring such an entry must not be blocked by whichever machine the
    author happens to be sitting at -- ``Entry.dest()`` still raises at
    convergence, on the machine where the path actually matters. Same posture
    as git being unavailable: cannot determine is not the same as unsafe.
    """
    if allowed:
        return None

    machine_key = config.machine_key()
    variables = config.vars_for(machine_key) if machine_key else dict(config.vars)
    try:
        resolved = resolve_dest(name, dest_spec, variables)
    except SecretsError as e:
        print(
            f"secrets-kit: note: this dest does not resolve on this machine "
            f"({e.message}), so the tracked-tree check was skipped. It is "
            f"re-checked at convergence on every machine that holds the entry.",
            file=sys.stderr,
        )
        return None

    exposure = repo_mod.dest_exposure(resolved)
    if exposure.undetermined:
        # A machine without git can still author secrets. Say what could not be
        # established and continue -- refusing here would be a guard breaking
        # the thing it guards. git ANSWERING unreadably is a different event
        # from git being absent, and is named as the anomaly it is.
        if exposure.anomalous:
            print(
                f"secrets-kit: ANOMALY: git could not answer whether "
                f"{resolved.as_posix()} is inside a working tree, so the "
                f"tracked-tree check did NOT run. Verify by hand that this "
                f"destination is not inside an unignored git repository. "
                f"git said: {exposure.detail or 'no output'}",
                file=sys.stderr,
            )
        else:
            print(
                f"secrets-kit: note: git is unavailable "
                f"({exposure.detail or 'git query failed'}), so the "
                f"tracked-tree check was skipped. It is re-checked at "
                f"convergence on every machine that holds the entry.",
                file=sys.stderr,
            )
        return None
    if not exposure.exposed:
        return None

    # This entry HAS the override, granted for the destination it used to have.
    # Being refused anyway is surprising, so say why rather than letting it
    # read as the flag having stopped working.
    changed = (
        "This entry already carries --allow-tracked-dest, but that consent was "
        "granted for its previous destination and does not transfer to a new "
        "one -- consent is per-destination. Re-run with --allow-tracked-dest "
        "to grant it for this path as well.\n\n"
        if consent_dropped
        else ""
    )
    # Without a verified repo root there is no correct path to print, so this
    # says what to do in prose rather than interpolating a placeholder into
    # something that looks like a real .gitignore location. Paths are posix,
    # like every other path this package prints for a human to act on.
    if exposure.toplevel:
        where = exposure.toplevel.as_posix()
        located = f"inside the git working tree at {where}"
        fix = (
            f"Add this line to {where}/.gitignore, commit it, and re-run:\n\n"
            f"    {exposure.gitignore_line}\n"
        )
    else:
        located = "inside a git working tree whose root could not be determined"
        fix = (
            "Find the repository that contains that path, add an ignore rule "
            "covering it to that repository's .gitignore, commit the change, "
            "and re-run.\n"
        )
    return _fail(
        f"'{name}' would materialize plaintext at {resolved.as_posix()}, which "
        f"is {located} and is NOT gitignored.\n"
        "Every convergence pass rewrites that file, so a routine `git add -A` "
        "stages the credential. A credential pushed once lives in the object "
        "store, in every clone, and in any fork or backup taken meanwhile -- "
        "rewriting history does not undo it.\n\n"
        f"{fix}\n"
        f"{changed}"
        "If this destination is deliberate -- the file is genuinely meant to "
        "be committed, or the repo is ignored some other way this check cannot "
        "see -- pass --allow-tracked-dest. That records the decision in the "
        "manifest, so the convergence pass on every machine honours it too."
    )


def _require_exclusive_blob(manifest: Manifest, clone: Path, name: str, blob: str) -> None:
    """Refuse mutation of the target blob while another entry owns its path."""
    target = os.path.normcase(os.path.normpath(os.fspath(clone / blob)))
    for owner, entry in manifest.entries.items():
        if owner == name:
            continue
        owned = os.path.normcase(os.path.normpath(os.fspath(clone / entry.blob)))
        if owned == target:
            raise SecretsError(
                f"cannot modify entry '{name}': blob '{blob}' is also owned "
                f"by entry '{owner}'",
                "This operation would change another entry's ciphertext. "
                "Use a uniquely named source when adding a different secret.",
            )


def cmd_add(args: argparse.Namespace) -> int:
    """Encrypt a file into the repo. Public-key op -- no passphrase needed."""
    config = _require_config()
    with operation_lock(DATA_DIR) as data_dir:
        operation = _prepare_entry(data_dir, paths_for(data_dir)["clone"], config.repo,
                                   name=args.name, source_name=Path(args.file).expanduser().name)
        return _add_entry(args, config, operation)


def _add_entry(args: argparse.Namespace, config: Config, operation: AuthoringOperation) -> int:
    clone = operation.clone
    try:
        manifest_path = clone / "manifest.json"
        manifest = Manifest.load(manifest_path)

        source = Path(args.file).expanduser()
        if not source.is_file():
            _fail(f"no such file: {source}")
            return operation.refuse()

        exists = args.name in manifest.entries
        if exists and not args.update:
            _fail(
                f"entry '{args.name}' already exists. Pass --update to rotate its "
                "value (this is the rotation path), or pick another name."
            )
            return operation.refuse()
        if not exists and not args.dest:
            _fail("--dest is required when adding a new entry")
            return operation.refuse()

        blob_rel = manifest.entries[args.name].blob if exists else f"blobs/{source.name}.age"
        _require_exclusive_blob(manifest, clone, args.name, blob_rel)

        plaintext = source.read_bytes()
        stored_spec = manifest.entries[args.name].dest_spec if exists else None
        dest_spec = args.dest or stored_spec
        mode = args.mode if args.mode is not None else (manifest.entries[args.name].mode if exists else "0600")
        newline = args.newline if args.newline is not None else (manifest.entries[args.name].newline if exists else None)

        # Consent is per-DESTINATION, never per-entry-forever. A stored override
        # carries forward only while the destination is unchanged -- otherwise
        # `add <name> --update --dest B` would inherit consent granted for dest A,
        # skip the check on B, AND re-persist the override so convergence honours
        # it too: a rotation could silently relocate a credential into a different
        # unignored working tree with nothing ever looking at it.
        dest_unchanged = not args.dest or args.dest == stored_spec
        inherited = bool(exists and dest_unchanged and manifest.entries[args.name].allow_tracked_dest)
        allow_tracked_dest = bool(args.allow_tracked_dest) or inherited
        # True when we are deliberately NOT honouring a stored override, so the
        # refusal can explain a rejection the user will not expect.
        consent_dropped = bool(
            exists and not dest_unchanged and manifest.entries[args.name].allow_tracked_dest
        )

        entry_data = {
            "blob": blob_rel,
            "dest": dest_spec,
            "mode": mode,
        }
        if allow_tracked_dest:
            entry_data["allow_tracked_dest"] = True
        if newline is not None:
            entry_data["newline"] = newline
        if args.doc:
            entry_data["doc"] = args.doc
        elif exists and manifest.entries[args.name].doc:
            entry_data["doc"] = manifest.entries[args.name].doc

        raw = json.loads(manifest.dump())
        raw["entries"][args.name] = entry_data
        for profile in args.profile or []:
            raw["profiles"].setdefault(profile, [])
            if args.name not in raw["profiles"][profile]:
                raw["profiles"][profile].append(args.name)
                raw["profiles"][profile].sort()

        rewritten = Manifest(manifest_path, raw)
        if rewritten.entries[args.name].newline == "lf" and b"\r\n" in plaintext:
            requirement = "inherited newline lf is required" if args.newline is None else "--newline lf was requested"
            _fail(
                f"{source} contains CRLF but {requirement}. "
                "Convert it first; seeding a CRLF ssh key or token breaks the "
                "consumer in ways that are painful to diagnose later."
            )
            return operation.refuse()

        # Validate the complete declaration and exposure before writing ciphertext.
        refusal = _refuse_exposed_dest(
            config,
            args.name,
            dest_spec,
            allow_tracked_dest,
            consent_dropped=consent_dropped,
        )
        if refusal is not None:
            return operation.refuse()

        if operation.record["selected_blob"] != blob_rel:
            raise AuthoringRecoveryError("entry selection differs from its captured footprint")
        operation.prepare_entry(rewritten.dump().encode("utf-8"), manifest.recipient, plaintext)

        verb = "rotate" if exists else "add"
        operation.apply_entry(f"{verb}: {args.name}")
        print(f"{'rotated' if exists else 'added'} '{args.name}' -> {blob_rel}")
        return 0
    except BaseException as primary:
        operation.failure(primary)
        raise


def cmd_remove(args: argparse.Namespace) -> int:
    """Drop an entry. Every machine deletes its copy on the next pass."""
    config = _require_config()
    with operation_lock(DATA_DIR) as data_dir:
        operation = _prepare_entry(data_dir, paths_for(data_dir)["clone"], config.repo,
                                   name=args.name, source_name=None)
        return _remove_entry(args, operation)


def _remove_entry(args: argparse.Namespace, operation: AuthoringOperation) -> int:
    clone = operation.clone
    try:
        manifest_path = clone / "manifest.json"
        manifest = Manifest.load(manifest_path)

        if args.name not in manifest.entries:
            _fail(f"no entry named '{args.name}'")
            return operation.refuse()

        blob_rel = manifest.entries[args.name].blob
        _require_exclusive_blob(manifest, clone, args.name, blob_rel)

        raw = json.loads(manifest.dump())
        raw["entries"].pop(args.name)
        for profile, names in raw["profiles"].items():
            raw["profiles"][profile] = [n for n in names if n != args.name]

        rewritten = Manifest(manifest_path, raw)
        if operation.record["selected_blob"] != blob_rel:
            raise AuthoringRecoveryError("entry selection differs from its captured footprint")
        operation.prepare_entry(rewritten.dump().encode("utf-8"), manifest.recipient, None)
        operation.apply_entry(f"remove: {args.name}")
        print(
            f"removed '{args.name}'. Note the ciphertext remains in git history "
            "forever -- if the VALUE was sensitive and is now exposed, rotate the "
            "underlying credential; deleting the blob is not revocation."
        )
        return 0
    except BaseException as primary:
        operation.failure(primary)
        raise


# --------------------------------------------------------------------------
# rotate-identity
# --------------------------------------------------------------------------

def cmd_rotate_identity(args: argparse.Namespace) -> int:
    """New keypair + re-encrypt every blob. Needs this machine to be unlocked.

    Rotation replaces the root of trust, so it runs under the same authoring
    operation as seeding and entry authoring: the complete replacement epoch
    is prepared privately, published as ONE exact-ref push whose outcome is
    proved, and only then cached. A rejection restores the checkout; an
    unproved outcome retains recovery evidence instead of guessing.
    """
    handed_off = _handoff_to_terminal(args)
    if handed_off is not None:
        return handed_off
    config = _require_config()
    with operation_lock(DATA_DIR) as data_dir:
        paths = paths_for(data_dir)
        operation = _prepare_rotation(data_dir, paths["clone"], config.repo)
        try:
            # After admission, deliberately: rotation validates the repository
            # it would publish to on the same terms every other authoring verb
            # does, so an unsupported clone is reported as one rather than
            # hidden behind a local precondition.
            if not paths["identity"].is_file():
                raise SecretsError(
                    f"this machine is locked; run `{cli_command('unlock')}` first. "
                    "Rotation re-encrypts every blob, so it has to be able to read "
                    "them."
                )
            manifest_path = operation.clone / "manifest.json"
            manifest = Manifest.load(manifest_path)

            print("Generating the replacement identity ...")
            identity_text, recipient = agefile.keygen()
            raw = json.loads(manifest.dump())
            raw["recipient"] = recipient
            rewritten = Manifest(manifest_path, raw)

            print("Re-encrypting every blob to the replacement recipient ...")
            print(
                "\nWhen age prompts, choose the passphrase for the NEW identity (it "
                "may be the same one or a different one). Every other machine will "
                "need to unlock again."
            )
            code = operation.prepare_rotation(
                identity_text, recipient, rewritten.dump().encode("utf-8")
            )
            if code != 0:
                raise SecretsError(
                    "age failed to wrap the new identity; nothing was published"
                )
            operation.apply_rotation(identity_text)
        except SecretsError as primary:
            phase = operation.record["phase"]
            operation.failure(primary)
            recovery = getattr(primary, "authoring_recovery_error", None)
            details = f"{primary}\n{recovery}" if recovery else str(primary)
            if phase == "definitely_rejected" and not recovery:
                details += "\nOwned rotation state restored; the generated identity was discarded."
            return _fail(details)
        except BaseException as primary:
            operation.failure(primary)
            raise
        print(
            "\nidentity rotated. Other machines will report a decrypt failure "
            f"once and need `! {cli_command('unlock')}` again.\n"
            "REMEMBER: this stops the old identity reading FUTURE blobs. It does "
            "not un-read the past. If a machine was lost, rotate the underlying "
            "credentials too -- that is the real revocation."
        )
        return 0


def _add_new_terminal_flag(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--new-terminal",
        action="store_true",
        help="open a terminal window and run this there (for agents: age needs a tty)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="secrets-kit",
        description="Fleet secrets: materialize age-encrypted credentials per machine.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("unlock", help="unlock this machine (interactive, once)")
    _add_new_terminal_flag(p)
    p.set_defaults(func=cmd_unlock)

    p = sub.add_parser("status", help="what this machine holds / is waiting on")
    p.add_argument("--refresh", action="store_true", help="fetch even if within cooldown")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("init", help="seed a new secrets repo (once, interactive)")
    p.add_argument("--force", action="store_true", help="re-seed over an existing identity")
    _add_new_terminal_flag(p)
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("add", help="encrypt a file into the repo")
    p.add_argument("name")
    p.add_argument("--file", required=True, help="plaintext source path")
    p.add_argument("--dest", help="materialization target (supports ${VAR} and ~)")
    p.add_argument("--mode", default=None, help="POSIX mode; inherits on updates, defaults to 0600 for new entries")
    p.add_argument("--newline", choices=["lf"], help="assert LF line endings")
    p.add_argument("--doc", help="pointer into the secrets inventory")
    p.add_argument("--profile", action="append", help="add to this profile (repeatable)")
    p.add_argument("--update", action="store_true", help="rotate an existing entry's value")
    p.add_argument(
        "--allow-tracked-dest",
        action="store_true",
        help="permit a dest inside a non-ignored git working tree (recorded in the manifest)",
    )
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("remove", help="drop an entry from the repo")
    p.add_argument("name")
    p.set_defaults(func=cmd_remove)

    p = sub.add_parser(
        "rotate-identity", help="new keypair + re-encrypt everything (interactive)"
    )
    _add_new_terminal_flag(p)
    p.set_defaults(func=cmd_rotate_identity)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except SecretsError as e:
        release_error = getattr(e, "operation_lock_release_error", None)
        recovery_error = getattr(e, "authoring_recovery_error", None)
        details = [str(e)] + [str(error) for error in (recovery_error, release_error) if error]
        return _fail("\n".join(details))


if __name__ == "__main__":
    sys.exit(main())
