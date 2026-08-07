# secrets-kit: `--dest` has no choice policy, and no guard against a tracked working tree

- **Plugin:** secrets-kit 0.7.1
- **Reported:** 2026-08-07
- **Severity:** one docs gap; one latent credential-disclosure bug

## Summary

Found while adding a new fleet entry whose consumer can only read its secret from
inside a git-tracked source directory.

1. **(docs)** The skill documents `--dest` mechanics but never says how to *choose*
   a destination, so the choice reads as a host-repo convention question and an
   agent stops to ask the user.
2. **(bug)** `add` and the convergence pass will materialize plaintext into a
   tracked git working tree with no check and no warning.

## Finding 1 -- no destination-choice policy

secrets-kit exposes one skill, `secrets-kit:secrets-kit` (a single ~315-line
SKILL.md, no `references/`). Discoverability is fine: an agent finds and loads it
unprompted. The gap is content.

Every mention of `--dest` in SKILL.md and README.md is mechanics:

- `${VAR}` and `~` expand
- resolution order is machine block -> global -> environment
- an unresolvable variable is a hard failure, never a literal path

Nothing states whether a secret should materialize:

- **(a)** at the path its consumer already reads, or
- **(b)** into one per-repo collection directory that the consumer is then taught
  about (by a copy step, a symlink, or a config option).

Existing entries all happen to be (b), because their consumers accept a
configurable path. A consumer that can only do (a) -- a build tool that reads a
fixed filename adjacent to its input -- has no rule to apply. The tool should
answer this rather than leaving it to per-repo taste.

**Suggested rule:** default to (a), materialize where the consumer already reads.
It removes the copy step and the second working copy that drifts from source.
Keep (b) available, with the tradeoff named, for consumers that accept a path.

## Finding 2 -- no guard when the destination is inside a tracked repo

This is the actionable one.

The pre-commit guard is installed into the **secrets repo** and protects the
blobs. It does not, and structurally cannot, protect a **consumer repo**
receiving plaintext. So the one case that can push a credential into public
history is the case with neither a rule nor a check:

- `add --dest` accepts a path inside a tracked working tree
- the convergence pass rewrites plaintext there every session
- nothing verifies the path is gitignored
- nothing warns, at add time or at convergence

Existing entries land in an already-gitignored directory, so this has never
fired. It is latent, not absent -- and finding 1 makes it *more* likely to fire,
since the missing rule is exactly what pushes an agent toward an in-tree dest.

Note the asymmetry. The project already takes a deliberate fail-closed,
two-independent-nets posture on the secrets repo (allowlist pre-commit hook plus
a deny-by-default `.gitignore`), on the stated grounds that "a plaintext
credential pushed once survives in the object store, in every clone, and in any
fork or backup taken meanwhile. Rewriting history does not fix it." The consumer
side has the same irreversible outcome and zero nets.

**Suggested fix:** on `add`, resolve the dest; if it falls inside a git working
tree, check it is ignored (`git check-ignore -q`). If it is not, refuse and print
the exact `.gitignore` line that would fix it, with an explicit override flag for
the intentional case. Re-check at convergence, degraded to a warning, to catch a
`.gitignore` that changes after the entry was created.

Refuse rather than warn: a warning emitted during a session-start convergence
pass is precisely the output nobody reads.

### Repro

1. In any git repo, pick a path that is tracked, or simply not gitignored.
2. `secrets-kit add <name> --file <plaintext> --dest '<that path>'`
3. Observe: accepted, no warning. The next convergence writes plaintext there,
   and a routine `git add -A` stages it.

## Minor, same capability

Nothing says what to do about a **non-secret file that a consumer nonetheless
requires** -- for example an ssh `known_hosts` pin that every consuming script
passes to `ssh`. Under the rule proposed in finding 1 it is a fleet entry like
any other, and omitting it leaves a freshly-unlocked machine holding every
credential and still unable to connect. Without the rule, it reads as out of
scope. Worth one line in the `add` capability.
