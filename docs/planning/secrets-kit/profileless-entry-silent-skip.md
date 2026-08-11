# secrets-kit: an entry added without `--profile` is silently never delivered

- **Plugin:** secrets-kit 0.8.1
- **Reported:** 2026-08-11
- **Severity:** silent no-op; the entry looks correct everywhere you would think to check

## Summary

`add` without `--profile` writes the blob, records the entry in `manifest.json`,
commits and pushes -- and produces something **no machine will ever receive**,
with no warning at add time and no mention at convergence.

Entitlement is by profile: `manifest.profiles` maps a profile name to a list of
entry names, and a machine receives the union of the profiles it is assigned in
`secrets.json`. An entry in **no** profile is in no machine's union, so it is
skipped. Nothing says so.

## Why it is hard to spot

Every surface an operator would check looks healthy:

- `add` prints its normal success line (`added '<name>' -> blobs/<name>.age`).
- The entry is present in `manifest.json` with the correct `dest`, `mode`,
  `newline` and `doc`.
- The blob exists in `blobs/` and the commit is in the repo's log.
- `status` prints `N ok, 0 written, 0 failed` -- **with no failure and no
  mention of the skipped entry**.

The only observable symptom is that `status`'s total is one lower than the
number of entries in the manifest, and nothing invites that comparison. In
practice this was diagnosed only by dumping `manifest.profiles` by hand and
noticing its lists summed to exactly the `ok` count.

Worse, the failure is invisible in the case that matters most. If the
destination happens to already exist -- a hand-placed copy, or a file the
consumer wrote earlier -- everything works locally and the entry appears fine
forever. The gap only surfaces on a *different* machine, which is precisely the
drift this plugin exists to eliminate.

## Repro

1. `secrets-kit add <name> --file <plaintext> --dest '<some path>'` (no `--profile`).
2. Delete the destination file.
3. `secrets-kit status --refresh`.
4. Observe: `N ok, 0 written, 0 failed`, and the destination is still absent.
   No error, no warning, no mention of `<name>`.

## Suggested fixes

Any one of these closes it; the first is the cheapest.

1. **Warn at add time.** If no `--profile` was given and the manifest defines at
   least one profile, print a prominent warning naming the consequence ("this
   entry is in no profile and will not be delivered to any machine"). A repo
   that uses no profiles at all should stay silent, so the warning does not fire
   on the simple single-profile-less setup.
2. **Require the choice.** With profiles defined, make `--profile` mandatory and
   offer an explicit `--no-profile` for the deliberate case (staging an entry
   before deciding entitlement).
3. **Account for skips in `status`.** Print `N ok, ... , M not entitled` (or list
   them) so the total always reconciles against the manifest. This is worth
   doing regardless of 1 and 2: it turns the one observable symptom into a
   statement instead of a subtraction the operator has to perform.

## Related

Same authoring path as the dest-policy findings in
`dest-policy-and-tracked-tree-guard.md` (both fixed in 0.8.0). The shape is the
same one that report described: `add` accepts something under-specified and the
consequence lands far away from the command that caused it, so the guidance and
the guard belong at the point of authoring.
