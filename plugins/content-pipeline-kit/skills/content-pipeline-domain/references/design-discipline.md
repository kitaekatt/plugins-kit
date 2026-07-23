# Design Discipline

> **Status: skeleton -- to be expanded.** This reference is a structural
> outline drawn from the plugin proposal; each section carries only the
> 2-4 sentences the proposal already established. Every principle here is
> guidance the library makes available as opt-in components -- never a rule
> the library enforces on every pipeline. A minimal pipeline is buildable
> with none of them wired in.

## Floor-first

Establish a known-good floor -- a small fixture set representative of
acceptable output -- before building generation logic against it. The floor
is what `validate.floor_guard` compares new candidates against; without a
floor fixture, "is this candidate acceptable" has no reference point.

## Better-informing, not forcing

Guardrails exist to inform a generation or selection decision with more
signal, not to force a specific outcome. A floor guard flags; it does not
block. A validator's advisory tier surfaces information; only the hard tier
blocks acceptance. Design new guardrails to default toward informing.

## The known-good <10% floor-rule gate

When a floor guard is registered, the acceptance band is deliberately tight
-- within 10% of the known-good fixture's metric. A wider band defeats the
purpose of having a floor at all; a pipeline that finds 10% too strict for
its content should widen deliberately and document why, not silently drift.

## Do-no-harm boundary

The do-no-harm boundary is baked into the data model (`store.attributed`'s
human-always-wins precedence), not enforced as a runtime check a caller
could forget to run. This is why the boundary is called structural rather
than procedural -- it cannot be skipped by omission.

## Write-only-on-diff

A delivery pass should only write output that actually differs from what is
already there. Writing identical content on every pass defeats freshness
tracking (every write looks like a change to anything watching mtimes or
VCS status) and creates VCS noise with no informational content.

## Human-always-wins

Restated from the attributed-store vocabulary: a human correction is never
silently overwritten by a subsequent machine regeneration. This is the
single rule every other do-no-harm mechanism (markers, revert, projection
rollback) exists to support.

## Exact-file-set-never-sweep

A VCS changeset should contain exactly the files a pipeline run touched --
never a wildcard sweep that could catch files another concurrent process
has open. This is the same discipline `vcs.seam`'s `move_into` choreography
exists to enforce structurally rather than leaving it to caller discipline.

## Deterministic seeding

Any stochastic decision inside the pipeline (see `freshness.seed`) is seeded
deterministically from stable entity identity, not from run-local state or
wall-clock time. A flag flip elsewhere in the pipeline must not perpetually
invalidate a hash that depends on the seed.
