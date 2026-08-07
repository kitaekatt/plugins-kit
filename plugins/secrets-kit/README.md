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

### Choosing a destination

Default: materialize at the path the consumer already reads. It removes the
copy step and the second working copy that drifts from source. Fall back to
a per-repo collection directory, with the consumer taught that path (a copy
step, a symlink, or a config option), only when the consumer cannot accept
an arbitrary path -- e.g. a build tool reading a fixed filename adjacent to
its input.

If the resolved dest falls inside a git working tree, `add` refuses unless
the path is gitignored, printing the exact `.gitignore` line that would fix
it; `--allow-tracked-dest` overrides this for the intentional case and the
override is persisted -- scoped to the entry and the destination it was
granted for, so moving the entry to a different `--dest` does not inherit it.
Convergence re-checks the same condition
every session (add-time can only validate the authoring machine's variable
resolution, and a dest can be per-OS or per-machine, so it may be ignored
where it was added and tracked where it lands), ahead of its unchanged-content
fast path so an exposed entry is reported every pass instead of going quiet
once it has settled. A pending write is withheld rather than writing plaintext
into tracked history; a dest already materialized is reported and left alone,
with the `git rm --cached` to untrack it and a reminder that a value ever
committed must be rotated, since deleting it from the tree is not revocation.
This mirrors the secrets repo's own two-net posture (an allowlist pre-commit
hook plus a deny-by-default `.gitignore`): a plaintext credential pushed
once survives in the object store, in every clone, and in any fork or
backup taken meanwhile, and a consumer repo has the same irreversible
outcome.

Interactive -- `age` prompts on the terminal itself, so these need a tty. Pass
`--new-terminal` and the CLI spawns a window for the prompt and returns
immediately; the passphrase is typed there and never reaches a transcript,
which is what lets an agent drive these without ever seeing it:

```bash
secrets-kit unlock --new-terminal           # once per machine
secrets-kit init --new-terminal             # once per fleet, on the machine holding the plaintext
secrets-kit rotate-identity --new-terminal  # new keypair + re-encrypt everything
```

Drop the flag when you are already at a terminal and want the prompt inline.

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
