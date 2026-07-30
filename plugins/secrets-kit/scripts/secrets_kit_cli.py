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
from secrets_kit import repo as repo_mod  # noqa: E402
from secrets_kit.converge import converge, paths_for  # noqa: E402
from secrets_kit.manifest import Config, Manifest  # noqa: E402
from secrets_kit.perms import tighten, tighten_dir  # noqa: E402
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


def _ensure_clone(config: Config, *, sync: bool = False) -> Path:
    paths = paths_for(DATA_DIR)
    clone = paths["clone"]
    if not repo_mod.is_clone(clone):
        print(f"cloning {config.repo} ...")
        repo_mod.clone(config.repo, clone)
        return clone
    if sync:
        print("syncing with the remote ...")
        repo_mod.sync(clone)
    return clone


def _ensure_guarded(config: Config) -> Path:
    """Sync, clone if needed, then guarantee the pre-commit guard before any write.

    Every authoring verb goes through here rather than ``_ensure_clone``. Two
    things have to be true before we let git record anything permanently, and
    neither is inheritable:

    - The clone must be level with the remote. The session pass fetches at most
      once every few hours, so the working tree an authoring verb would read
      its decisions from is routinely hours stale -- and "is this repo seeded?"
      answered about the past is how a second fleet identity gets generated.
    - The pre-commit guard must exist. It lives in ``.git/hooks``, which is
      untracked, so it has to be re-established locally every time.
    """
    clone = _ensure_clone(config, sync=True)
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
    clone = _ensure_clone(config)
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

    paths = paths_for(DATA_DIR)
    tighten_dir(DATA_DIR)

    print("Enter your fleet secrets passphrase (input is hidden).")
    code = agefile.unwrap_identity(wrapped, paths["identity"])
    if code != 0:
        # Leave nothing half-written: a truncated identity file would make the
        # next pass fail with a confusing decrypt error instead of "locked".
        try:
            paths["identity"].unlink()
        except OSError:
            pass
        return _fail("incorrect passphrase (or age failed); nothing was written")

    tighten(paths["identity"], 0o600)
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
    try:
        clone = _ensure_guarded(config)
    except SecretsError as e:
        # A clone that diverged from an already-seeded remote is the signature
        # of a failed earlier seed, and the generic "diverged" advice would
        # leave the user resolving git rather than told what they actually
        # need. Name the real next step; the divergence is a consequence, not
        # the problem.
        clone = paths_for(DATA_DIR)["clone"]
        if repo_mod.is_clone(clone) and repo_mod.remote_has(clone, "identity.age"):
            return _fail(
                f"{e}\n\n"
                "Note what the remote already holds: an identity.age. This "
                "repo IS seeded -- the local commit(s) above are a seed "
                "attempt that never published, and discarding them loses "
                "nothing. What this machine needs is not another seed but "
                f"`{cli_command('unlock --new-terminal')}`."
            )
        raise

    manifest_path = clone / "manifest.json"
    wrapped = clone / "identity.age"

    # Ask the REMOTE, not the checkout. Seeding is the one irreversible act
    # here -- a second identity orphans every blob encrypted to the first --
    # and the checkout can only tell us what was true at the last fetch. The
    # local file is checked too, for the case where the branch has no upstream.
    if not args.force and (repo_mod.remote_has(clone, "identity.age") or wrapped.exists()):
        return _fail(
            "this repo is already seeded -- identity.age exists. Re-running "
            "init would generate a SECOND fleet identity and orphan every "
            "existing blob (they are encrypted to the first one's public "
            "key).\n"
            f"To use the existing fleet identity on this machine, run "
            f"`{cli_command('unlock --new-terminal')}`.\n"
            "To change the passphrase or key while keeping the blobs readable, "
            "use `rotate-identity`. Pass --force only if you really mean to "
            "abandon the existing secrets and start over."
        )

    # Everything from here to the push is one transaction. A partial seed is
    # the worst outcome available: this machine would cache an identity the
    # fleet has never heard of, and every later decrypt would fail with an
    # error pointing at the wrong thing.
    before = repo_mod.head_sha(clone)

    print("Generating the fleet age identity ...")
    identity_text, recipient = agefile.keygen()

    print(
        "\nChoose a strong passphrase for the fleet identity. You will type it "
        "twice now, and once on each machine you unlock -- nowhere else. "
        "Escrow it in your password manager: after seeding it is the only "
        "remote path back into these secrets."
    )
    code = agefile.wrap_identity(identity_text, wrapped)
    if code != 0:
        try:
            wrapped.unlink()
        except OSError:
            pass
        return _fail("age failed to wrap the identity; nothing was written")

    manifest = Manifest(
        manifest_path,
        {"version": 1, "recipient": recipient, "profiles": {}, "entries": {}},
    )
    manifest_path.write_text(manifest.dump(), encoding="utf-8")

    # Deny-by-default .gitignore, layered under the pre-commit guard. The guard
    # stops a deliberate `git add`; this stops a careless `git add -A` from
    # staging a stray plaintext file at all. Two independent nets, because the
    # thing they prevent cannot be undone.
    wrote_ignore = guard.ensure_gitignore(clone)

    seeded_paths = ["identity.age", "manifest.json"]
    if wrote_ignore:
        seeded_paths.append(".gitignore")
    try:
        repo_mod.commit_and_push(
            clone, "seed: fleet identity + empty manifest", seeded_paths
        )
    except SecretsError as e:
        # Publishing is what MAKES the seed real. If it did not land, unwind
        # rather than leaving a local-only fleet identity behind: the next run
        # would find identity.age in the checkout, conclude the repo is seeded,
        # and refuse -- pointing the user at an identity no other machine can
        # ever obtain.
        repo_mod.rollback_to(clone, before, created=seeded_paths)
        return _fail(
            f"{e}\n"
            "Nothing was published and nothing was kept: the generated "
            "identity has been discarded and this machine is unchanged. The "
            "passphrase you just chose applies to nothing -- re-run init once "
            "the repo state above is resolved and choose one again."
        )

    # Cache the unlocked identity locally so the seeding machine does not have
    # to unlock itself immediately after creating the key it just held. Written
    # only AFTER the push, so this file can never name a key the fleet lacks.
    paths = paths_for(DATA_DIR)
    tighten_dir(DATA_DIR)
    fd = os.open(str(paths["identity"]), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(identity_text)
    tighten(paths["identity"], 0o600)

    print(f"\nseeded. recipient = {recipient}")
    print(f"Add secrets with: {cli_command('add')} <name> --file <path> --dest <dest>")
    return 0


# --------------------------------------------------------------------------
# add / remove
# --------------------------------------------------------------------------

def cmd_add(args: argparse.Namespace) -> int:
    """Encrypt a file into the repo. Public-key op -- no passphrase needed."""
    config = _require_config()
    clone = _ensure_guarded(config)
    manifest_path = clone / "manifest.json"
    manifest = Manifest.load(manifest_path)

    source = Path(args.file).expanduser()
    if not source.is_file():
        return _fail(f"no such file: {source}")

    exists = args.name in manifest.entries
    if exists and not args.update:
        return _fail(
            f"entry '{args.name}' already exists. Pass --update to rotate its "
            "value (this is the rotation path), or pick another name."
        )
    if not exists and not args.dest:
        return _fail("--dest is required when adding a new entry")

    plaintext = source.read_bytes()
    if args.newline == "lf" and b"\r\n" in plaintext:
        return _fail(
            f"{source} contains CRLF but --newline lf was requested. "
            "Convert it first; seeding a CRLF ssh key or token breaks the "
            "consumer in ways that are painful to diagnose later."
        )

    blob_rel = f"blobs/{source.name}.age"
    if exists:
        blob_rel = manifest.entries[args.name].blob

    agefile.encrypt_to_recipient(manifest.recipient, plaintext, clone / blob_rel)

    entry_data = {
        "blob": blob_rel,
        "dest": args.dest or manifest.entries[args.name].dest_spec,
        "mode": args.mode,
    }
    if args.newline:
        entry_data["newline"] = args.newline
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
    manifest_path.write_text(rewritten.dump(), encoding="utf-8")

    verb = "rotate" if exists else "add"
    repo_mod.commit_and_push(
        clone, f"{verb}: {args.name}", [blob_rel, "manifest.json"]
    )
    print(f"{'rotated' if exists else 'added'} '{args.name}' -> {blob_rel}")
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    """Drop an entry. Every machine deletes its copy on the next pass."""
    config = _require_config()
    clone = _ensure_guarded(config)
    manifest_path = clone / "manifest.json"
    manifest = Manifest.load(manifest_path)

    if args.name not in manifest.entries:
        return _fail(f"no entry named '{args.name}'")

    raw = json.loads(manifest.dump())
    blob_rel = raw["entries"].pop(args.name)["blob"]
    for profile, names in raw["profiles"].items():
        raw["profiles"][profile] = [n for n in names if n != args.name]

    rewritten = Manifest(manifest_path, raw)
    manifest_path.write_text(rewritten.dump(), encoding="utf-8")
    try:
        (clone / blob_rel).unlink()
    except OSError:
        pass

    repo_mod.commit_and_push(clone, f"remove: {args.name}", [blob_rel, "manifest.json"])
    print(
        f"removed '{args.name}'. Note the ciphertext remains in git history "
        "forever -- if the VALUE was sensitive and is now exposed, rotate the "
        "underlying credential; deleting the blob is not revocation."
    )
    return 0


# --------------------------------------------------------------------------
# rotate-identity
# --------------------------------------------------------------------------

def cmd_rotate_identity(args: argparse.Namespace) -> int:
    """New keypair + re-encrypt every blob. Needs this machine to be unlocked."""
    handed_off = _handoff_to_terminal(args)
    if handed_off is not None:
        return handed_off
    config = _require_config()
    clone = _ensure_guarded(config)
    manifest_path = clone / "manifest.json"
    manifest = Manifest.load(manifest_path)
    paths = paths_for(DATA_DIR)

    if not paths["identity"].is_file():
        return _fail(
            f"this machine is locked; run `{cli_command('unlock')}` first. "
            "Rotation re-encrypts every blob, so it has to be able to read "
            "them."
        )

    print("Decrypting every blob with the current identity ...")
    plaintexts = {}
    for name, entry in manifest.entries.items():
        plaintexts[name] = agefile.decrypt_with_identity(
            paths["identity"], clone / entry.blob
        )

    print("Generating the replacement identity ...")
    identity_text, recipient = agefile.keygen()
    print(
        "\nChoose the passphrase for the NEW identity (it may be the same one "
        "or a different one). Every other machine will need to unlock again."
    )
    code = agefile.wrap_identity(identity_text, clone / "identity.age")
    if code != 0:
        return _fail("age failed to wrap the new identity; nothing was changed")

    touched = ["identity.age", "manifest.json"]
    for name, entry in manifest.entries.items():
        agefile.encrypt_to_recipient(recipient, plaintexts[name], clone / entry.blob)
        touched.append(entry.blob)

    raw = json.loads(manifest.dump())
    raw["recipient"] = recipient
    manifest_path.write_text(Manifest(manifest_path, raw).dump(), encoding="utf-8")

    fd = os.open(str(paths["identity"]), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(identity_text)
    tighten(paths["identity"], 0o600)

    repo_mod.commit_and_push(clone, "rotate: fleet identity", touched)
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
    p.add_argument("--mode", default="0600", help="POSIX mode, default 0600")
    p.add_argument("--newline", choices=["lf"], help="assert LF line endings")
    p.add_argument("--doc", help="pointer into the secrets inventory")
    p.add_argument("--profile", action="append", help="add to this profile (repeatable)")
    p.add_argument("--update", action="store_true", help="rotate an existing entry's value")
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
        return _fail(str(e))


if __name__ == "__main__":
    sys.exit(main())
