# Deferred Requirements

How bootstrap handles a precondition that only SOME capability needs: record it,
do not ask, and let the ACTION that needs it ask at the moment of need.

Audience: anyone writing a `custom_bootstrap.py`, and skill authors wiring a
requirement their skill depends on.

## Motivation

Bootstrap's default channel for an unmet precondition is `ctx.add_failure`,
which aggregates into fix-all: the user is shown the problem at session start,
on every machine, every session, until it is resolved. That is right for a
precondition the machine genuinely needs -- a missing tool, a broken venv, an
unresolvable pin. The session cannot do its job without it.

It is wrong for a precondition that is only a precondition SOMETIMES. A paid
API credential, an opt-in specialty plugin, a hardware-specific SDK: a
developer who never invokes the capability does not have an unmet requirement,
they have no requirement at all. Escalating it anyway produces a prompt about
work the session was never going to do -- and because it looks exactly like a
real failure, it trains the user to skim past the fix-all block that will one
day carry something urgent.

The reverse failure is just as bad: dropping the requirement entirely. Then the
capability fails at the moment of need with a raw `401`, a `KeyError`, or a
`ModuleNotFoundError`; the user has no idea what is missing or who owns it; and
nothing recorded the perfectly good diagnosis bootstrap already made.

A deferred requirement is the middle position: **detected early, asked late.**
Bootstrap does the detection, because that is when it has the context and the
credentials to check. The ask happens at the point of need, because that is the
one moment the user has the context to decide.

## The rule

> Escalate a precondition only if a developer who never invokes the capability
> would still notice it was unmet. Otherwise defer it.

Applied:

| Precondition | Channel |
|---|---|
| A tool the project's build needs | `add_failure` |
| A broken or missing venv | `add_failure` |
| An unresolvable marketplace pin | `add_failure` |
| A credential for a paid API one skill calls | `add_deferred_requirement` |
| An opt-in specialty plugin | `add_deferred_requirement` (see below) |
| An SDK only used on GPU machines | `add_deferred_requirement` |

## The API

```python
ctx.add_deferred_requirement(
    "openrouter_credential",
    user_msg="... nothing to do until you run something that calls the API",
    agent_msg="... the prepared statement the skill presents verbatim",
    satisfied_by="llm-scripting-kit set-key",
)
```

It is the exact counterpart to `add_failure`, and deliberately produces none of
what `add_failure` produces: no fix-all entry, no elevation queue task, no
session-start prompt. The engine writes the records to
`<plugin data dir>/deferred_requirements.json`:

```json
{
  "plugin": "llm-scripting-kit",
  "updated": "2026-07-29T00:00:00Z",
  "requirements": [
    {
      "name": "openrouter_credential",
      "plugin": "llm-scripting-kit",
      "user_msg": "...",
      "agent_msg": "...",
      "satisfied_by": "llm-scripting-kit set-key"
    }
  ]
}
```

Notes that are load-bearing:

- **The file is the handoff.** The point-of-need code reads the prepared
  statement from it rather than carrying its own paraphrase, so there is ONE
  authored copy of the ask. A skill that restates the instructions is how the
  two drift and a user ends up following stale advice.
- **It is rewritten in full every pass, and REMOVED when the plugin defers
  nothing.** That is what makes a satisfied requirement disappear, with no
  separate clear step for anyone to forget. Do not treat the file's mere
  existence as stale-safe state -- if it is there, the last completed pass
  still wanted it there.
- **A script that raises part-way does not rewrite it.** It never finished
  deciding what it needs, so the previous pass's record is better evidence than
  a truncated one.
- **Log with `log_ok`, not `log`.** An unmet deferred requirement is the
  EXPECTED state on a machine that does not use the capability, so it belongs
  in verbose output. Using `log` puts the nag back by a different door.
- **Guard the call in a stdlib-only script** that may run against an older
  engine: `getattr(ctx, "add_deferred_requirement", None)`. Fall back to
  silence, never to `add_failure` -- the fallback would resurrect the very
  prompt the deferral removes.

## Consuming it at the point of need

The action that needs the requirement owns three steps, mirroring the
three-part flow in [action-triggered-install.md](action-triggered-install.md):

1. **Preflight.** Check the requirement directly (is the key resolvable? is
   the shared lib on disk?) BEFORE doing the work. Cheap and side-effect free.
2. **Ask, using the recorded statement.** On a miss, read
   `deferred_requirements.json`, find the entry by `name`, and present its
   `agent_msg`. Name the capability that is unavailable and the fallback if one
   exists (an offline mode, a dry run). Do not proceed without an answer.
3. **Satisfy and retry.** Run whatever `satisfied_by` names, then retry the
   action. Nothing needs a restart.

A previously-declined requirement is not settled for the rest of the session's
unrelated work: re-asking on the next genuine need is correct.

## Relationship to action-triggered install

Action-triggered plugin install is the INSTANCE of this pattern where the
missing precondition happens to be a plugin. Everything above applies to it;
[action-triggered-install.md](action-triggered-install.md) adds the
install-specific mechanics (`install: "manual"`, the shared-lib path check,
`claude plugin install`, the mid-session relaunch). Read this doc for the
principle and that one for the plugin case.

## Wiring it as a plugin author

1. In `custom_bootstrap.py`, replace the `add_failure` call for the optional
   precondition with `add_deferred_requirement`, and downgrade its `log` to
   `log_ok`.
2. Write the `agent_msg` as the finished ask -- verbatim-presentable, naming
   the capability, the exact command, and any transcript/security caveat. This
   is the copy the skill will show; it should need no editing at the point of
   need.
3. Add a short **Requirements** note to the SKILL.md of every skill that needs
   it, stating the preflight, where the recorded statement lives, and the
   no-requirement fallback.
4. Do not gate unrelated parts of the skill on it. Preflight inside the action
   that needs it, so every other action keeps working without it.
