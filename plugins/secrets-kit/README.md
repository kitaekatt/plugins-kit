# secrets-kit

Fleet secrets provisioning. A private git repo holds age-encrypted credentials;
every machine materializes the subset it is entitled to on session start. A
fresh machine gets everything it needs by cloning its config repo, restarting
the client, and answering one prompt -- with no file copied by hand and no step
run on a different machine.

## The problem it solves

Machine-local credentials in a gitignored `secrets/` directory exist on exactly
one machine. Every other machine looks identical and is silently broken: the
scripts are there, the tokens are not. Copying files by hand fixes one box,
records nothing, and goes stale the moment a value rotates.

## How it works

```
fleet-secrets/          (private repo)
  manifest.json         recipient pubkey + profiles + entry -> dest mapping
  identity.age          the age keypair, passphrase-wrapped
  blobs/*.age           one per credential, encrypted TO the pubkey
```

```
~/.claude/secrets.json  (private, tracked)   repo URL + machine -> profiles + per-machine vars
<data_dir>/repo         the clone (fetched at most once per 6h)
<data_dir>/identity.txt the unlocked identity (0600, never in a git tree)
<data_dir>/state.json   blob + plaintext hashes, so the steady state decrypts nothing
```

Blobs encrypt **to a public key**, so adding or rotating a secret needs no
passphrase and can be done unattended from any machine. Only `identity.age` is
passphrase-wrapped, so only *unlocking a machine* needs the passphrase -- once,
ever.

## Usage

The CLI is **not on PATH** -- it ships as a shim inside the plugin's
version-keyed install directory. Resolve it before using any command below:

```bash
SK=$(ls -d ~/.claude/plugins/cache/plugins-kit/secrets-kit/*/bin/secrets-kit | tail -1)
```

Then `$SK <verb>`. The commands are written as `secrets-kit <verb>` for
readability; substitute the resolved path.

```bash
secrets-kit status                  # what this machine holds / waits on (safe, no passphrase)
secrets-kit add ha-token --file secrets/ha-token.txt \
    --dest '${KNOWLEDGE_BANK}/secrets/ha-token.txt' --profile home-admin
secrets-kit remove ha-token         # every machine deletes its copy next pass
```

Interactive (must be run by the user, so the passphrase never reaches a
transcript -- `age` prompts on the terminal itself):

```
! secrets-kit unlock                # once per machine
! secrets-kit init                  # once per fleet, on the machine holding the plaintext
! secrets-kit rotate-identity       # new keypair + re-encrypt everything
```

## What it does not do

Stated plainly, because the alternative is implying the crypto did more than it
did:

- It protects the **repo host and transport**. It does not protect a stolen
  unlocked machine -- that box had the plaintext on disk.
- Removing a blob, or rotating the identity, does not un-read the past.
  Ciphertext stays in git history. **Real revocation is rotating the underlying
  credential.**
- Passphrase strength is the wall. There is no server, no lockout, no MFA.

## Design notes

- **Never blocks a session.** An offline machine converges on a stale clone; a
  failed fetch is a log line. Only a missing identity raises an ask, and it is
  the one thing a human can actually resolve.
- **Atomic writes at the final mode.** The temp file is created in the
  destination directory already at 0600 (owner-only ACL on Windows), written,
  fsynced, then renamed -- so decrypted material never exists at a loose mode
  and a crash leaves either the old file or nothing.
- **Two outcomes only.** Every failure is AUTO (an agent can fix it now) or ASK
  (only the user can supply it). There is no warning tier; a warning about a
  credential is a failure nobody acted on.
- **An allowlist pre-commit guard, installed automatically.** Authoring verbs
  install `.git/hooks/pre-commit` into the clone and refuse to write to an
  unguarded repo. Only the manifest, the wrapped identity, and age-ciphertext
  blobs may be committed; the guard reads the index rather than the worktree,
  and rejects an unwrapped `AGE-SECRET-KEY-` anywhere. It is copied, not
  sourced, because the plugin's cache path moves on every version bump.
- **Written against the bootstrap service-provider seam** from day one
  (`service` block in `bootstrap.json`, `bootstrap(ctx)` entry point touching
  only the documented ctx surface, all logic in `lib/secrets_kit/`), so folding
  it into the engine later is a file move rather than a rewrite.
