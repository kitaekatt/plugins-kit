# The first-run experience: presets, and an interview as the escape hatch

How a plugins-kit plugin should establish a user's preferences the first time it
runs, so that later invocations stop asking.

## The problem this solves

A capable plugin accumulates settings whose right value depends on the person,
not on the repo: how much it reads, how much it spends, how loud it is, whether
it confirms before an expensive operation. Two failure modes bracket the space.

**Silent defaults** pick for the user invisibly. When the levels differ
materially -- a bounded sample versus an exhaustive read -- a silent wrong pick
is expensive in both directions and the user cannot see it happen.

**Per-run prompts** make the choice visible but charge for it every time. A
prompt on the fifth invocation of the same command is not diligence; it is a
plugin that failed to learn something it was already told.

The fix is to ask ONCE, early, at a level of abstraction the user can actually
answer, and to treat any later prompt as evidence that the first-run capture was
incomplete.

## The first question: presets, plus an interview

Early in the plugin experience, ask ONE question -- what experience do you want
-- offering a small set of presets plus an interview:

| Option | Meaning |
|---|---|
| **Conservative defaults** | Cheap, bounded, low-surprise. Nothing expensive runs without being asked for by name. |
| **Power-user defaults** | The experience a Claude Code power user should expect: fuller analysis, fewer confirmations, higher cost accepted as normal. |
| **Interview me** | No preset fits; ask me a small number of questions and set the individual values. |

The presets are the point. Most users take one and answer nothing further --
that is what makes the pattern cheap. The interview exists so that "no preset
fits" has an answer other than reading configuration documentation.

**This question is the foundation, and it is deliberately revisitable.** Later
questions are framed against its answer, so getting it wrong must be cheap to
correct; a user changes their posture without re-onboarding, and the change
re-derives the settings the preset owns.

**The count is settled: two presets plus the interview, filling three of the
four slots.** The count was left open deliberately -- "two presets is not the
fixed shape" -- until a capability audit of every plugin and skill in the
marketplace could answer it on evidence rather than intuition. Two candidate
arguments for a third preset were tested and both failed:

- A candidate third preset of "more plugins, no extra opinions" turned out not
  to be a level BETWEEN the other two. The cluster it names (see Ambient QoL,
  below) IS the default tier -- it defines the boundary rather than adding a
  slot beside it.
- A candidate case for "custom occupies the middle" also failed, on
  orthogonality rather than expressiveness: a user who needs Perforce support is
  not partway between conservative and power-user, they are off that axis
  entirely (see Rigor and domain are orthogonal axes, below). No amount of
  slider between two points on one axis reaches a point that is not on it.

The two constraints that bounded the search still bound the answer, and remain
true independent of any specific plugin roster:

- **The slot budget is hard.** `AskUserQuestion` allows four options and the
  interview occupies one, so at most three presets. There is no fifth slot to
  grow into.
- **A preset must be self-identifying.** Someone should recognize themselves in
  it from the label alone, before knowing anything about the plugin. A preset
  that reads as "somewhere between the other two" is worse than absent: it makes
  the one question EVERY user sees harder to answer, in order to save a few
  people an interview they would have completed in under a minute.

Name presets by CALIBRATION, not by mechanics. "Power user" and "conservative"
describe who the setting is for; "exhaustive read, three passes" describes what
it does and means nothing to someone who has not yet used the plugin.

## The interview: fewest questions necessary

The interview's quality metric is question COUNT, not coverage. A setting that
can be derived is not a question.

Ask a question only when all four hold:

1. **The presets actually differ on it.** A setting both presets share is not a
   preference; it is a default. It never becomes a question.
2. **It is not derivable from an earlier answer.** Answers cascade. Someone who
   accepts unattended expensive runs has answered several downstream questions
   at once; asking them again reads as not having listened.
3. **A wrong value is not cheaply correctable.** If the user finds out
   immediately and fixes it with a flag, ship a default and disclose it.
4. **The user can answer it without knowing the implementation.** A question
   requiring the reader to already understand the plugin's internals is a
   documentation failure wearing a question mark.

Mechanics:

- **One `AskUserQuestion` call, batched, at most four questions.** Never drip one
  question per turn.
- **Order by cascade depth** -- the question that eliminates the most downstream
  questions goes first.
- **Every option states its consequence**, not a restatement of its label.
- **Stop early.** If the answers so far determine the rest, stop asking. Ending
  the interview after two questions is a success, not an incomplete run.

More than four genuine questions means the presets are wrong. Fix the presets.

## A preset controls install footprint, not skill availability

The distinction that makes the two presets tractable: a preset decides which
plugins provision by default, not which skills exist. Skills are always
present once a plugin is installed -- the marketplace has no mechanism that
hides a skill from an installed plugin -- so the only thing a preset can
change is which plugins get installed and enabled without being asked for by
name.

This matters because it locates where harm can actually originate. A capability
audit of every plugin and skill in the marketplace found that **every harm case
traced to a `bootstrap.json`** (an unattended install, a `required_fields`
prompt that interrogates the user for something they may not own, a hook
registered on every session) **or a SessionStart script -- never to a skill
merely being loadable.** A skill sitting unused costs nothing; an elevated
system package install, or a write into the user's project, costs something
whether or not the user ever invokes the skill that caused it.

Per-skill opt-in is a solved problem independent of presets: several skills
already ship `disable-model-invocation: true` so they load without
autonomously triggering. A preset does not need to reach down to that level --
its job is coarser and stops at the plugin boundary. Name a preset for what it
PROVISIONS, not for a stance on how each skill inside it behaves.

## The two presets: default and elective

The audit above is also what settles the tier count two sections up. It
resolved into two presets along a single line: what should provision on a
machine with no stated preference, versus what a user explicitly opts into
because they want the tooling to hold them to a standard.

**The default preset** -- `bootstrap`, `bootstrap-stuck-fix`, `claude-ui-kit`.
A machine that provisions its own dependencies, repairs a wedged plugin
registry, and has a status line. Nothing in this set changes how the user
works, gates anything, or writes into their repo. `bootstrap` is mandatory and
`bootstrap-stuck-fix` is a dependency-free safety net, so `claude-ui-kit` is
the only genuinely elective member -- the default preset is honestly "the
substrate, plus a status line." Self-identifying as: *I want good tooling that
does not have opinions about my process.*

**The elective preset adds the rigor cluster** -- `git-kit`, `skills-kit`,
`awesome-kit`. Multi-agent review before push; SKILL.md, CLAUDE.md, and
project-doc auditing and authoring; durable task folders; background-agent
delegation under a rendered routing policy; plus the smaller conveniences
those plugins carry (recap, the poster). This preset changes how
the user is made to work -- review becomes a gate, task folders want
committing, delegated work spends tokens without a hard ceiling -- so it is
deliberately not the default, and it is deliberately the preset the picker
copy should make attractive: a user who takes the default preset sees almost
nothing happen, which is correct but will not sell itself.

Everything not named in either preset is on request, for either of two
reasons: it requires the user to already own something (see Domain, below), or
it is simply not judged worth defaulting for everyone (a plugin can lack any
ownership prerequisite and still not clear that bar). "On request" is a single
rule covering both.

## The four capability clusters

The audit that produced the two presets classified every plugin's opinions
into four clusters, and the clusters are worth keeping as vocabulary for
future preset or routing decisions, not only for the two presets that resulted
from this one:

| Cluster | What it changes |
|---|---|
| **Ambient QoL** | How Claude Code looks and what it reports -- a status line, a poster. |
| **Rigor** | How the user is made to work -- review gates, audits, durable task tracking. |
| **Domain** | Requires the user to already own the thing the plugin automates -- a depot, a UE project, a Hue bridge, an account. |
| **Architecture kits** | Meaningful only if the user is building the shape the kit assumes. |

**Rigor and Domain are orthogonal axes, not adjacent points on one ladder.**
`p4-kit` is the falsifier for any design that treats them as one dimension: its
*capability* is pure Rigor -- it runs the identical multi-agent review pipeline
as `git-kit`, sharing `bootstrap_lib/code_review/` -- but its *delivery* is
pure Domain, requiring a depot plus an elevated system-wide Perforce install.
Placing it in the opinionated preset elevate-installs Perforce on everyone who
does not use Perforce; leaving it out costs Perforce users the tier's headline
capability for no domain-shaped reason. A one-dimensional ladder cannot place
`p4-kit` correctly at any point on it -- which is why domain-shaped plugins sit
outside both presets entirely rather than forming a third tier between them.

**A preset must be computed over its dependency closure, not a hand-written
list.** `awesome-kit`'s `bootstrap.json` auto-installs `skills-kit`
(`"install": "auto"`), so a preset that lists `awesome-kit` without also
listing `skills-kit` is not wrong today only by accident -- it is one
dependency edit away from silently installing something the preset never
named. Compute the closure; do not hand-maintain the expansion.

## Relationship to per-run prompting

A per-run prompt is the FALLBACK for a preference that was never captured, not a
parallel mechanism. The precedence:

1. An explicit flag on the invocation -- always wins, always silent.
2. A captured first-run preference -- applies silently.
3. Neither: prompt if the choice is expensive in both directions, otherwise take
   a default and **disclose it in keyword form** (`defaults: depth=basic`) so it
   can be corrected.

**Worked instance.** md-domain's coverage verb asks which analysis depth to run
when the invocation does not say, because a silent wrong pick either opts the
user into an extreme run or hands them a bounded sample they may read as
exhaustive (`skills/md-domain/references/standards/coverage-standards.md`,
"Selecting the depth"). Under this pattern that prompt is correct only while no
first-run preference exists; a user who chose power-user defaults has already
answered it, and coverage should run advanced without asking.

## Disclosure survives the presets

Choosing a preset does not exempt a plugin from disclosing what it accepted. The
rule is unchanged: whenever a run takes a value the user did not express, name
it in keyword form on one line, listing only the keys that fell to a default. A
preset makes the values PREDICTABLE; it does not make them invisible.

**The general pattern, worked out for a footprint-having plugin, is: act
silently when healthy, log the action when you change something, and
refuse-plus-explain when the user already has their own answer.** `claude-ui-kit`
is the model. Its status-line installer
(`plugins/claude-ui-kit/scripts/install_statusline.py`) handles four cases:

- No status line anywhere -> install it, and emit an always-visible `ctx.log`
  entry naming the action (`install_statusline.py:127-133`).
- A status line is already ours and identical -> `ctx.log_ok`, which is
  verbose-only. Silent when healthy (`:137-139`).
- A status line is ours but stale, or a legacy absolute path -> rewrite it and
  name the migration performed (`:141-159`).
- A status line belongs to someone else -> `ctx.add_failure("statusline_conflict",
  ...)` at `:162`, which does **not** touch it. The user-facing message says it
  will not overwrite the existing line and names the opt-in phrase to replace
  it; the agent-facing message says `DO NOT modify it` and to explain that
  `claude-ui-kit` is installed but inactive.

The fourth case is not disclosure at all -- it is declining to act, which is
the stronger move whenever the user's existing state already encodes a
decision. A plugin that would silently overwrite that state is not disclosing
a default; it is discarding a choice the user already made, and no keyword-form
notice afterward repairs that.

## The answer is durable, and bootstrap owns it

**Whatever the user answers has to survive** -- the session, the machine, and
the plugin version. A posture captured in session state is not captured; it is
re-asked, which is the failure the pattern exists to remove.

**The home is `bootstrap`.** Every plugin in this marketplace declares a
`bootstrap.json` and depends on bootstrap, so
bootstrap is the single layer every other plugin can rely on being present. It
already owns exactly this job: it runs at SessionStart, provisions per-user
config, and propagates it across the fleet. Storing the posture anywhere else
would build a second configuration mechanism beside the one that already
converges every machine.

Consequences that follow, rather than needing separate decisions:

- **Durability and propagation are inherited**, not re-solved. The posture is
  bootstrap-managed config and moves like the rest of it.
- **One posture, asked once**, rather than once per plugin. A per-plugin
  question is more precise and asks N times; the whole value of the pattern is
  that the user answers early and then stops being asked.
- **A plugin reads the posture; it does not own it.** A plugin may still expose
  its own finer settings, but it resolves the coarse posture from bootstrap and
  never re-onboards the user for its own copy.
- **A plugin without a `bootstrap.json` cannot assume the posture exists.** It
  reads it if present and falls back to conservative defaults with disclosure.

**The onboarding record must live in a file separate from `~/.claude/bootstrap.json`
itself.** That manifest layer already exists on established developer
machines -- it is the ordinary place a user hand-declares a project or
personal dependency, described in the fleet's own configuration docs -- so
gating the picker on that file's existence would guarantee the picker never
fires for exactly the users who have used bootstrap the longest. The
onboarding answer belongs in its own record (for example,
`~/.claude/plugins/data/plugins-kit/bootstrap/experience.json`), because it
answers a different question than `bootstrap.json` does: the manifest declares
dependencies and is meant to be hand-edited; the onboarding record stores a
one-time answer and must never be silently re-expanded by a later bootstrap
run, or a hand edit to it would be reverted the same way a re-expanded preset
would be (see the interview and preset rules above).

**An absent onboarding record means UNANSWERED, not an implicit default
preset.** Collapsing the two loses the ability to ever ask: if "no record"
were treated as "chose the default preset," there would be no remaining signal
to distinguish a user who was never offered the question from one who
answered it. The gate stays two-valued only if absence and an explicit answer
are kept distinct -- present means never ask again, absent means ask.

## Open design questions

Deliberately unanswered here -- recorded so they are decided rather than
invented at implementation time.

- **Trigger.** What counts as "early in the plugin experience" -- the first
  session after installing any plugin, or the first invocation of a skill that
  actually has an expensive mode? Asking at install time reaches everyone but
  asks before the user has context to answer.
- **New settings after onboarding.** When a plugin later adds a setting the
  posture did not cover, does it re-prompt, or take the preset's implied value
  silently and disclose?
- **Per-plugin override.** Does a plugin get to deviate from the fleet posture
  for its own expensive operation, and if so is that a flag, a config key, or
  a second question the plugin is allowed to ask once?
- **Non-interactive first run.** An unattended dispatch cannot be interviewed;
  presumably it takes conservative defaults and discloses without persisting a
  posture the user never chose -- but persisting-or-not should be stated.
