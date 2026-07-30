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

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PLUGIN_ROOT / "lib"))

from secrets_kit import SecretsError, cli_command  # noqa: E402
from secrets_kit import agefile  # noqa: E402
from secrets_kit import guard  # noqa: E402
from secrets_kit import repo as repo_mod  # noqa: E402
from secrets_kit.converge import converge, paths_for  # noqa: E402
from secrets_kit.manifest import Config, Manifest  # noqa: E402
from secrets_kit.perms import tighten, tighten_dir  # noqa: E402

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


def _require_config() -> Config:
    config = Config.load(CONFIG_PATH)
    if config is None:
        raise SecretsError(
            f"no configuration at {CONFIG_PATH}",
            "secrets-kit needs a secrets.json declaring the repo URL and this "
            "machine's profiles before it can do anything.",
        )
    return config


def _ensure_clone(config: Config) -> Path:
    paths = paths_for(DATA_DIR)
    clone = paths["clone"]
    if not repo_mod.is_clone(clone):
        print(f"cloning {config.repo} ...")
        repo_mod.clone(config.repo, clone)
    return clone


def _ensure_guarded(config: Config) -> Path:
    """Clone if needed, then guarantee the pre-commit guard before any write.

    Every authoring verb goes through here rather than ``_ensure_clone``. The
    guard is per-clone (``.git/hooks`` is untracked), so "the repo is guarded"
    is not something a machine can inherit -- it has to be established locally,
    every time, before we hand git anything to record permanently.
    """
    clone = _ensure_clone(config)
    note = guard.require_guard(clone)
    if note:
        print(f"pre-commit guard: {note}")
    return clone


# --------------------------------------------------------------------------
# unlock
# --------------------------------------------------------------------------

def cmd_unlock(_args: argparse.Namespace) -> int:
    """Decrypt the fleet identity onto this machine. Once per machine, ever."""
    config = _require_config()
    clone = _ensure_clone(config)
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
    config = _require_config()
    clone = _ensure_guarded(config)
    manifest_path = clone / "manifest.json"
    wrapped = clone / "identity.age"

    if wrapped.exists() and not args.force:
        return _fail(
            f"{wrapped} already exists -- this repo is already seeded. "
            "Re-running init would orphan every existing blob (they are "
            "encrypted to the OLD public key). Use `rotate-identity` to "
            "change the passphrase or key, or pass --force if you really mean "
            "to start over."
        )

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

    # Cache the unlocked identity locally so the seeding machine does not have
    # to unlock itself immediately after creating the key it just held.
    paths = paths_for(DATA_DIR)
    tighten_dir(DATA_DIR)
    fd = os.open(str(paths["identity"]), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(identity_text)
    tighten(paths["identity"], 0o600)

    seeded_paths = ["identity.age", "manifest.json"]
    if wrote_ignore:
        seeded_paths.append(".gitignore")
    repo_mod.commit_and_push(
        clone, "seed: fleet identity + empty manifest", seeded_paths
    )
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
    print(f"{verb}d '{args.name}' -> {blob_rel}")
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

def cmd_rotate_identity(_args: argparse.Namespace) -> int:
    """New keypair + re-encrypt every blob. Needs this machine to be unlocked."""
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="secrets-kit",
        description="Fleet secrets: materialize age-encrypted credentials per machine.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("unlock", help="unlock this machine (interactive, once)")
    p.set_defaults(func=cmd_unlock)

    p = sub.add_parser("status", help="what this machine holds / is waiting on")
    p.add_argument("--refresh", action="store_true", help="fetch even if within cooldown")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("init", help="seed a new secrets repo (once, interactive)")
    p.add_argument("--force", action="store_true", help="re-seed over an existing identity")
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
