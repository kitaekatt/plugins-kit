# The plugin-opinion razor

Auditable criteria for deciding whether a plugin in this marketplace is entitled to
impose a workflow opinion on the developers who install it.

The one-line rule, and the summary register of stances this marketplace has chosen to
impose, live in the root [CLAUDE.md](../../CLAUDE.md). This document is the detail: the
numbered criteria, how to detect each one, and worked examples in both directions.

## The vision

**The default is awesome and opinionated. Configurability is earned.**

These plugins exist to expose powerful customizations that let a user produce their best
experience. That goal is not served by making everything a knob -- an option nobody needs is
a worse default, a larger surface to document, and a decision pushed onto someone who did
not want to make it. So a plugin holds its opinions confidently, and a setting appears only
where the opinion demonstrably costs a real user something.

The plugins here are published, so every opinion is inherited by users we have never met.
That is fine. The failure is not "has an opinion" -- it is holding an opinion that drives a
real user to fight the plugin or abandon it.

## Vocabulary

**Workflow opinion** -- an assumption about how the user works, rather than something
intrinsic to the plugin's job. Branch names, directory layouts, which VCS, whether a file
must be committed, review rosters, model routing, thresholds, cadences.

**Intrinsic assumption** -- something the plugin cannot do its job without. A Perforce
plugin assuming Perforce is intrinsic; a Python plugin assuming Python is intrinsic.
Intrinsic assumptions are outside this razor entirely.

## The razor

> **Can I articulate ONE SERIOUS, or TWO DISTINCT, user-preference scenarios in which this
> not being configurable leaves the user needing or wanting to uninstall the plugin, or to
> take remedial action against the default behaviour?**

PASSES -> it must become configurable, with the opinionated default preserved so nothing
changes for anyone who was happy.

FAILS -> leave it hardcoded. That is a correct, finished outcome, not a deferred TODO. Do
not open an issue for it.

### What counts as a scenario

The audience is **Claude Code power users**. Scenarios must be grounded in preferences such
a user realistically holds -- not hypothetical organisations, not "someone might".

A scenario must survive three checks:

1. **Real audience.** Can you picture a Claude Code power user actually having this
   preference? "A team with a compliance department" is not this audience. "A user close to
   their weekly usage limit" is.
2. **Real cost.** The consequence is uninstalling, forking, or repeated manual remediation.
   A single self-explaining error message that tells you exactly what to pass instead is
   friction, not a scenario.
3. **Distinct, if you are relying on two.** Two phrasings of the same underlying preference
   count once. Two genuinely different users wanting different things count twice.

One scenario suffices only when it is SERIOUS: the user's realistic response is to stop
using the plugin, or to work around it every single time.

### Why this replaced the earlier form

An earlier draft asked "would a competent team reasonably do this differently?" That
over-generates: the answer is yes for almost any decision, which would have turned every
opinion into a knob and produced exactly the sprawl the vision rejects. The scenario test
has an evidentiary bar -- you must name the user and the cost -- so it can return NO, and
in practice it does for roughly a third of candidates (see the findings table).

## Criteria

### OP-1 -- No maintainer-only material on the published surface

Everything under `plugins/<name>/` is copied into a consumer's plugin cache. A file that
only means something inside plugins-kit does not belong there.

**Detect:** for each file under a published plugin, ask *who reads this on a machine that
is not ours?* Generated baselines, fingerprints, our design history, and build plumbing
all answer "nobody". A strong tell is a file whose own header names a `scripts/` tool the
consumer does not have.

**Satisfied by:** moving it to `docs/`, `scripts/`, or a task folder.

**Worked example (real, remediated 2026-08-08).** The orchestrate skill shipped
`references/decision-fingerprint.txt`, a sha256 baseline whose header instructed the
reader to regenerate it with `uv run python scripts/check_orchestration_drift.py --update`
-- a script absent from every consumer install. Alongside it,
`references/orchestrate-2.0-design.md` (our drift check, clean-room derivation rounds,
"Remaining work") was linked from SKILL.md as though it were guidance, and
`references/tier-principles.md` grew from 642 to 1,065 lines when a build step embedded 25
`emits:` blocks of generator plumbing into it.

Note the mechanism: none of that arrived as a decision to publish something. It accreted
inside files that already shipped, because a build step colocated its inputs with its
artifact for convenience. **Colocation is a publishing decision.**

**Remediation.** The colocation reason expired once the decision half became GENERATED
(`scripts/generate_orchestration.py`, compiling the tree from `tier-principles.md` and
`lexicon.md`) rather than hand-written and checked for drift: a fingerprint can no
longer disagree with principles compiled from those same principles, so
`scripts/check_orchestration_drift.py` and `decision-fingerprint.txt` -- the ONLY reason
the two design docs sat inside the policed `references/` directory -- were deleted.
`tier-principles.md` and `orchestrate-2.0-design.md` moved to
`docs/reference/orchestrate/` (outside every published plugin); the SKILL.md links to
both were dropped rather than repointed, since a consumer install has no `docs/` tree.
`lexicon.md` was not moved -- it is genuine vocabulary reference a consumer reads
alongside the rendered tree, not build plumbing -- and stays in the skill's
`references/`.

A later pass (2026-08-10) found `lexicon.md` was not clean, though: alongside its
generator-facing term records it also carried authoring rules (why a term is
`[skill]` vs `[concept]`, bare vs glossed) and per-term derivation annotations citing
`tier-principles.md` criteria by number (P2.1, P0.5) or recording design history (a
demotion, a correction, a retired taxonomy) -- maintainer-only material that had
accreted into a file that does ship, the same failure this criterion names. That
prose was pruned in place into `docs/reference/orchestrate/lexicon-derivation.md`;
`lexicon.md` itself stayed put (`SKILL.md` links it, and a consumer install has no
`docs/` tree to move it into). `generate_orchestration.py --check` before and after
the prune produced a byte-identical `orchestration.yaml`, confirming the removed
prose was never a generator input -- `parse_lexicon` reads only the `###` heading,
the one-line definition, and the `**Test:**` / `**Gloss:**` lines.

**Audit note:** md-domain catches PART of this. A skill's `references/*.md` is claimed
and audited by the `audit_skill` lane, whose SR-4 (reader fit) criterion flags
maintainer-only material on that surface (`skill-standards.md` section 10 -- the criterion
statement lives there, not here). That is one judgment criterion over one MARKDOWN
document. It cannot see a non-markdown artifact at all -- `decision-fingerprint.txt`, the
worked example above, is invisible to every lane -- and it reads nothing outside a skills
tree. The rest of OP-1 must still be checked by hand or by a dedicated sweep, and a clean
md-domain audit does not discharge it.

### OP-2 -- Every workflow opinion has been run through the scenario test

The razor itself, applied per opinion.

**Detect:** enumerate what the plugin decides on the user's behalf. For each, either name
the config key and its default, or state the scenarios you tried and why they failed. An
opinion nobody has tested is the finding -- not the opinion itself.

**Worked example of the test PASSING (two distinct scenarios).** git-kit and p4-kit fix
their reviewer roster and model routing inside SKILL.md: sonnet for compliance, opus for both
bug lanes, opus validators. (1) A power user near their weekly usage limit wants sonnet
everywhere to protect the opus pool -- an acutely realistic preference, since the
orchestrate policy this marketplace ships renders a live capacity readout precisely because
users run close to those limits. Absent the seam their only remedies are forking the plugin or not
running review. (2) A different user wants to ADD a lane -- a security reviewer, or one
routed to a non-Claude backend for independence. Two distinct users, two distinct wants,
neither addressable. PASSES; the seam is a layered `review_profiles.yaml` defaulting to
the pre-seam profiles. Built -- see the SEAMS BUILT table below.

*Evidence from practice:* this exact gap was hit while authoring this document. A review
needed its opus lanes routed to a different backend, and with no seam the only way through
was to bypass the skill's dispatch and hand-roll the reviewer prompts. That is the "remedial
action against the default" the test names.

**Worked example of the test FAILING.** git-kit's base-branch fallback is
`origin/main -> origin/master -> main -> master`, omitting `dev`, `develop` and `trunk`.
Tempting to call a finding -- but auto-detect tries `@{upstream}..HEAD` FIRST, and a branch
a power user actually works on almost always tracks a remote. When it does not, the failure
is one message naming the exact fix ("pass an explicit range"). No uninstall, no repeated
remediation, no second distinct scenario. FAILS -- so it gets no config key. If the omission
still bothers you, widen the hardcoded list; that is a one-line default change, not a seam.

**Worked example of (a) done right.** `orchestrate` renders its entire policy -- routing,
backends, agent types, effort, capacity thresholds -- from `defaults/orchestration.yaml`
through a three-layer merge (shipped, then user, then project), with record-id merge
semantics, sparse overrides, and `disabled: true` to remove a record. Its SKILL.md goes
further and states that it carries no routing table *deliberately*, so the policy cannot be
answered from memory instead of from configuration.

**Worked example of (a) done right, differently.** `skills-kit:md-domain` buckets its rules
Architectural / Optional / Inoffensive and exposes a per-rule `off` toggle plus threshold
overrides across five config layers -- and then documents which rules are NOT configurable
and why. Declaring the boundary of configurability is itself part of satisfying the razor.

**Worked example of the consumer-owned opinion.** `git-kit` and `p4-kit` submit gates are
authored by the consumer in their own CLAUDE.md. The plugin supplies the mechanism and
holds no opinion about what must happen before a submit. Where this shape is available it
beats both outcomes of the razor, because there is no default to be wrong.

**Worked example of (a) done right.** `orchestrate` renders its entire policy -- routing,
backends, agent types, effort, capacity thresholds -- from `defaults/orchestration.yaml`
through a three-layer merge (shipped, then user, then project), with record-id merge
semantics, sparse overrides, and `disabled: true` to remove a record. Its SKILL.md goes
further and states that it carries no routing table *deliberately*, so the policy cannot be
answered from memory instead of from configuration.

**Worked example of (a) done right, differently.** `skills-kit:md-domain` buckets its rules
Architectural / Optional / Inoffensive and exposes a per-rule `off` toggle plus threshold
overrides across five config layers -- and then documents which rules are NOT configurable
and why. Declaring the boundary of configurability is itself part of satisfying the razor.

**Worked example of the consumer-owned opinion.** `git-kit` and `p4-kit` submit gates are
authored by the consumer in their own CLAUDE.md. The plugin supplies the mechanism and
holds no opinion about what must happen before a submit. Where this shape is available it
is better than either branch of the razor, because there is no default to be wrong.

### OP-3 -- Configurable is not enough; there must be a default

A setting with no sensible default imposes a decision at first run, which is the thing the
razor exists to prevent. It also breaks the upgrade path: a consumer who never set the key
gets different behaviour when it appears.

**Detect:** every config key introduced should name its default, and that default should
reproduce the behaviour consumers already had.

### OP-4 -- The default is documented where a consumer looks

A default discoverable only by reading the source is not documented. It belongs in the
plugin's configuration reference, next to the key.

**Detect:** grep the plugin's config doc for the key. `orchestrate`'s
`references/configuration.md` and md-domain's `references/configuring-standards.md` are
the models.

### OP-5 -- A registered stance names what it forecloses

The register is not a list of things we like. An entry earns its place by stating the
opinion, why it is imposed rather than configured, and what a disagreeing consumer should
do instead (use a different tool, fork, or accept it).

**Detect:** a register entry that only asserts the opinion, with no rationale and no
alternative, is incomplete. It reads as a stance but functions as an excuse.

### OP-6 -- Deviation inside this repo is remediated by a seam, never by a workaround

This is the sharpest criterion, because it has a positive signal you can grep for.

When plugins-kit itself cannot live with one of its own plugins' opinions, that is the
strongest possible evidence the opinion needed a configuration seam. The correct response
is to build the seam. Documenting the resulting warnings as noise is the anti-pattern: it
fixes our machine, converges nobody, and leaves every consumer with the same friction and
no instructions at all.

**Detect:** grep the tree for `treat .* as noise`, `ignore the warning`, `does not apply
to this repo`, `deliberate deviation`. Each hit is a candidate finding.

**Worked example (real, unfixed).** `awesome-kit:task` holds that `dev/tasks/<stub>` is the
durable, version-controlled half of the task system and `tmp/<stub>` the ephemeral half,
and its `validate` gate blocks work while a `dev/tasks` folder is uncommitted. This repo
gitignores `dev/` entirely, so the root CLAUDE.md instructs the reader:

> The task CLI's validate/work verbs may warn that a `dev/tasks` folder is uncommitted,
> and `archive` on a dev/tasks folder expects to commit a final state and delete the
> folder. Here, treat those as noise.

The plugin's own home repo disagrees with the plugin, and the remediation was prose telling
a reader to ignore a gate. The seam is straightforward: `durability_roots`
(`{ephemeral: tmp, durable: dev/tasks}`) plus an off switch for the
uncommitted-folder rule, both defaulting to today's behaviour. Note also that the two path
prefixes are baked into argparse as `choices=["tmp", "dev/tasks"]`, so the durability
semantics ride entirely on which of exactly two literal strings prefixes a path.

### OP-7 -- VCS, branch-name, and path assumptions are declared

The most common shape of undeclared opinion, and the easiest to detect.

**Detect:** grep for `main`, `master`, `origin/`, `dev/`, `tmp/`, `.git` in plugin scripts
and skills. Each hit is either intrinsic, configurable, or a finding.

**Worked example (real, low severity).** `git-kit`'s auto-detect falls back through
`origin/main -> origin/master -> main -> master`, which omits `dev` -- this repo's own
branch. The impact is bounded, because upstream resolution (`@{upstream}..HEAD`) is tried
first and `dev` tracks `origin/dev` here, so the fallback never fires in practice. It bites
only a branch with no upstream. Recorded as a real gap with an honest severity: the seam
would be a `base_branch_candidates` list defaulting to today's four.

**Worked example (real).** `awesome-kit:task` privileges git as the VCS it detects and
automates. Under another VCS the scripts run no commands and hand submission back to the
agent -- a strictly degraded path baked into the code rather than a pluggable alternative.
The skill does declare this, which is why it is a weaker finding than the durability one:
declared-but-not-configurable is a candidate for the register, not a silent assumption.

## Findings, with verdicts

Every candidate found in the 2026-08-08 survey, run through the scenario test. Carried here
so an audit neither rediscovers them as new nor re-litigates the ones that failed.

### PASSES -- seam required, not yet built

None outstanding.

### REGISTERED -- passes the test, declined deliberately

| Plugin | Opinion | Disposition |
|---|---|---|
| awesome-kit:task | git is the privileged, automated VCS | Registered as a deliberate stance in `plugins/CLAUDE.md`, with the rationale OP-5 requires and the bounded degradation a Perforce consumer actually gets. Not a seam. |

### SEAMS BUILT -- verdict discharged

| Plugin | Opinion | Seam, as built |
|---|---|---|
| git-kit, p4-kit | reviewer roster and model routing fixed in SKILL.md | Layered `review_profiles.yaml`. Shipped defaults and the resolver live in `bootstrap_lib.code_review`; precedence is shipped -> `~/.claude/config/review_profiles.yaml` -> `<project_root>/.claude/review_profiles.yaml`. Each plugin's `references/configuration.md` documents the keys and the shipped table (OP-4). The default is pinned byte-for-byte by `test_shipped_only_render_matches_pre_seam_bytes`. |

### FAILS -- correctly hardcoded, do not open these

| Plugin | Opinion | Why it fails |
|---|---|---|
| git-kit | base-branch fallback omits `dev`/`develop`/`trunk` | `@{upstream}` resolves first for any tracked branch; the fallback path ends in one message naming the exact remedy. Friction, not a scenario. Widen the list if desired -- that is a default change. |
| git-kit | fan-out threshold of 1 in review mode; `lanes <= 6` dispatch cutoff | Internal dispatch mechanics with no user-visible behaviour change to prefer. No user has a preference about a lane count. |
| p4-kit | `-m 20` pending-CL listing limit | One scenario at most (>20 pending CLs), remedied by picking the CL explicitly. If 20 proves low, raise the constant. |
| awesome-kit:task | document size ceilings (400 hard / 250 soft) | The tool's own house style for a rotation mechanism it owns, like a formatter's line length. Users adopt the mechanism or not. |
| p4-kit | auto-shelve then fingerprint-matched cleanup | Intrinsic to reviewing an unshelved pending CL in Perforce. Outside the razor. |

### BORDERLINE -- re-test if evidence appears

| Plugin | Opinion | Why unresolved |
|---|---|---|
| awesome-kit:task | `dev/tasks/<stub>` is durable and must be committed | RE-RUN, verdict downgraded from PASSES. The scenario that made it serious -- this repo gitignoring `dev/` and having to call the resulting blocking warnings noise -- was discharged by DETECTION rather than a seam: `validate.py` classes a git-ignored task root as an advisory note, never a warning, so it cannot gate `work`, and `location_ops.py` carries the matching `vcs_ignored` archive disposition. What remains is one non-serious scenario: a user whose durable root is `docs/tasks` or `.tasks` cannot express it, because the two prefixes are argparse `choices`. One weak scenario is below the bar of one serious or two distinct. Re-test if a second appears. |
| bootstrap | 3600s cooldown, 24h env-recheck TTL | A plugin developer iterating locally wants a shorter cooldown -- a real audience here, since this marketplace is public and others develop against it. But `bootstrap-reset-cooldown.sh` already gives a one-command remedy, which blunts "repeated manual remediation". Needs one more distinct scenario to pass. Notable that the four-layer manifest that would hold the key **already exists** -- a plugin with a configuration system can still fail the razor by not routing an opinion through it. |

Verdicts are evidence-based, so they expire. A FAILS entry that later acquires a real
scenario should be re-run and moved, and the scenario recorded -- that is the intended
lifecycle, not a reversal.

## Running the audit

1. **OP-1 sweep.** For each published plugin, list files under it and ask the
   off-our-machine question. Generated artifacts and design history are the usual hits.
2. **OP-6 grep.** Search the tree for the workaround phrases above. Each hit is a
   documented deviation, which by construction is an unbuilt seam.
3. **OP-7 grep.** Search plugin scripts and skills for branch names, VCS names, and path
   prefixes. Triage each into intrinsic / configurable / finding.
4. **OP-2 pass, per plugin.** Enumerate what the plugin decides for the user. Run each
   through the scenario test and write the verdict down with its scenarios. An opinion that
   has never been tested is the finding -- a FAILS verdict is a completed check, not a gap.
5. **OP-3 / OP-4 on anything added since the last audit.** New keys need defaults, and
   defaults need to be documented in the plugin's config reference.
6. **OP-5 on the register itself.** Entries that assert without rationale or alternative
   are incomplete.

Two failure modes for the audit itself:

- **Over-generating seams.** If the pass ends with almost everything marked configurable,
  the scenario test was not applied -- it was skipped in favour of "someone might". Roughly
  a third of candidates should FAIL. Check the rejects: an audit with none is not auditing.
- **Ending in prose.** Findings are recorded as seams to build. An audit that concludes by
  documenting the same opinions more thoroughly has produced the anti-pattern it was
  looking for.
