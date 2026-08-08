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

**Two presets is not the fixed shape.** More are on the table, and each one that
a real cohort recognizes itself in removes interviews -- which is the point,
since the interview is the expensive path. Two constraints bound it:

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

## The answer is durable, and bootstrap owns it

**Whatever the user answers has to survive** -- the session, the machine, and
the plugin version. A posture captured in session state is not captured; it is
re-asked, which is the failure the pattern exists to remove.

**The home is `bootstrap`.** Every plugin in this marketplace but one declares a
`bootstrap.json` and depends on bootstrap (the exception is `agent-glue`), so
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
