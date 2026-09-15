# Choosing and guarding a destination

Full procedure for choosing `--dest` on `secrets-kit add`/`--update`, the
tracked-working-tree guard, its override, exposure remediation, and the
protocol that actually writes plaintext to the destination.

## Choosing the destination

1. **Default: materialize at the path the consumer already reads.** It
   removes a copy step and a second working copy that can drift from the
   fleet-managed one. Only fall back to a per-repo collection directory
   (then teach the consumer that path, via a copy step, symlink, or config
   option) when the consumer cannot accept an arbitrary path -- e.g. a build
   tool reading a fixed filename adjacent to its input.

2. **The tracked-working-tree guard.** If the resolved dest falls inside a
   git working tree, `add` refuses unless the path is gitignored, and prints
   the exact `.gitignore` line that would fix it. This is a hard failure, not
   a warning. Where the check cannot be run -- git unavailable, or a dest
   naming a variable this machine does not resolve (a legitimate
   cross-machine or per-OS entry) -- `add` notes the skip and proceeds;
   convergence checks it on each machine. Convergence separates the two
   causes: git being unavailable is systemic and reported plainly, while git
   being present and answering unreadably is reported as an ANOMALY naming
   the secret, the dest, and git's raw output -- the guard did not run and
   the destination needs checking by hand.

3. **The intentional case: `--allow-tracked-dest`.** For the case where the
   dest belongs inside a tracked repo and will stay ignored there (e.g. a
   per-repo secrets directory covered by a pattern), pass
   `--allow-tracked-dest`. The override is persisted, so it is not re-typed
   on every `--update`. It is scoped to the entry AND the destination it was
   granted for, not to the entry alone: an `--update` that moves the entry to
   a different `--dest` does NOT inherit it, the check runs against the new
   path, and granting consent there means passing `--allow-tracked-dest`
   again.

4. **Convergence re-check ordering.** Convergence re-runs the same check on
   every session, honouring a persisted `--allow-tracked-dest`, and runs it
   BEFORE the unchanged-content fast path so an exposed entry is reported
   every pass rather than going quiet once it has settled. If the dest is
   inside a work tree and unignored it is recorded as a failure and
   surfaced; other entries still converge normally. Two cases, with
   different remediation: a pending write is WITHHELD, while a dest already
   materialized is reported and left alone -- nothing is rewritten or
   deleted, because the plaintext is already on disk and removing a file the
   user may depend on is not secrets-kit's call. The already-materialized
   message also names the `git rm --cached` to untrack it, and says that a
   value ever committed must be rotated: deleting it from the tree is not
   revocation.

## The destination-write protocol

Once a dest is chosen and passes the guard above, the entry's plaintext is
written to it by this sequence, in order:

1. The destination's mode is established **before** any plaintext is
   written: a sibling temporary file is created in the destination's own
   directory (not a shared temp directory), and tightened to the entry's
   requested mode immediately, before the producer writes a byte.
2. The plaintext is written to that temporary file.
3. On success the file is flushed and `fsync`ed, then atomically renamed
   (`os.replace`) into the final destination path. Decrypted material is
   therefore never visible at the final path in a loose mode, and never
   partially written at the final path.
4. On failure, the temporary file is unlinked and the final destination slot
   is left untouched -- nothing partially written ever displaces an existing
   file.

Implementation pointer (verified against the source, not guessed): the
sequence above is `secrets_kit.converge._atomic_write`
(`plugins/secrets-kit/lib/secrets_kit/converge.py:868`), which delegates the
allocate/write/fsync/replace mechanics to
`secrets_kit.perms._private_output`
(`plugins/secrets-kit/lib/secrets_kit/perms.py:141`).

**Documented limits -- do not read more into this than the code provides:**

- The mode applied is the **caller-supplied mode**, not an unconditional
  0600 -- `add --mode` (default 0600) sets what gets tightened.
- This is **not power-loss recovery**. `perms._private_output` fsyncs the
  file's own contents before the rename, but does not fsync the parent
  directory, so a crash at the wrong instant can still leave the rename
  itself unpersisted on some filesystems.
- It does **not defend against a concurrent substitution in an untrusted
  parent directory** -- the guarantee assumes the destination's parent
  directory is not adversarially writable by another actor at the same
  time.
- On Windows the mode is applied as an owner-only ACL rather than a POSIX
  bit. That path is **accepted, not independently verified** -- there is no
  native-Windows certification here or elsewhere in this reference.

## Gotchas

- The convergence re-check is not redundant with the add-time check:
  add-time can only validate the authoring machine's variable resolution,
  and a dest can be per-OS or per-machine, so it may be ignored where it was
  added and land tracked on a machine that resolves the variable
  differently. It also catches a `.gitignore` that changes after the entry
  was created.
- This mirrors the secrets repo's own posture: an allowlist pre-commit hook
  plus a deny-by-default `.gitignore`, because a plaintext credential pushed
  once survives in the object store, in every clone, and in any fork or
  backup taken meanwhile -- rewriting history does not fix it. A consumer
  repo has the same irreversible outcome; this guard is its first net.
