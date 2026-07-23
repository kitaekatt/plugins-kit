# Design Discipline

The design philosophy behind `content_pipeline`. Every principle here is
**opt-in**: the library ships each as a component a pipeline chooses to wire
in, never as a rule the library enforces on every pipeline. A minimal
pipeline -- read stale units, regenerate, write -- is buildable with none of
them. The point of the library is that a new pipeline can be built without
reinventing these disciplines, not that every pipeline must carry every one.

Each entry states the principle, why it exists (one short paragraph), and the
library component that embodies it. Read this reference when you are deciding
which guardrails a pipeline should adopt, or when you are extending the
library and want a new mechanism to match its grain.

## Human-always-wins attribution

**Principle.** A human correction is never silently overwritten by a
subsequent machine regeneration. When a field carries both a machine value
and a human value, the human value is the effective value -- always, by
construction.

*Why.* This is the single rule every other do-no-harm mechanism exists to
support. Batch regeneration is only safe to run repeatedly if a human who
fixed an output can trust that the next run will not undo the fix. Make that
guarantee structural and the whole pipeline becomes safe to re-run on a whim;
leave it to careful coding and every regeneration is a gamble.

*Embodied by.* `store.attributed` -- `effective_value` and `AttributedField`
resolve `human > machine > sourced` by fixed precedence, and the field is
frozen (a correction produces a new instance). A regeneration writes only the
`machine` slice, so a populated `human` slice cannot be lost by omission.

## Do-no-harm ownership markers + first-class revert

**Principle.** When a pipeline mutates authored content in place, every
machine-written region carries an ownership marker, and a populated region
WITHOUT the marker is treated as human-owned and never touched. Reverting the
pipeline's writes is a first-class operation, not a manual cleanup.

*Why.* In-place mutation shares one artifact between the pipeline and human
authors. The marker is what lets a later pass tell "I wrote this, I may rewrite
it" from "a human took this over, hands off" -- without it, in-place delivery
cannot be safe. And because a run is only trustworthy if it is undoable, revert
must be as first-class as apply.

*Embodied by.* `deliver.inplace` -- `Marker` / `classify_ownership` (a present,
unmarked row classifies HUMAN), `apply_inplace` (skips human-owned rows), and
`revert_marked` (strips marker + value on exactly the marked rows).

## Write-only-on-diff

**Principle.** A pass writes an output only when it actually differs from what
is already on disk. Regenerating identical content is not a write.

*Why.* Writing unchanged content defeats freshness tracking (every write looks
like a change to anything watching mtimes or VCS status) and floods version
control with no-op diffs that bury the real changes. Comparing a content hash
before writing keeps the write set equal to the genuine change set.

*Embodied by.* `freshness.ensure.ensure` -- regenerate in memory, compare
content hashes, write only on a real change (with the VCS open-for-edit hook
fired only on the write path). `store.intermediary.ensure_intermediary`
applies the same idea to the synthesized anchor slice.

## One-predicate freshness

**Principle.** Every "does this need regeneration?" decision, and every
coverage-bucket tally, delegates to one staleness predicate. There is not a
second, subtly-different staleness check anywhere in the pipeline.

*Why.* When "needs regen" and "coverage says stale" are computed by two
different code paths, they drift, and the coverage report starts lying about
what the next run will do. Routing both through one predicate makes the report
and the regen set provably consistent -- they cannot disagree because they are
the same computation.

*Embodied by.* `freshness.classify` -- `classify` (the single
`HUMAN > EXCLUDED > MISSING > STALE > FRESH` predicate), `needs_generation`,
and `bucket_counts` all read the same states.

## One-rule-set-many-sites validation

**Principle.** The rules an agent checks while generating are the exact same
rules the audit checks afterward. A rule cannot exist at one site and not the
other.

*Why.* If the in-loop generation validators and the post-hoc audit validators
are separate lists, they drift: the agent passes a check the audit later fails,
or the audit enforces a rule the generator never saw. Sharing one `Validator`
list across both sites makes "valid during generation" and "valid during audit"
mean identically the same thing.

*Embodied by.* `validate.contract` -- one `Validator` protocol and `run_rules`
feed both `llm.submit_validated` (in-loop) and `audit` (post-hoc); `Severity`
tiers (`HARD` / `SOFT` / `ADVISORY`) and the shared `is_rejecting` predicate
give one accept/reject decision everywhere.

## Advisory floor guards on a known-good corpus

**Principle.** A quality diagnostic that flags suspicious output is admitted
only after it is gated against a known-good corpus: it may flag strictly fewer
than 10% of known-good items (the default `<0.10`, strict). A guard that flags
more is a bad signal and does not ship. Even an admitted guard only flags for
human review -- it never hard-blocks.

*Why.* A heuristic that disagrees with work already judged good is noise, and
noise that blocks acceptance is worse than no signal at all. Calibrating each
guard independently against known-good output keeps only signals that
correlate with real defects, and keeping them advisory means a false flag
costs a glance, not a blocked pipeline.

*Embodied by.* `validate.floor_guard` -- `evaluate_guards` gates each named
guard per-signal on `DEFAULT_THRESHOLD` (0.10, strict `<`); `flag` surfaces
flagged items for review; the tier is `Severity.ADVISORY`, which never blocks.

## Deterministic seeding of stochastic gating

**Principle.** Any stochastic decision inside the pipeline is seeded
deterministically from stable entity identity -- never from run-local state or
wall-clock time.

*Why.* A random decision seeded from run-local entropy changes every run, and
if that decision feeds a freshness hash, the entity re-stales perpetually --
the pipeline never converges. Seeding from stable identity means the same
entity makes the same "random" choice every run, so a flag flip elsewhere
cannot churn an unrelated hash.

*Embodied by.* `freshness.seed` -- `deterministic_seed` / `seeded_random`
derive the seed from the entity id; `pipeline.single_pass.seed_for` threads it
into a stage's stochastic decision.

## Exact-path, never-wildcard VCS

**Principle.** A changeset contains exactly the files a run touched, named
explicitly. A pipeline never wildcard-adds a directory into its changeset.

*Why.* A wildcard add sweeps in whatever else happens to be dirty in the
working tree -- another process's in-flight edits, unrelated local changes --
and silently attaches them to the pipeline's changeset. Naming the exact paths
the run produced keeps the changeset an honest record of the run and nothing
else.

*Embodied by.* `vcs` + `deliver` -- `git_vcs.move_into` is `git add` of exact
paths only; `deliver.deliver_changeset` builds the changeset from precisely the
items whose write succeeded, and rebuilds the description from that same moved
subset so the message never claims a file that did not land.

## Truthful audit stamping

**Principle.** What a run records about itself is exactly what happened. The
changeset description lists only the items that actually landed; an audit
finding is the runtime's own verdict, not a second rule set that could
disagree; a substituted model id on a record is the model that actually ran.

*Why.* Records that overstate a run -- a description claiming items that failed
to move, an audit using stricter rules than the runtime, a logged model that
was silently swapped -- turn the audit trail into fiction, and a fiction is
worse than no record because it is trusted. Deriving every stamp from the real
outcome keeps the trail load-bearing.

*Embodied by.* `deliver.deliver_changeset` (description rebuilt from the moved
subset), `audit.auditor.AuditSpec` (the audit reuses the runtime's own policy /
marker / projection classifiers, so a finding is by construction the runtime's
judgment), and `llm.backends.routed_model` (a substituted id is what lands on
the response and therefore on the audit record).

## Altitude discipline (don't force the wrong granularity)

**Principle.** Understanding that applies to a whole entity enters the prompt
as context, at entity granularity; signal that distinguishes one item's
candidate from another enters through selection, at item granularity. Do not
force entity-level knowledge down into per-item machinery, nor push per-item
grading up to the entity.

*Why.* Modelling every signal at the same altitude collapses two genuinely
different jobs. Entity-level context ("what is this whole thing about") is the
same for every item under it and belongs in a source-tier provider computed
once; item-level discrimination ("which of these candidates is best here") is
per-item and belongs in the candidate grade/select. Forcing one to live at the
other's altitude either recomputes shared context per item (wasteful) or
smears per-item judgment across a whole entity (imprecise).

*Embodied by.* `providers` source tier (unit-agnostic, entity-level context
computed once) versus `store.candidate` + the convergence-loop select stage
(per-item grading and selection). The two tiers in `freshness` and `providers`
exist precisely to keep the altitudes separate.

## When to adopt which

None of the above is required. A fully-automated, single-owner pipeline that
writes projection artifacts might adopt only human-always-wins (free -- it is
the store's default), write-only-on-diff, and one-predicate freshness, and
skip markers, floor guards, round-trip, and audit entirely. Adopt a discipline
when its failure mode is one your pipeline can actually hit: markers and revert
when humans share the artifact; floor guards when you have a known-good corpus
and want a review signal; deterministic seeding only if a stage is stochastic;
truthful audit stamping whenever a record of the run is consumed by anyone.
Each is a tool in the box, not a checklist to complete.
