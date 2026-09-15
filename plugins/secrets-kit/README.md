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
version-keyed install directory, and the passphrase verbs (`unlock`, `init`,
`rotate-identity`) need a real terminal. Resolving the shim and driving those
verbs is the skill's job, not README's: see
[`skills/secrets-kit/SKILL.md`](skills/secrets-kit/SKILL.md) ("Resolving the
CLI" and "The passphrase verbs").

Once resolved, the everyday commands read as:

```bash
secrets-kit status                  # what this machine holds / waits on (safe, no passphrase)
secrets-kit add ha-token --file secrets/ha-token.txt \
    --dest '${KNOWLEDGE_BANK}/secrets/ha-token.txt' --profile home-admin
secrets-kit remove ha-token         # every machine deletes its copy next pass
```

### Destinations

Default: materialize at the path the consumer already reads, falling back to
a per-repo collection directory only when the consumer cannot accept an
arbitrary path. A `--dest` that lands inside a git working tree and is **not
gitignored** is refused, with `--allow-tracked-dest` for the intentional
case. The full selection procedure, the override's scope, per-machine
resolution, convergence check ordering, exposure remediation, and the
write protocol that actually puts plaintext on disk (with its documented
limits) are owned by the skill: see
[`skills/secrets-kit/references/destinations.md`](skills/secrets-kit/references/destinations.md).

Seed, add/update and remove preserve recovery evidence when publication or
finalization is unresolved. Follow the [skill's authoring recovery guidance](skills/secrets-kit/SKILL.md#technique)
for publication limits, restoration conditions and preservation obligations.

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
- **Atomic writes at the destination's mode**, never a looser one in
  between. Mechanics, the implementation pointer, and the documented limits
  (no power-loss recovery, no defense against a concurrent substitution in
  an untrusted parent, Windows ACL accepted-not-verified) are owned by
  [`skills/secrets-kit/references/destinations.md`](skills/secrets-kit/references/destinations.md).
- **Two outcomes only.** Every failure is AUTO (an agent can fix it now) or ASK
  (only the user can supply it). There is no warning tier; a warning about a
  credential is a failure nobody acted on.
- **An allowlist pre-commit guard, installed automatically.** Only the
  manifest, the wrapped identity, and age-ciphertext blobs may be committed.
  Mechanics and why the hook is copied rather than sourced are owned by
  [`skills/secrets-kit/SKILL.md`](skills/secrets-kit/SKILL.md) ("The
  pre-commit guard").
- **Written against the bootstrap service-provider seam** from day one
  (`service` block in `bootstrap.json`, `bootstrap(ctx)` entry point touching
  only the documented ctx surface, all logic in `lib/secrets_kit/`), so folding
  it into the engine later is a file move rather than a rewrite.
