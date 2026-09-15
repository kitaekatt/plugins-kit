---
_schema_version: 1
name: secrets-kit
author: christina
skill-type: technique-skill
description: Use when unlocking, seeding, adding, rotating, removing, or diagnosing fleet secrets. Do NOT use for per-project API keys.
---

# secrets-kit

Fleet secrets provisioning. A private git repo holds age-encrypted blobs; each
machine materializes the subset it is entitled to, on every session start,
declaratively. The user's whole interface is "restart and answer the prompt" --
plus exactly one hidden-input command, once per machine, ever.

## The trust model in one breath

Blobs are encrypted **to a public key**. Only `identity.age` is
passphrase-wrapped. Therefore:

- **Adding or rotating a secret needs no passphrase** -- it is a public-key
  operation, runnable on any machine, by an agent, unattended.
- **Unlocking a machine needs the passphrase** -- once, ever, by the user.
- The passphrase is typed exactly twice in the system's life per epoch: once at
  seeding, once per machine at unlock.

This asymmetry is the reason the design is usable at all. Do not reach for the
passphrase to do routine work; if you think you need it, you are on the wrong
path.

## Resolving the CLI: `secrets-kit` is NOT on PATH

The CLI ships as a shim inside the plugin's own install directory, which is
version-keyed and never added to PATH. Typing a bare `secrets-kit` fails with
`command not found` -- so **every `secrets-kit <verb>` written below is
shorthand, not a command to hand anyone verbatim.** Resolve it first:

```bash
ls -d ~/.claude/plugins/cache/plugins-kit/secrets-kit/*/bin/secrets-kit | tail -1
```

That prints the invocation to use, e.g.
`~/.claude/plugins/cache/plugins-kit/secrets-kit/0.5.0/bin/secrets-kit`. Run it
with that path, and substitute that path into any prepared statement you relay.

The bootstrap failure messages and the CLI's own error text already render the
resolved path (`secrets_kit.cli_command()`), so take the command from those
rather than rewriting it into a bare command name.

## The passphrase verbs: ask, then run with `--new-terminal`

`unlock`, `init` and `rotate-identity` need a tty, because `age` prompts on the
terminal itself rather than reading stdin. You have no tty -- and neither does
the user's `!` prefix, so relaying "type this yourself" does not work either.

Instead: **ask the user for consent, then run the verb yourself with
`--new-terminal`.** It spawns a real terminal window, returns immediately, and
the user answers the hidden-input prompt in that window. The passphrase still
never reaches the transcript, which was the whole point of the tty requirement.

```bash
<resolved-path> unlock --new-terminal
```

Then tell them a window has opened and the prompt is in there, not here.

## Technique

```yaml
technique_skill:
  _schema_version: "1"
  identity: >-
    Provision fleet credentials from a private age-encrypted repo onto each
    machine -- unlock, seed, add, rotate, remove, diagnose -- via the
    secrets-kit CLI and its session-start convergence pass.
  scope:
    covers:
      - unlocking a new machine so it can receive fleet secrets
      - seeding a brand-new secrets repo from the machine holding the plaintext
      - adding, rotating, and removing a fleet credential
      - diagnosing why a secret did not materialize on a machine
      - rotating the fleet identity after a machine is lost
    excludes:
      - per-project or per-service API keys with a single consumer (openrouter-account)
      - deciding WHICH credentials belong in the fleet (that is the owner's call)
      - anything requiring the user's passphrase to pass through an agent (never)
  invariants:
    - id: passphrase_never_in_chat
      rule: >-
        NEVER ask the user to type or paste the fleet passphrase into the chat,
        under any framing, including AskUserQuestion's free-text "Other".
        Transcripts are written to disk. There is deliberately no
        paste-it-here fallback, unlike an API key.
      instead: >-
        Ask for consent, then run `secrets-kit unlock --new-terminal`. age
        prompts with hidden input in the window that opens, which the
        transcript never sees.
    - id: cli_is_not_on_path
      rule: >-
        NEVER hand the user (or type yourself) a bare `secrets-kit <verb>`.
        The shim lives in the plugin's version-keyed install dir and is not on
        PATH, so a bare name fails with "command not found" -- which, for a
        relayed prepared statement, wastes the one interactive step the user
        was asked to take.
      instead: >-
        Resolve the shim path first (see "Resolving the CLI" above) and use it,
        or relay the bootstrap/CLI message verbatim -- those already render the
        resolved path themselves.
    - id: passphrase_verbs_need_new_terminal
      rule: >-
        NEVER run `unlock`, `init`, or `rotate-identity` bare -- age prompts on
        a tty you do not have, so they hang or fail. And do NOT relay them for
        the user to type: the `!` prefix has no tty either, and it spends their
        attention on clerical work.
      instead: >-
        ASK the user first, then run the verb yourself with `--new-terminal`.
        It spawns a real terminal window, returns immediately, and the user
        answers the hidden-input prompt in that window -- so the passphrase
        still never touches the transcript. Tell them to look for the window.
    - id: never_bypass_the_guard
      rule: >-
        NEVER use `git commit --no-verify` in a fleet-secrets clone, and never
        remove or edit `.git/hooks/pre-commit` there to get a commit through.
        The guard is an allowlist that refuses anything but manifest.json,
        identity.age, blobs/*.age, and a README/.gitignore/.gitattributes.
      instead: >-
        If a secret is being refused, encrypt it (`secrets-kit add`) instead of
        committing it. If a legitimate NON-secret file is refused, widen the
        allowlist in secrets-kit's canonical hook so every clone agrees --
        a local edit fixes one machine and silently leaves the rest strict.
    - id: deleting_a_blob_is_not_revocation
      rule: >-
        Removing an entry, or rotating the identity, does not un-read the past.
        Ciphertext stays in git history and a lost machine already had the
        plaintext.
      instead: >-
        Real revocation is rotating the underlying credential itself (the HA
        token, the UniFi password) per the fleet's secrets inventory. Say so
        plainly rather than implying the crypto handled it.
  techniques:
    - id: unlock
      name: Unlock a machine for fleet secrets
      keywords: [unlock, locked, new machine, secrets_locked, passphrase, materialize]
      goal: "Let this machine receive fleet secrets for the first time."
      operation: "secrets-kit unlock --new-terminal   (YOU run this, after asking)"
      steps:
        - n: 1
          action: >-
            Ask the user whether to unlock now (AskUserQuestion). Do not offer
            to handle the passphrase for them -- the offer is the thing that
            must never exist.
        - n: 2
          action: >-
            On consent, run `secrets-kit unlock --new-terminal`. It returns
            immediately; say plainly that a terminal window has opened and the
            hidden-input passphrase prompt is in THAT window, not here.
        - n: 3
          action: >-
            After they report success, nothing else is needed: the failing check
            re-runs every session, so the next pass materializes everything.
            Confirm with `secrets-kit status` if you want evidence.
      gotchas:
        - "The window holds itself open after the verb finishes, so the user can read the result. A vanished window means the launch failed, not that it succeeded."
        - "A wrong passphrase writes nothing and leaves the machine locked -- they can just retry."
        - "This is once per machine, not once per session. A machine asking repeatedly means the identity file is not persisting; check the data dir's permissions."
    - id: status
      name: Check what this machine holds
      keywords: [status, what did i get, diagnose, nothing materialized, check secrets]
      goal: "See what this machine holds and what it is waiting on."
      operation: "secrets-kit status [--refresh]"
      steps:
        - n: 1
          action: >-
            Run `secrets-kit status` -- no passphrase, no terminal needed.
            Read the printed result: entries materialized, entries pending,
            and any failures (each failure names the secret and the reason).
      gotchas:
        - "Safe to run yourself -- no passphrase, no terminal needed. This is the first thing to run when a secret is unexpectedly absent."
        - "`--refresh` bypasses the 6h fetch cooldown. Use it after someone rotates a secret and you want it NOW."
        - "Exit 1 means there are failures, which are printed. 'not configured' means no secrets.json exists -- an expected state, not a fault."
    - id: seed
      name: Seed a brand-new secrets repo
      keywords: [seed, init, new repo, first time, birth event, create fleet-secrets]
      goal: "Create the fleet identity and manifest in a brand-new secrets repo."
      operation: "secrets-kit init --new-terminal   (YOU run this, after asking)"
      steps:
        - n: 1
          action: >-
            Confirm the repo exists and is PRIVATE, and that secrets.json
            declares it. `gh repo create <acct>/<name> --private` is fine for
            the agent to run.
        - n: 2
          action: >-
            The pre-commit guard is AUTOMATIC -- every authoring verb installs
            it before it writes, and refuses to proceed on an unguarded clone.
            You do not need to install it by hand. Confirm it landed
            (`.git/hooks/pre-commit` in the clone carries a
            `secrets-kit-guard-version` marker) and move on.
        - n: 3
          action: >-
            Ask the user, then run `secrets-kit init --new-terminal`. They
            choose the passphrase and type it twice in the window that opens.
            Remind them to escrow it in their password manager -- after seeding
            it is the only remote path back in.
        - n: 4
          action: >-
            Then YOU add each entry with `secrets-kit add` (no passphrase
            needed), and write secrets.json's machines/profiles blocks.
      gotchas:
        - "Run ONCE per fleet. `init` on an already-seeded repo refuses, because every existing blob is encrypted to the OLD public key and would be orphaned. Use rotate-identity instead."
        - "It asks the REMOTE whether the repo is seeded, not the local checkout -- a clone that has not fetched since before someone else seeded would otherwise report 'never seeded'. If init says already seeded, believe it over the session-pass message that sent you here, and run `unlock --new-terminal` instead."
        - "Seed publishes one exact commit without automatic rebase or a second push. Before submission or after a validated rejection, it restores its owned entry checkout and index and preserves the existing identity cache. A lost push report can still mean publication succeeded."
        - "Uncertain publication or incomplete cache finalization retains encrypted recovery evidence and blocks ordinary operations. Preserve the wrapped proposed key, clone, cache and recovery marker for private inspection; never discard a possibly published key or hand-push a pending seed. Checkout restoration requires proved-absent publication and validated ownership."
        - "These recovery checks cover seed, force-seed, add/update, remove and whole-identity rotation."
        - "This is the one sanctioned exception to pull-not-push: it must run on the machine holding the plaintext. Every step afterwards is a pull."
    - id: add_rotate
      name: Add or rotate a fleet credential
      keywords: [add secret, new credential, rotate, update value, changed token]
      goal: "Put a credential into the fleet, or change its value."
      operation: >-
        secrets-kit add <name> --file <path> --dest '${VAR}/rel/path'
        [--mode 0600] [--newline lf] [--profile <p>] [--doc <pointer>]
        [--update] [--allow-tracked-dest]
      steps:
        - n: 1
          action: >-
            Run `secrets-kit add <name> --file <path> --dest '${VAR}/rel/path'`
            (add `--update` to rotate an existing name's value). No
            passphrase, no terminal -- this is public-key encryption and is
            agent-runnable.
        - n: 2
          action: >-
            Read the result: success writes the encrypted blob and updates
            the manifest; a refused destination prints the exact fix (see
            the destinations reference below for the full guard procedure).
            Every other machine converges on its own next session -- there
            is nothing further to run there.
      gotchas:
        - "AGENT-RUNNABLE: public-key encryption, no passphrase, no terminal. This is the routine path."
        - "Add/update require a clean admitted checkout and prepare outputs privately before one exact publication, without automatic rebase or a second push. Unsubmitted work or validated rejection restores the owned entry state. Uncertainty retains recovery and blocks ordinary operations: preserve clone, cache and recovery evidence for private reconciliation. Add/update never rewrite the identity cache."
        - "Entry authoring uses the fixed direct `blobs/<entry-or-source>.age` slot required by the secrets-repo hook allowlist; there is no alternate layout setting. Nested, absolute, backslash, non-`.age`, and non-`blobs/` selections are refused before encryption or publication, so changing the slot requires a reviewed hook and manifest contract change."
        - "`--update` is the rotation path (keeps the blob filename and dest). Adding a name that exists without --update refuses, so a typo cannot silently clobber a different secret."
        - "`--newline lf` ASSERTS the plaintext has no CRLF and fails if it does. Use it for ssh keys and tokens: a CRLF-seeded key breaks the consumer in ways that are miserable to diagnose later."
        - "`--dest` supports ${VAR} and ~. Variables resolve from secrets.json (machine block first, then global, then the environment). An unresolvable variable is a hard failure, never a literal path."
        - "A non-secret file a consumer still requires (e.g. an ssh known_hosts pin every consuming script passes to ssh) is a fleet entry like any other -- do not treat 'not a credential' as out of scope. Omitting it leaves a freshly-unlocked machine holding every credential and still unable to connect."
        - "Every other machine converges on its next session. There is nothing to run there."

    - id: choosing_a_dest
      name: Choose a --dest, or diagnose a destination write, for an entry
      keywords: [dest, destination, where does this go, collection directory, --allow-tracked-dest, tracked working tree, gitignore, materialize path, atomic write, fsync, temp file, write failed, permission, mode]
      goal: "Decide where a new entry's dest should point, and diagnose a destination-write failure, without loading the full procedure for routine work."
      operation: "n/a -- policy, applied when choosing --dest for add_rotate, or when a materialized write needs diagnosis"
      steps:
        - n: 1
          action: >-
            Default: materialize at the path the consumer already reads.
            Only fall back to a per-repo collection directory (then teach
            the consumer that path, via a copy step, symlink, or config
            option) when the consumer cannot accept an arbitrary path --
            e.g. a build tool reading a fixed filename adjacent to its
            input. If the resolved dest falls inside a git working tree,
            `add` refuses unless the path is gitignored -- a hard failure,
            not a warning. For choosing between the two, the full override
            scope, per-machine resolution, convergence check ordering, and
            exposure remediation, load `references/destinations.md`.
        - n: 2
          action: >-
            If a write to an already-chosen dest fails, or its resulting
            mode or permissions look wrong, do not assume corruption or a
            bug before reading the write protocol: load
            `references/destinations.md` for the exact write sequence, its
            implementation pointer, and its documented limits.
      gotchas:
        - "This guard is the essential invariant even without loading the reference: an unignored tracked dest is refused at add-time, not merely warned about."
        - "Destination-write invariant, also true without loading the reference: the destination's mode is established before any plaintext touches disk, via a same-directory temporary file that is fsynced then atomically renamed into place -- so a partially written file is never visible at the final path. This is not a power-loss guarantee and it does not defend against a concurrent substitution in an untrusted parent directory."
    - id: remove
      name: Remove a fleet credential
      keywords: [remove secret, delete credential, stop distributing]
      goal: "Stop distributing a credential; delete it everywhere."
      operation: "secrets-kit remove <name>"
      steps:
        - n: 1
          action: >-
            Run `secrets-kit remove <name>`. Machines delete their local
            copy on the next pass (the orphan sweep) -- removal genuinely
            propagates, it is not just a manifest edit.
      gotchas:
        - "The ciphertext remains in git history forever. If the VALUE is now considered exposed, rotate the underlying credential; see the deleting_a_blob_is_not_revocation invariant."
    - id: rotate_identity
      name: Rotate the fleet identity
      keywords: [rotate identity, lost machine, stolen laptop, revoke, new passphrase, compromised]
      goal: "Replace the fleet keypair and re-encrypt every blob."
      operation: "secrets-kit rotate-identity --new-terminal   (YOU run this, after asking)"
      steps:
        - n: 1
          action: >-
            Must run on an UNLOCKED machine -- it decrypts every blob to
            re-encrypt it. Verify with `secrets-kit status` first.
        - n: 2
          action: >-
            Ask the user for consent, then run
            `secrets-kit rotate-identity --new-terminal` yourself.
        - n: 3
          action: >-
            After it completes, every OTHER machine hits one decrypt failure and
            needs `secrets-kit unlock --new-terminal` again. Tell the user that
            up front so the wave of asks is expected rather than alarming.
        - n: 4
          action: >-
            Also remove the lost machine's SSH key from the git host, so it
            cannot fetch new ciphertext at all.
      gotchas:
        - "This stops the old identity reading FUTURE blobs. It does not un-read the past. For a lost machine, ALSO rotate the underlying credentials -- that is the real revocation, because the plaintext was already on that box."
        - "Rotation publishes one exact commit with no automatic rebase and no second push, and caches the new identity only after that publication is established. A rejection -- including a remote that moved while rotation was preparing -- restores the checkout and leaves the existing cache readable."
        - "An unproved publication retains encrypted recovery evidence and blocks ordinary operations, exactly as seed and entry authoring do. That block is protective: the local blobs are already re-encrypted to the new recipient, so re-running rotation would work against ciphertext the cached identity can no longer open. Preserve the recovery directory, clone and cache for private inspection."
        - "Every entry must occupy its reserved blobs/<name>.age slot. Rotation refuses a manifest naming any other layout BEFORE it generates a key, because it re-encrypts whatever the manifest names."
  anti_patterns:
    - id: hand_copying_secrets
      name: Copying secret files between machines by hand
      keywords: [scp the token, copy the file, just paste it, per-machine drift]
      why_it_seems_right: "The machine needs the credential now and copying one file is faster than any of this."
      why_it_is_wrong: "It is exactly the per-machine drift this plugin exists to end: invisible to every other machine, unrecorded, and stale the moment the value rotates."
      alternative: "Add it to the fleet (`secrets-kit add`) and let the machine pull it. If something is genuinely urgent, do the copy but LABEL it a stopgap and file the real fix -- do not let it read as the solution."
    - id: hand_resolving_a_rejected_push
      name: Making a rejected push go through
      keywords: [push rejected, fetch first, diverged, force push, merge the secrets repo, resolve and push]
      why_it_seems_right: "It is the reflex for any git repo, and the rejection looks like ordinary branch drift."
      why_it_is_wrong: "In THIS repo a rejected push usually means the remote already holds something the local clone never saw -- most often an identity.age seeded elsewhere. Forcing or merging past that replaces the fleet's key with a second one, orphaning every blob encrypted to the first. The rejection is the safety net, not the problem."
      alternative: "Preserve the clone, cache, wrapped proposed key and recovery marker for private inspection while publication or recovery remains unresolved. Do not push, merge, reset or delete recovery artifacts to make the attempt go through. Local upstream tracking information cannot prove publication absent; checkout restoration requires proved-absent publication and validated ownership."
    - id: asking_for_the_passphrase
      name: Offering to set the passphrase for the user
      keywords: [paste the passphrase, i will set it, transcript, convenience]
      why_it_seems_right: "It is the same shape as the API-key flow, which does offer a paste-it-here option."
      why_it_is_wrong: "An API key is one revocable service credential; this is the master key to every credential in the fleet. The transcript is a file on disk, so pasting it there is permanent exposure of the root of trust."
      alternative: "Follow passphrase_verbs_need_new_terminal and cli_is_not_on_path: ask for consent, resolve the shim and run unlock with --new-terminal. The user enters the passphrase only in that terminal window, never in chat."
```

```yaml
references:
  - id: destinations
    path: references/destinations.md
    keywords: [dest, destination, --dest, --allow-tracked-dest, tracked working tree, gitignore, materialize path, collection directory, exposure remediation, atomic write, fsync, temp file, write protocol]
    summary: >-
      Full --dest selection procedure -- default vs. collection-directory
      fallback, the tracked-working-tree guard and its --allow-tracked-dest
      override and scope, per-machine variable resolution, convergence
      check ordering, and exposure remediation for an already-materialized
      dest -- plus the destination-write protocol (mode-before-write,
      same-directory temp file, fsync, atomic replace), its implementation
      pointer, and its documented limits.
```

## The pre-commit guard

Every authoring verb calls `guard.require_guard()` before it writes: it installs
`.git/hooks/pre-commit` into the clone (idempotently, upgrading an older
version) and **refuses to proceed if the clone ends up unguarded**. There is no
"remember to set this up" step, because `.git/hooks` is untracked -- the guard
cannot ship inside the secrets repo and so has to be established locally on
every machine, every time.

The hook is **copied, not sourced**, because the plugin's cache path is
version-keyed and moves on every version bump -- a sourced hook would point at
a directory that stops existing the moment the plugin updates.

It is an **allowlist**: only `manifest.json`, `identity.age`, `blobs/*.age`, and
`README.md` / `.gitignore` / `.gitattributes` may be committed, blobs and the
identity must be real age ciphertext, and an unwrapped `AGE-SECRET-KEY-` is
refused anywhere including inside otherwise-allowed files. A denylist would have
to anticipate every shape a secret can take and would lose to the first one it
did not; this fails closed, so the worst case is an annoyed human.

It inspects the **index**, not the working tree -- otherwise staging plaintext
and then tidying the worktree would sail straight through.

A seeded repo also gets a deny-by-default `.gitignore`, so the careless
`git add -A` never even stages a stray plaintext file. Two independent nets,
because what they prevent cannot be undone: a plaintext credential pushed once
survives in the object store, in every clone, and in any fork or backup taken
meanwhile. Rewriting history does not fix it -- rotating the credential does.

## Where things live

| Thing | Path | Notes |
|---|---|---|
| Machine declaration | `~/.claude/secrets.json` | Private, tracked in claude-settings. Repo URL, vars, machines -> profiles. |
| Secrets repo clone | `<data_dir>/repo` | Fetched at most once per 6h unless `--refresh`. |
| Unlocked identity | `<data_dir>/identity.txt` | 0600 / owner-only ACL. Never in a git tree. Never expires. |
| Convergence state | `<data_dir>/state.json` | Blob + plaintext hashes per entry. A cache; safe to delete. |

`<data_dir>` is `~/.claude/plugins/data/plugins-kit/secrets-kit/`.

## Why the steady state is free

`state.json` records the sha256 of both the blob and the materialized
plaintext. If both still match, the pass decrypts **nothing** -- it is a stat
plus a hash of a few small local files. Hashing the destination is what makes
local deletion, truncation, and tampering self-healing rather than a failure
mode: any interrupted pass simply converges the remainder next session.

## secrets.json machine keys reference env.json

They must match the `machines` registry in `env.json`, which stays the single
machine list. secrets-kit cross-checks and raises an ASK on a mismatch rather
than guessing -- a machine name that exists in only one of the two files is a
typo with consequences, not a new machine.
