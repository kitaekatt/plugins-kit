# Stable channel: design

Status: design proposal. Not implemented. Two prior research units established
the Claude Code (CC 2.1.227) mechanics and the four mechanism families this
document builds on; their facts are treated as evidence, not re-derived here.
Two user directions are binding on this design and are threaded through every
section:

- **Bootstrap becomes the sole updater** for its marketplaces, displacing CC's
  per-marketplace `autoUpdate` -- held to be correct independent of the stable
  channel, but STRICTLY conditional on bootstrap fully providing the
  functionality it displaces (section 5).
- **The asymmetry: it is better to update too much than never update at all.**
  Every uncertain, degraded, or error path resolves toward UPDATING (or toward
  restoring CC's autoUpdate), never toward holding. A stable channel that
  cannot prove a version is stable does not hold -- it updates to latest and
  says so. Stated at each point of application below (marked "fail-open").
- **Never downgrade, as a rule.** Downgrading is out of scope by design, not a
  trade-off this document weighs: there is no CLI downgrade verb, the 14-day
  orphan GC makes reaching backwards unreliable, and downgrade is the
  under-update side of the asymmetry above. No downgrade path, cache-dir
  repointing, or backwards registry rewrite appears anywhere in this design.
  This ALIGNS the feature with existing machinery rather than fighting it:
  `marketplaces[].pin` is already documented as directional ("freezes future
  drift but never downgrades plugins already past the snapshot") and both
  version-comparison helpers in the codebase are one-directional. The stable
  channel inherits that same semantic instead of introducing an opposite one
  -- a simplification that removes a whole class of risk (section 10).

## 1. The feature

A consumer's `bootstrap.json` can declare, per plugin, that the plugin should
be synced to the STABLE channel rather than the newest published version.
"Stable" means: the newest published version that has either aged past a
window (default 7 days) since its publish, or was explicitly flagged stable at
publish time.

**What the stable channel IS, precisely: a rate limiter on forward movement,
not a version selector.** It decides WHEN a plugin advances, never WHERE it
lands beyond "the newest qualifying version"; it can only ever hold a machine
at its current version or move it toward a newer one. Nothing in the manifest
surface lets a consumer name an arbitrary older version, and no engine path
moves a machine backwards.

## 2. Chosen mechanism

**Family A (bootstrap-side hold, as sole updater) as the enforcement plane,
plus a publish-time stable ledger (`.claude-plugin/stable.json`) as the data
plane.** The ledger is the user's own "write a file at publish time" idea,
made the single source of truth for both stability rules. Families B, C, and D
are rejected; D's tag substrate is noted as a possible later hardening and
deliberately not built.

The catastrophic failure mode of family A is named up front and drives the
architecture: **a wrong move turns `autoUpdate` off for a machine and that
machine then never updates again** -- silently, forever, and self-masking (a
machine that stopped updating is indistinguishable from a machine with nothing
to update). Section 6 is the design's answer; section 9's staging refuses to
mute anything until the replacement updater is proven.

### 2.1 The four decisive axes

1. **Does the hold actually hold on a consumer machine?** Yes, once CC's
   per-marketplace `autoUpdate` is off -- the same move the existing
   `marketplaces[].pin` already performs and records. With CC muted, bootstrap
   is the sole updater, which it already largely is (self-registration keeps
   even undeclared plugins fresh via `claude plugin update`). The gap between
   "largely" and "fully" is specified and closed in section 5; muting is
   deferred until that closure is proven (section 9).
2. **Can two consumers on the same marketplace be on different channels?**
   Yes, structurally: the channel is declared in the consumer's layered
   `bootstrap.json`, per plugin, per layer (user / project / local). The
   marketplace stays channel-neutral. Per-plugin version divergence and
   per-project scope divergence are established first-party behavior, so this
   adds no new divergence axis.
3. **The ahead-of-stable machine.** Settled by the never-downgrade rule: the
   machine is left alone, logged, and stays ahead until stable catches up to
   it (section 7.5). There is no downgrade problem because there is no
   downgrade: the only version-targeted operation anywhere in the design is
   advancing a behind-stable machine UP to stable, a forward install.
4. **Publish-time moving parts.** One new generated-and-committed artifact
   (`stable.json`), appended by `publish.py` inside the release commit it
   already builds, verified by the `verify()` step it already runs. No tags,
   no second marketplace, no bundle plugin to re-publish as versions age.

### 2.2 Why not the alternatives

- **B (per-plugin `sha` in marketplace.json).** One `marketplace.json` for
  every consumer -- cannot express "stable for you, latest for me"; fails
  axis 2 outright, and would move all consumers backward at adoption.
- **C (second `plugins-kit-stable` marketplace).** Plugin identity changes
  (`bootstrap@plugins-kit-stable`): duplicate installs, duplicate hooks, two
  venv/data trees for a consumer with both; every `bootstrap.json` ref and
  every `dependencies: ["bootstrap"]` edge stops matching; cross-marketplace
  deps need the `allowCrossMarketplaceDependenciesOn` allowlist; the
  `git-subdir` self-referencing source it would likely need is unverified.
  The identity split alone is disqualifying.
- **D (git tags + dependency constraints).** The only CC-native enforcement,
  including at auto-update time. But it inverts authority: constraints live in
  a DEPENDING plugin's `plugin.json`, not a consumer's `bootstrap.json`, so
  channels would ship as a "stable bundle" plugin whose `=version` constraints
  must be re-published every time stable moves -- and under the age rule
  stable moves by the passage of time alone, potentially daily, with no code
  change to justify the publish. It requires tagging every release forever in
  a repo with zero tags, and per-project channel choice via project-scoped
  bundle installs is speculative. Against the feasibility unit's own "D as
  primitive + A as knob" recommendation: once A's precondition (autoUpdate
  off) holds, there is nothing left for CC-native constraints to defend
  against -- D would be armor on a locked door, bought with a perpetual
  re-tagging treadmill. And under the sole-updater direction, A's
  precondition is wanted anyway, independent of this feature.
- **A without the ledger.** The 7-day rule has no data source: nothing
  anywhere records when a version was published. The ledger IS the missing
  data source, and doubles as the carrier for the explicit stable flag.

## 3. The stable ledger: `.claude-plugin/stable.json`

### 3.1 Location and distribution

Repo path: `.claude-plugin/stable.json`, sibling of `marketplace.json`. It
reaches every consumer for free inside the marketplace clone
(`~/.claude/plugins/marketplaces/plugins-kit/`), which the engine keeps fresh.
No new distribution channel.

### 3.2 Primary data, not derived data

`marketplace.json` is derived from `plugin.json` files and must never be
hand-edited; `stable.json` is different in kind: its publish timestamps are
primary facts that exist nowhere else and cannot be regenerated from the
plugin tree. It is an **append-only ledger authored exclusively by tooling**
(`publish.py`, and `scripts/stable_channel.py` for retroactive marks), not a
regenerable artifact; the regen pre-commit hook does not touch it.
Consistency is enforced where it reaches consumers: `publish.py`'s `verify()`
(section 8.3).

### 3.3 Schema

```json
{
  "_schema_version": 1,
  "plugins": {
    "unreal-kit": [
      {"version": "0.11.5", "published_at": "2026-08-01T18:20:11Z", "channel": "auto"},
      {"version": "0.11.6", "published_at": "2026-08-10T02:05:44Z", "channel": "auto"},
      {"version": "0.12.0", "published_at": "2026-08-11T21:14:02Z", "channel": "stable",
       "requires": {"bootstrap": "0.80.0"}}
    ]
  }
}
```

Per published plugin, an append-only list of publish records, oldest first:

- `version` -- the `plugin.json` version shipped by that publish.
- `published_at` -- UTC ISO-8601, written by `publish.py` at publish time.
- `channel` -- one of:
  - `"auto"` (default): becomes stable by aging.
  - `"stable"`: stable immediately (the maintainer's explicit flag).
  - `"blocked"`: never stable, regardless of age (the retraction mark for a
    burned release).
- `requires` -- the built-against dependency floors for this version, one
  `{provider: min-version}` entry per declared outbound edge (section 12.4).

No commit SHA is stored: the release commit's own SHA cannot appear inside
the release commit, and it is not needed -- the engine resolves a version to a
committish by scanning clone history (section 7.4). Dev-only
(`published: false`) plugins never enter the ledger.

### 3.4 The definition of "stable", precisely

Evaluated **at consume time by the engine**, from the ledger in the local
marketplace clone:

```
stable(plugin) = the record with the highest version such that
    channel == "stable"
    OR (channel == "auto" AND now - published_at >= stable_age_days)
where channel == "blocked" never qualifies.
```

- **The two rules are OR'd**, with `blocked` as the one negative override:
  the explicit flag overrides age upward (stable before 7 days); `blocked`
  overrides it downward (never stable); `auto` waits.
- **"Hasn't changed in 7 days" means 7 days since THAT version's publish**,
  measured from `published_at`, regardless of supersession. The alternative
  ("survived 7 days as latest") makes stable stop advancing under sustained
  churn; the chosen reading guarantees stable trails a churn burst by exactly
  the window. "Changed" means "published": a dev-branch commit that has not
  shipped is invisible to consumers by construction (the cache keys on
  version), so publish time is the only change time that exists on a consumer
  machine.
- Consume-time evaluation is load-bearing: a plugin published once and left
  alone must BECOME stable by aging, with no publish event to recompute
  anything.
- **The resolution only ever gates forward movement.** `stable(plugin)` is an
  upper bound on how far the updater advances THIS pass, applied against the
  installed version with a one-directional comparison (the same directional
  semantic as `min_version` and the pin). An installed version above the
  bound is never a target for correction -- it is simply already past the
  gate. The bound is not absolute: a dependency floor from an installed
  consumer can raise a provider's effective target past its stable
  (section 12.4).
- **Fail-open: a plugin with no qualifying record** -- never-stable, absent
  from the ledger (published before the ledger existed), ledger missing,
  ledger unparseable, timestamps uncomputable -- resolves to **latest**, with
  a visible line saying so (section 7.6). Opting into stable never means "no
  plugin" and never means "held on an old version because the metadata is
  broken". This inversion is deliberate and directed: a stable channel that
  cannot prove a version is stable updates rather than holds.

## 4. Manifest surface

### 4.1 `plugins[]` entry: `channel`

```json
{
  "plugins": [
    {"ref": "plugins-kit:unreal-kit", "channel": "stable"},
    {"ref": "plugins-kit:hue-kit", "channel": "stable", "min_version": "0.9.0"},
    {"ref": "plugins-kit:bootstrap"}
  ]
}
```

- `channel`: `"latest"` (default) | `"stable"`. A scalar on the entry, so it
  layers exactly like `min_version`: merge identity is `ref`, highest-priority
  layer wins. A project `.claude/bootstrap.json` can put one plugin on stable
  while the user layer stays latest; `bootstrap.local.json` can override
  either. Per-machine, per-project channel choice falls out of the existing
  merge model with no new machinery.
- An unknown `channel` value is treated as `"latest"` with a visible warning
  (fail-open), never as an error that blocks the pass.

### 4.2 `marketplaces[]` entry: `stable_age_days`

```json
{
  "marketplaces": [
    {"name": "plugins-kit", "stable_age_days": 7}
  ]
}
```

- `stable_age_days`: positive integer, default `7`. Per-marketplace (a policy
  over one ledger), consumer-side because evaluation is consumer-side. An
  unparseable value falls back to the default with a warning (fail-open).

Deferred (section 9): `marketplaces[].default_channel`.

### 4.3 Interactions with existing fields

- **`min_version`**: stays a pure floor and OUTRANKS the channel.
  `min_version <= stable`: no interaction. `min_version > stable(plugin)`:
  fail-open -- the engine satisfies the floor (updates to the newest version
  meeting it, i.e. past stable) and logs a visible warning naming the
  conflict. It does not refuse and does not hold below the floor: refusing is
  the under-update side of the trade, and the user wrote the floor.
- **`install: "manual"`**: `channel` governs the update half the engine
  already owns for manual plugins -- updates target stable instead of latest.
  Install state remains the user's.
- **`scope`**: orthogonal; updates target the recorded scope (the existing
  update-where-it-lives rule).
- **`marketplaces[].pin`**: the pin wins. A pin freezes the whole clone,
  including `stable.json`, so channel evaluation under a pin would read a
  frozen ledger and the targeted install would fight the pinned checkout. A
  `channel: "stable"` plugin under a pinned marketplace logs a one-line
  warning and behaves as pinned. (A pin is an explicit user hold, so the
  asymmetry does not override it -- the asymmetry governs FAILURE paths, not
  the user's stated intent.)
- **Self-registration**: self-registered entries carry no `channel` and
  default to latest -- unchanged behavior for everyone who declares nothing.

## 5. Bootstrap as sole updater: the replacement contract

Direction: bootstrap displaces CC's per-marketplace `autoUpdate`, strictly
conditional on fully replacing it. What CC's autoUpdate does today: at every
session start, with no cooldown, refresh the marketplace and advance every
installed plugin from that marketplace to the latest listed version, comparing
version strings. The replacement must match that reach and cadence for every
latest-channel plugin, including plugins the user never declared.

### 5.1 Coverage: every plugin, declared or not

- Declared `plugins[]` entries (`install: "auto"`): already installed,
  scoped, enabled, and updated every pass.
- Undeclared plugins: the engine self-registers every processed plugin that
  ships a `bootstrap.json` into `bootstrap.local.json` as
  `{"ref": ..., "install": "manual"}`, and manual entries are kept fresh via
  `claude plugin update` each pass. Gap to close: a plugin with NO
  `bootstrap.json` is never processed and so never self-registered. The
  sole-updater sweep therefore iterates the REGISTRY (union: registry records
  plus enabled refs), not the manifest-bearing set: every installed, enabled
  plugin from a bootstrap-managed marketplace gets `claude plugin update`
  each updater pass, `bootstrap.json` or not. Channel only modulates the
  TARGET (stable vs latest); coverage is universal.

### 5.2 Cadence: the freshness probe closes the cooldown gap

Bootstrap's cadence today is gated by the per-project cooldown (3600s) and the
registry-change bypass -- and that bypass is armed by CC's OWN update writes.
With CC muted, a remote publish changes nothing on the local disk, so nothing
arms the bypass: a publish could sit unapplied for the full cooldown window,
or indefinitely on a machine that only ever starts sessions inside the window.
That is a regression against CC's every-session-start cadence, so the
replacement adds a **freshness probe** to the SessionStart shell hook's
detached `_provision` path (never the foreground path -- session readiness
must not block on the network):

- On every session start, including cooldown-skipped ones: `git ls-remote
  <source> <default-branch>` per bootstrap-managed marketplace, compared
  against the clone's recorded HEAD. A changed remote SHA arms the same
  bypass a registry change arms today: the full pass runs, refreshes the
  clone, and the updater sweep advances plugins.
- Probe cost: one network round trip per marketplace per session start, in
  the detached background path. Matches CC's own per-session network touch.
- **Fail-open**: a probe that errors (offline, DNS, auth) logs and falls
  through to the existing cooldown behavior -- the machine updates on the
  next pass at the latest, which is CC-equivalent (CC offline also does not
  update). A probe error is never a reason to skip an already-due pass.

### 5.3 What the updater records (liveness and efficacy)

Every updater sweep writes, per marketplace, an **updater stamp**: pass
timestamp, clone HEAD SHA at sweep time, and per-plugin (installed -> target)
outcomes, into bootstrap's data dir (via the existing `stamps.py`
conventions). This stamp is the liveness signal the dead-man's switch
(section 6.2) and the detection surface (section 6.1) both key on -- and in
slice 1 it doubles as the parity evidence (section 9).

## 6. The catastrophic failure mode, addressed head-on

The failure: `autoUpdate: false` is written, then bootstrap-as-updater stops
running (engine wedged, plugin disabled, delivery path broken, a machine that
stops starting sessions in a project) -- and the machine never updates again,
silently, looking exactly like a healthy machine with nothing to report.

### 6.1 Detection

Three layers, cheapest first:

1. **Self-detection in-session**: the UserPromptSubmit display hook (runs on
   every prompt, ~0ms idle) compares the updater stamp's age against a
   staleness threshold whenever the mute marker exists. Stale mute -> a
   VISIBLE line in the next display ("marketplace plugins-kit: updates are
   muted but the updater has not run for N days") plus the restore action
   below. The self-masking property is broken by making mute-without-recent-
   updater-pass a first-class reportable state rather than silence.
2. **Fleet-side detection**: `verify()`-style tooling is publisher-side and
   cannot see consumers; there is no fleet telemetry channel and this design
   does not invent one. Detection is therefore local-first by construction.
3. **Human-auditable state**: `bootstrap-reset-cooldown.sh --status` (or a
   sibling `--updater-status`) prints mute state, prior value, and last
   updater pass per marketplace, so "is this machine muted and current?" is
   one command, not an inference.

### 6.2 The dead-man's switch: the mute is contingent on liveness

**Mechanism: a staleness-triggered restore, enforced by the layers that still
run when the engine does not.** The mute self-expires:

- The engine, at the START of every pass, re-asserts the mute only if the
  pass is going to run the updater sweep; a completed sweep refreshes the
  updater stamp.
- The SessionStart shell hook (which runs even on cooldown-skipped sessions,
  before any engine) and the UserPromptSubmit display hook both check: mute
  marker present AND updater stamp older than `mute_max_staleness` (default
  7 days) -> **restore CC's `autoUpdate` to the recorded prior value, delete
  the mute marker, and log the restore visibly**. From that moment CC updates
  again on its own; the machine may overshoot stable, which is the acceptable
  over-update side of the trade, taken deliberately.
- Why hook-side: the hooks are the only bootstrap code that still executes
  when the ENGINE is broken, and they are near-dependency-free. The restore
  is a small JSON edit performed with the standalone Python the hook already
  guarantees; on failure it retries next session and logs.
- **Residual hole, named**: if the bootstrap PLUGIN itself is disabled or CC
  never loads it, no bootstrap code runs at all and nothing expires the mute.
  That is precisely the escape-hatch test ("would this fix have to be
  installed by the thing it repairs?" -- yes), so the remediation for that
  shape ships in **`bootstrap-stuck-fix`**: a narrow check -- mute marker
  present + updater stamp stale beyond threshold -> restore recorded value --
  following that plugin's narrowness discipline. Shipping this remediation is
  a PRECONDITION of the slice that first mutes (section 9).

Alternatives considered and rejected: (a) no mute at all, correct after CC
advances -- requires downgrades, which do not exist; (b) a TTL encoded in
`known_marketplaces.json` -- CC has no such field, and inventing one in CC's
file is hand-shaping foreign state; (c) an OS scheduled task as watchdog --
a second delivery/maintenance surface, out of proportion (YAGNI), and itself
un-updatable once wedged.

### 6.3 Reversibility and the prior-value record

The prior `autoUpdate` value is recorded in **`marketplace_pins.json`** --
extending the existing record (which already stores the pre-pin `autoUpdate`
for `marketplaces[].pin`) with a `reason` field (`"pin"` | `"sole-updater"`),
rather than inventing a second file with the same job. One file answers "why
is autoUpdate off and what was it before", for every instrument that turns it
off. Restore paths, all reading that record: the engine (channel declarations
removed -> restore next pass, same lifecycle as unpin), the dead-man hooks
(staleness), `bootstrap-stuck-fix` (engine-dead shape), and a human following
`--updater-status` output. A missing or corrupt record restores to `true`
(fail-open: when in doubt, CC updates).

### 6.4 Bootstrap failing to update ITSELF

If the broken thing is bootstrap's own delivery path, no bootstrap release can
reach the machine (it arrives only via the mechanism that is broken there) --
the CLAUDE.md escape-hatch rule verbatim. Consequences for this design:
bootstrap's own ref is **exempt from any hold** (it is never eligible for
`channel: "stable"`; a manifest declaring it logs a warning and is treated as
latest) so the updater's own engine always advances as fast as CC would have
advanced it, and the stuck-machine remediation above lives in
`bootstrap-stuck-fix`. The one plugin that must never lag is the updater.

## 7. Engine behavior (per stable-channel entry)

New module: `bootstrap_lib/stable_channel.py` (ledger parsing, stable
resolution, committish resolution). Bootstrap version bump required to ship
any of it (manifest semantics are engine code).

### 7.1 Arming

When any processed entry on a marketplace declares `channel: "stable"` AND
the sole-updater gate is on (section 9): force `autoUpdate: false` for that
marketplace, record the prior value in `marketplace_pins.json` with
`reason: "sole-updater"` (6.3). When the last declaration disappears, restore
and remove -- the unpin lifecycle. The clone itself is still refreshed every
pass (listing freshness does not bump installed versions; documented
`alwaysUpdate` semantics), so the ledger evaluated is current while CC's
installer is muted.

- action: `marketplace plugins-kit: autoUpdate disabled (bootstrap is sole updater; prior value recorded)` -- on the transition.
- action: `marketplace plugins-kit: autoUpdate restored (<reason>)` -- on any restore path.
- ok (verbose): `marketplace plugins-kit: sole updater active; last sweep <age>` -- steady state.

### 7.2 Per-plugin comparison

The sweep processes shared-lib providers before their consumers
(generalizing the existing bootstrap-first ordering), and the per-plugin
target incorporates dependency floors: a consumer's advance first raises its
providers to any recorded floor, and a provider's hold target is raised to
the max of its installed consumers' floors (section 12.4).

Each updater sweep: resolve `stable(plugin)` (3.4), read the installed
version from the registry (registry record preferred; the cache-scan
fallback's "highest version dir" heuristic is WRONG under a hold, where a
newer dir may coexist with an older loaded one -- on a fallback-only machine
the hold is not enforceable, so fail-open: treat the plugin as latest-channel
and say so). Branch on installed vs stable:

### 7.3 BEHIND stable -> advance to stable, exactly

If stable == the clone's latest (quiet plugin): plain
`claude plugin update <ref> --scope <recorded>`. If stable < latest (churn in
flight -- the case the feature exists for), a plain update overshoots and the
CLI has no version argument, so the engine performs a **targeted install via
the clone**:

1. Resolve the committish for `stable(plugin)` (7.4).
2. `git checkout --detach <sha>` in the marketplace clone (the pin
   machinery's existing move).
3. `claude plugin update <ref> --scope <recorded>` (or `install` when absent
   and `install: "auto"`) -- CC copies from the clone, installing exactly the
   stable version. A forward move (installed < stable), never a downgrade, so
   it does not depend on CC downgrade behavior.
4. `git checkout <default-branch>` in a `finally` -- the clone is never left
   detached (that is the pin's job, and no pin is set).
5. Re-check the registry; log the outcome.

The single-instance engine lock serializes this against concurrent passes; CC
does not touch the clone mid-dance because autoUpdate is off. **Fail-open**:
if the dance fails at any step (checkout error, update error, re-check
mismatch), the engine logs the failure AND falls back to a plain update to
latest on the same pass -- a machine that cannot be placed at stable is placed
at latest, never left behind stable.

- action: `plugin plugins-kit:foo: channel stable -> updated 0.4.0 -> 0.4.2`
- action: `plugin plugins-kit:foo: channel stable -> installed 0.4.2 via clone checkout (latest is 0.5.0)`
- action: `plugin plugins-kit:foo: stable install failed (<reason>); updated to latest 0.5.0 instead` -- the fail-open fallback, always visible.

### 7.4 Committish resolution (no SHA in the ledger)

`resolve_committish(clone, plugin, version)`: walk
`git log --format=%H -- plugins/<name>/.claude-plugin/plugin.json` on the
clone's master history; return the newest commit whose blob at that path
parses to `version`. Deterministic, cheap (the file changes only on bumps),
cacheable per (plugin, version). **Fail-open**: resolution failure (version
in the ledger but absent from history) -> update to latest, visible warning.

### 7.5 AT stable and AHEAD of stable

- **AT stable**: ok entry (verbose-only): `plugin plugins-kit:foo: at stable 0.4.2`.
- **AHEAD of stable: leave alone and log -- decided by the never-downgrade
  rule, not weighed here.** Downgrade is out of scope by design (no CLI verb,
  the 14-day orphan GC, the under-update side of the stated asymmetry), and
  refuse-to-arm is rejected because every machine is ahead of an empty ledger
  at adoption. The machine simply stays ahead until stable catches up to it,
  which is coherent under BOTH stability rules: under the age rule, the
  installed version (or a successor) ages past the window and stable reaches
  or passes it within at most `stable_age_days`; under the explicit-flag
  rule, the next `--stable` publish moves stable to the flagged version
  immediately. Either way the machine transitions from "ahead" to "at or
  behind stable" without ever moving backwards, and the hold does its job
  from that point on.
  What the consumer sees: one action entry
  `plugin plugins-kit:foo: installed 0.5.0 ahead of stable 0.4.2; holding until stable catches up (never downgrades)`,
  deduplicated via a stamp keyed on `(plugin, installed, stable)` -- the same
  state logs once as an action, thereafter verbose-only, so a days-long ahead
  state does not train the user to skip the display. When stable catches up,
  the state resolves silently into the normal at-stable / behind-stable
  branches.

### 7.6 Degraded metadata -> latest, visibly (fail-open, consolidated)

Every path where stable cannot be computed resolves to LATEST plus a visible
action entry -- never a hold, never a fix-all that blocks updating:

- ledger missing from the clone / unparseable / unknown schema:
  `plugin plugins-kit:foo: channel stable requested but stable metadata unavailable; using latest <v>`
- plugin absent from the ledger, or no qualifying record yet:
  `plugin plugins-kit:foo: no stable release yet; using latest <v>`
- timestamps uncomputable, committish unresolvable, registry unusable: the
  corresponding line from 7.2-7.4.

First occurrence per (plugin, resolved-latest) is an action entry; unchanged
recurrence is verbose ok. The one thing that is never allowed is SILENT
divergence from what the user asked for -- the user opted into stable and is
getting latest; the line says so.

## 8. Publish-time changes

### 8.1 `publish.py`

- **Ledger append** (inside the existing derived-artifacts stage): for every
  published plugin in `bumps`, append
  `{version, published_at: <now UTC>, channel: "auto", requires: {...}}` to
  `.claude-plugin/stable.json` -- the `requires` map records the
  built-against dependency floors per section 12.4 -- staged with
  `marketplace.json` and `index.html` so it rides INSIDE the release commit;
  `commit_derived`'s amend-or-follow-up logic covers it unchanged.
- **`--stable` flag**: `--stable` marks every bump in this publish
  `channel: "stable"`; `--stable <plugin>` (repeatable) marks a subset. The
  maintainer's flagging surface is a CLI flag on the one script that owns
  publishes -- the decision lands in the ledger and the publish log, not in a
  commit trailer nobody parses or a `plugin.json` field (stability is release
  data, not plugin data).
- **Preflight**: refuse when the working-tree ledger already contains a
  record for a (plugin, version) being published (re-run or hand-edit), or is
  unparseable. (Publisher-side strictness is fine -- the asymmetry governs
  consumer-side failure paths; a refused publish updates nobody wrongly and
  is loudly visible to the operator.)
- No `claude plugin tag --push` anywhere in the flow (family D declined).

### 8.2 Retroactive marks: `scripts/stable_channel.py`

`uv run python scripts/stable_channel.py mark <plugin> <version> stable|blocked`
rewrites one record's `channel`, refusing unknown plugin/version. Committed on
dev. Reach: the ledger reaches consumers via the MASTER clone and the publish
gate requires a bump -- so a retroactive mark lands with the next publish.
Acceptable for both uses: `blocked` marks a burned release, and the
burned-version recovery pattern (gotcha 3) already mandates a patch-bump
publish, which carries the mark; a retroactive `stable` can wait for the next
publish or ride an infra-drift master sync. Aging into stability needs no
push at all -- the timestamps are already on every consumer's machine.

### 8.3 `verify()` additions

- Newest ledger record per bumped plugin matches its `plugin.json` version
  and carries a parseable timestamp.
- Append-only against `origin/master`'s copy: every record on master is
  present locally, byte-identical except `channel` (the one mutable field).
  This is the anti-hand-edit gate, placed at the moment the file reaches
  consumers.

No new pre-commit hook: the ledger only matters when it reaches master,
`publish.py` is unbypassable there, and every commit-time gate taxes every
concurrent session in the shared tree. If dev-branch drift is observed, add
one following `_gitindex.py`'s SCOPE_SKIP convention; not before (YAGNI).

### 8.4 The publish definition

Gains one clause: a publish also appends the ledger records inside the
release commit. `regen_marketplace.py` and `marketplace.json` are untouched
(channels are consumer-side; the listing does not encode them -- axis 2).

## 9. Staging -- no machine's autoUpdate is touched until the updater is proven

Restructured under the binding direction: slice 1 mutes nothing; the slice
that first writes `autoUpdate: false` sits behind an explicit evidence gate
and a user go.

**Slice 1 -- ledger + shadow updater (observe/verify only; ZERO mutation of
CC state).**

- `publish.py`: ledger append including the `requires` floor map (12.7 --
  floors, like timestamps, cannot be reconstructed later), `--stable`,
  preflight + verify additions; `scripts/stable_channel.py mark`.
- Engine: read `channel` / `stable_age_days`; resolve stable; write the
  updater stamp (5.3); and log, per plugin per pass, THE DECISION IT WOULD
  HAVE MADE (`would hold at 0.4.2`, `would update to 0.4.2`,
  `no stable yet; would use latest`) alongside what CC's still-active
  autoUpdate actually did (installed version before vs after session start).
  CC remains the updater; `autoUpdate` is not written; no targeted installs
  run.
- Why this is the smallest real value: the ledger must exist before anything
  can age into stability (enforcement on day one holds nothing for a full
  window, because stable is undefined for every plugin), consumers get drift
  visibility immediately, and every session becomes a parity trial of the
  shadow updater against the real one -- on real machines, at zero risk.

**Slice 2 -- the updater machinery, still shadow.** Freshness probe (5.2)
wired into the detached hook path (it only ARMS passes -- arming more passes
is CC-cadence parity, not a mute); the registry-union sweep (5.1) exercised in
log-only mode; the targeted-install dance implemented and integration-tested
against a scratch marketplace (never the live clone); dead-man restore logic
(6.2) and the `marketplace_pins.json` `reason` extension (6.3) implemented and
unit-tested; the `bootstrap-stuck-fix` restore remediation written. Still no
`autoUpdate` write on any consumer machine.

**Slice 3 -- flip, gated.** The first slice that ever writes
`autoUpdate: false`. Gate, all three required:

1. **Parity evidence**: over >= 14 days of slice-1/2 telemetry on the
   maintainer's own fleet, zero sessions where the shadow updater's decision
   for a latest-channel plugin lagged what CC actually installed (shadow ==
   CC, every time), and zero unhandled ledger/probe errors in the logs.
2. **Recovery evidence**: the dead-man restore observed working end-to-end at
   least once (mute planted by hand on a test machine, engine disabled,
   restore fires at threshold), and `bootstrap-stuck-fix`'s restore published.
3. **User go**: the flip ships default-OFF behind engine config
   (`sole_updater: false`); turning it on -- first for the maintainer's fleet
   via config, then as the default in a later bump -- is an explicit,
   user-authorized publish each time.

With the gate passed, slice 3 activates: arming (7.1), behind-stable advance
including the targeted dance (7.3), ahead-of-stable dedup stamp (7.5), the
`min_version` fail-open (4.3), dependency-floor enforcement with
provider-first ordering (12.4), and GC-window verification (risk 3) with any
re-freshen logic it forces.

**Out of scope by rule**: any rollback/downgrade verb (never-downgrade,
preamble). **Deferred indefinitely**: `marketplaces[].default_channel` (build
when a consumer declares more than a handful of entries); family-D tag
hardening; a fleet telemetry channel.

## 10. Risks and unknowns, ranked by the asymmetry

Under-update risks (permanent, silent) outrank over-update risks (recoverable,
visible) by direction. Where a choice trades one for the other, this design
takes the over-update side, and says so at each point (4.3, 6.2, 6.3, 7.2,
7.3, 7.4, 7.6).

1. **Mute without a live updater** -- the catastrophic, self-masking,
   under-update failure. Addressed by: mute deferred to gated slice 3;
   liveness-contingent mute with hook-side dead-man restore (6.2); prior
   value recorded and restorable from four independent paths (6.3);
   `bootstrap-stuck-fix` remediation for the engine-dead shape, shipped as a
   slice-3 precondition; bootstrap's own ref exempt from any hold (6.4).
   Residual: a machine where NO bootstrap code runs and stuck-fix is not
   installed stays muted -- named, bounded by the slice-3 gate, and the
   restore-on-doubt default (corrupt record -> `true`).
2. **Old engines ignore `channel` silently** (min_version's chicken-and-egg
   shape). Re-ranked DOWN by the asymmetry: the failure direction is
   over-update (the consumer gets latest), which is the acceptable side.
   Documented as advisory-until-engine-current; no further mitigation owed.
3. **14-day orphan GC vs a held version -- largely eliminated by the
   never-downgrade rule.** The design never reaches BACKWARDS for a reaped
   version: the targeted install (7.3) serves a machine advancing FORWARD to
   stable and installs from the clone checkout, not from a cache dir, so a
   GC'd superseded dir costs nothing there. The one residual exposure is the
   CURRENTLY-INSTALLED held dir itself: unverified whether GC treats a
   registry-referenced dir as orphaned once latest supersedes it. If it does,
   the failure is loud (plugin fails to load) and forward-recoverable (next
   pass reinstalls stable, or fail-open latest) -- one bad session, not a
   wedge. Slice-3 verification item: establish that GC behavior empirically;
   only if referenced dirs are eligible does the hold check need a
   re-freshen.
4. **The `registry_v2_empty` insight is stale or version-bound**: on this
   machine (CC 2.1.227) the registry IS populated. (a) The repo insight needs
   a correction pass -- flagged, out of scope here. (b) The cache-scan
   fallback's highest-dir heuristic is wrong under a hold; handled fail-open
   in 7.2 (fallback-only machine -> latest-channel + visible line).
5. **CC downgrade behavior unresolved -- and irrelevant, by rule.** Never
   downgrading is a design rule (preamble), so no path in this design can
   ever need the answer. The risk is eliminated outright, along with the
   complexity that would have served it: no downgrade verb to build, no
   cache-dir repointing, no backwards registry rewrite, no dependence on the
   burned old dirs GC reaps. Recovery from a bad release is forward only:
   mark it `blocked`, patch-bump past it -- which reaches every machine,
   which a local rollback never would.
6. **`git-subdir` self-referencing sources unverified -- and unused** (family
   B/C territory only).
7. **First-arming race**: CC may advance a plugin once before the engine
   first writes `autoUpdate: false` (the pin documents the identical race).
   Self-heals as ahead-of-stable: held, logged, caught up within a window --
   the over-update side, accepted.
8. **Stale editable `.pth` under a hold**: a held plugin's venv can silently
   execute newer cached code (pre-existing defect
   `stale_editable_self_install`, unfixed). A hold lengthens the exposure
   window, raising that fix's priority in venv provisioning; it is not made a
   dependency of this feature, but slice 3's smoke test must include one held
   plugin whose venv resolution is checked.

## 11. Plugin-opinion razor

- **`stable_age_days = 7`**: PASSES (a risk-averse team wanting 30 days and a
  near-latest team wanting 2 are both realistic power-user preferences; the
  remedy without a seam is abandoning the channel). Config key:
  `marketplaces[].stable_age_days`, default 7 (4.2).
- **Stable is opt-in; the default channel is latest**: FAILS the test. The
  default preserves what every consumer already has; "user wants stable" is
  served by the `channel` key, which IS the seam. No second knob.
- **Bootstrap as sole updater -- opt-in or default?** Per the user direction
  this is the intended end state, not merely a stable-channel precondition.
  Razor: the muting itself PASSES the test in the abstract (a consumer could
  prefer CC's updater), so it gets a seam: engine config `sole_updater`,
  default `false` in slice 3, flipped to `true` as default only by a later
  explicit publish (9, gate 3). The seam doubles as the rollback: setting it
  `false` restores the recorded `autoUpdate` value on the next pass.
- **`mute_max_staleness = 7 days`** (dead-man threshold): FAILS the test --
  no realistic power-user preference turns on its exact value (any value in
  the plausible range produces the same behavior: eventual restore), and a
  seam would invite setting it to "never", which reintroduces risk 1.
  Hardcoded; registered here so the claim is falsifiable.
- **Ahead-of-stable leave-and-log is not configurable**: not a razor
  question -- never-downgrade is a stated design rule (preamble), so
  force-back is out of scope rather than a declined preference. The remedy
  for a user burned by a bad latest is forward and fleet-wide: mark the
  release `blocked`, publish the recovery bump.
- **Fail-open-to-latest on degraded metadata is not configurable**: a
  "fail-closed" preference (hold when unsure) exists in the abstract but is
  REFUSED by the binding asymmetry direction; registered here rather than
  seamed.
- **The built-against floor default** (section 12): PASSES the test -- a
  maintainer who knows a consumer runs fine against an older provider will
  not want the provider dragged forward past its own stable on that
  consumer's account. Seam: the optional `requires` override (12.5), which
  loosens (or tightens) the recorded floor per edge. Default stays the
  conservative publish-time snapshot, because a too-tight floor costs only
  extra forward movement (the acceptable side) while a too-loose one costs a
  runtime failure.

## 12. Inter-plugin dependencies and stability: the floor rule

The gap: a stable plugin may require a dependency version that is NOT stable
(awesome-kit stable, but built against a skills-kit that is not). The naive
rule -- "a plugin is stable only if its dependencies are stable" -- has an
unacceptable consequence: bootstrap publishes constantly, everything depends
on bootstrap, so everything is transitively unstable most of the time.
This section chooses a different rule and defends it.

### 12.1 The real dependency graph, verified against the tree

Two edge sets exist, and they are not the same thing:

- **`plugin.json` `dependencies`** -- presence-only, deliberately unversioned
  bare strings (version constraints would resolve against `{plugin}--v{ver}`
  git tags this repo does not use). Verified edges: every plugin -> bootstrap
  (enforced by `check_bootstrap_dependency.py` + the publish gate), plus
  awesome-kit -> skills-kit and prototypes -> skills-kit. This graph cannot
  even EXPRESS the fragility in question -- it carries no versions -- and CC
  uses it only for install-time presence, which channels never affect
  (channels move versions, not presence). It needs no change and takes no
  part in stability.
- **The shared-lib graph** (`shared_libs` / `shared_lib_imports` in
  `bootstrap.json`) -- the coupling that actually breaks at runtime.
  Verified: providers are bootstrap (`bootstrap_lib`), llm-scripting-kit
  (`llm_scripting_kit`), skills-kit (`skills_kit_lib`), content-pipeline-kit
  (`content_pipeline`), p4-kit (`p4kit_vcs`); consumers are git-kit, p4-kit,
  unreal-kit, llm-scripting-kit (-> `bootstrap_lib`), content-pipeline-kit
  and workflow-kit (-> `llm_scripting_kit` + `bootstrap_lib`), awesome-kit
  (-> `skills_kit_lib`). Acyclic today (chains like bootstrap ->
  llm-scripting-kit -> content-pipeline-kit, no cycles).

The mechanism detail that makes this THE axis the channel must reason about:
`_shared_libs/<name>/` is a single version-independent copy synced from the
OWNER's installed version. **The lib every consumer executes is always the
provider's currently-installed code.** A held consumer cannot keep an old
lib, and a held PROVIDER pins the lib for every consumer on the machine --
including latest-channel consumers that expect newer API. The
`codex_dispatch_is_silent_on_failure` insight records the live instance of
this shape ("bootstrap must ship before or with llm-scripting-kit, or
CodexCliBackend raises ModuleNotFoundError on every consumer"); a stable
channel would manufacture that shape at scale. (The
`stale_editable_self_install` defect compounds it: a plugin's OWN stale
editable can misresolve even when the shared lib is right -- pre-existing,
tracked in risk 8.)

### 12.2 What never-downgrade already defuses

Be precise about which failure modes are impossible before defending against
the rest:

- **A provider being AHEAD of what a held consumer was built against is the
  normal, safe direction** -- it is today's steady state under ordinary
  publish skew (owner and consumer never update in the same instant), and
  shared-lib APIs evolve additively in practice. Holds only DELAY a
  consumer's advancement; they never make its provider older than anything
  the consumer has already run against.
- **"My dependency got downgraded below what I need" cannot happen** --
  nothing in this design moves any plugin backwards, ever.

The one REAL defect class is therefore: **a provider held (or lagging)
behind what an advancing consumer requires** -- newer consumer, older lib,
`ModuleNotFoundError`/`AttributeError` at exit 0 in the worst case. That is
the only shape the rule below defends against.

### 12.3 Rejected alternatives

- **Contagion ("stable only if deps are stable")** -- the rule the user
  named, rejected for the reason the user gave, made concrete by the
  verified graph: every plugin depends on bootstrap, bootstrap is the most
  frequently published plugin in the marketplace, so every bootstrap publish
  resets the entire fleet's stability clock and `channel: "stable"` degrades
  to "hold everything, always". It is also the under-update direction (it
  HOLDS consumers on account of a provider), which the asymmetry forbids.
  And it defends against the wrong direction: 12.2 shows provider-newer is
  safe; contagion polices provider-newer while doing nothing about
  provider-older.
- **Cohorts (tightly-coupled sets advance in lockstep)** -- rejected because
  the verified graph is hub-shaped: `bootstrap_lib` reaches git-kit, p4-kit,
  unreal-kit, llm-scripting-kit, content-pipeline-kit, workflow-kit, and
  transitively awesome-kit via skills-kit. Any honest cohort computation over
  that graph collapses to one cohort containing nearly the whole marketplace
  -- which is exactly `marketplaces[].pin`, a feature that already exists.
  Cohorts add a grouping concept whose degenerate case duplicates the pin and
  whose non-degenerate case does not occur in this graph.
- **Substrate exemption as the whole answer ("a shared-lib provider is never
  held")** -- it does kill the bootstrap case by construction, and the set is
  mechanically detectable (a `shared_libs` key). But five of the thirteen
  published plugins are providers, including skills-kit and
  content-pipeline-kit -- fast-churning plugins that are among the most
  plausible things a consumer would WANT to hold. Excising them makes the
  feature a torso. Two shards of the idea survive on their merits:
  bootstrap's own ref stays hold-exempt (already required by the sole-updater
  role, 6.4), and the updater sweep processes providers before consumers
  (12.4), generalizing the engine's existing bootstrap-first ordering.

### 12.4 The chosen rule: built-against floors, recorded at publish time

Invert the contagion question. Instead of "am I stable only if my deps are
stable", ask **"does my target version REQUIRE anything my providers'
installed versions do not provide"** -- a minimum-version FLOOR per edge,
recorded in the ledger at publish time:

- **Recording (automatic, zero maintainer effort).** When `publish.py`
  appends a plugin's ledger record, it also records
  `"requires": {"<provider>": "<version>"}` for every outbound edge in the
  union graph (shared_lib_imports mapped lib -> owner via the tree's
  `shared_libs` declarations, plus `plugins[]` refs and `dependencies`
  entries), with the floor defaulting to **the provider's version in the
  tree at the release commit** -- the version the plugin was built and
  tested against. Conservative by construction: possibly tighter than
  necessary, never looser than what was actually tested.
- **Consumer-advance check.** Before the updater advances consumer C to
  target version c (stable or latest), it reads `requires(c)` and raises
  each provider P to `max(installed(P), stable(P), floor)` first --
  providers are processed before consumers, so this lands in the same pass.
  Raising P past its own stable to satisfy a floor is permitted and logged:
  `plugin plugins-kit:skills-kit: advanced past stable to 0.46.0 (floor
  required by awesome-kit 0.30.0)` (action entry). The floor beats the hold
  because the alternative -- holding C below ITS stable to keep P stable --
  is contagion re-entering through the back door, and the under-update side
  of the trade.
- **Provider-hold check (the same rule seen from the other end).** A
  provider's hold target is `max(stable(P), max over installed consumers'
  floors)`. Holding P at a version some installed consumer's floor exceeds
  would manufacture the 12.2 defect deliberately; the hold target rises
  instead, up to latest at most.
- **Why bootstrap changing destabilizes nobody**: a new bootstrap publish
  changes no existing consumer's recorded floors. The floor only binds when
  a consumer version that actually NEEDS the newer bootstrap becomes the
  consumer's target -- which is the user's requirement, met by construction.

### 12.5 Declarations: what is new, and who declares it

**Nothing new is mandatory.** The edge set is already declared today
(`shared_lib_imports`, `plugins[]` refs, `dependencies`); the floors are
recorded mechanically by `publish.py`. One OPTIONAL new key: a `requires`
map in a plugin's `bootstrap.json` (e.g. `{"requires": {"skills-kit":
"0.44.0"}}`), declared by the plugin maintainer, which overrides the
snapshot default for that edge -- normally to LOOSEN it when the maintainer
knows an older provider suffices, occasionally to tighten it ahead of a
publish. Precedence: declared value wins over the snapshot; `verify()`
refuses a declared floor naming a version absent from the provider's
history.

**When a floor is wrong or absent**: absent -> the snapshot default (there
is no unfloored edge after the first post-ledger publish); too tight -> a
provider advances further than strictly needed (over-update side, visible in
the action line, harmless); too loose (maintainer override error) -> the
same runtime failure that exists today without the feature, loud at the
`bootstrap_guard`/import layer, corrected by fixing the override. Records
predating the ledger have no floors: fail-open -- no floor, no constraint,
advance normally.

### 12.6 No-deadlock proof

Every constraint the rule generates has the form `installed(P) >= f` where
`f` is a version that existed in P's history at some consumer's publish
time, hence `f <= latest(P)` by construction. All constraints are lower
bounds; all engine motion is forward (never-downgrade); advancing any P to
`latest(P)` satisfies every constraint on P simultaneously; and advancing is
always available because floors outrank holds (12.4) and every degraded
path fails open to latest (3.4, 7.6). So the constraint system is satisfiable
by finite forward motion from any state, and monotone -- satisfying one
constraint can only raise versions, which never violates a lower bound.
A malformed floor with `f > latest(P)` (corrupt ledger, bad override) is
detected at evaluation and resolved fail-open: advance P to latest, advance
C normally, one visible warning naming the bad floor. A dependency cycle, if
one ever appears in the graph, degrades the same way: both plugins advance
to latest with a warning. No path holds anything forever; the never-update
failure cannot arrive by this route.

### 12.7 Deltas to the rest of this design

- **Publish-time** (extends 8.1): the ledger record gains the `requires`
  map; `verify()` gains the floor-existence check (12.5). `publish.py`
  builds the lib->owner map from the tree's `shared_libs` declarations.
- **Engine** (extends 7.2/7.3): the updater sweep orders providers before
  consumers (generalizing the existing bootstrap-first ordering), and the
  advance/hold targets incorporate floors per 12.4.
- **Slice 1 changes: yes, minimally.** The `requires` map is recorded from
  the first ledger append -- floors, like timestamps, cannot be
  reconstructed later, and slice 3's enforcement needs a floor history that
  only accumulates while slices 1-2 run. The slice-1 shadow report includes
  would-be floor resolutions (`would advance skills-kit past stable to
  0.46.0 (floor from awesome-kit)`), making the floor logic part of the
  parity evidence the slice-3 gate consumes. No enforcement change (slice 1
  enforces nothing anyway).
- **Risk register** (extends section 10): the residual dependency risk after
  this rule is confined to maintainer-declared floor overrides that are too
  loose -- loud at runtime, identical to the pre-feature failure mode, and
  absent by default since overrides are optional.
