---
name: autonomous
description: "Use when the user hands work over to finish without them: decide instead of asking, recap at the end. Do NOT use for a plain afk or a quiet user."
skill-type: technique-skill
---

# autonomous

Records an explicit hand-over (a granted goal) and works it through. The model
is `${CLAUDE_PLUGIN_ROOT}/references/presence.md` (its Authority and Modes
sections); this skill sets the state and leaves what autonomous means to that
file.

Who invoked it decides `--by`: the user typed `/presence-kit:autonomous`
(`--by user`), or you invoked the Skill `presence-kit:autonomous` yourself
because the user's own message handed the work over (`--by model`). The user's
own command always works; your own invocation honors the configuration.

Arguments: $ARGUMENTS

```yaml
technique_skill:
  _schema_version: "1"
  identity: Record the user's hand-over as the granted goal, then work it to done and end with one recap.
  scope:
    covers:
      - the user handing work over to finish without them
      - the user revoking such a hand-over
    excludes:
      - a plain afk, or a user who is only quiet (presence-kit:afk)
      - scheduled tasks, loops, routines and headless runs, which are not hand-overs
  techniques:
    - id: enter_autonomous
      name: Record the hand-over and work the goal
      keywords: [autonomous, hand over, take it from here, finish without me, decide for me, go ahead without asking]
      goal: The session state holds the granted goal, and the work proceeds under the autonomous mode until the goal is done or revoked.
      steps:
        - n: 1
          action: >-
            Read the configuration before anything else. `behaviors` maps each
            id to on or off; `values.waiter_model` names the waiter model. An
            id that is off does not apply.
          tool: '"${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${CLAUDE_PLUGIN_ROOT}/scripts/presence.py" config'
          expected: One JSON object with `behaviors`, `values` and `layers`.
          on_failure: >-
            An `{"error": "config", ...}` object: report its `message` once.
            If you invoked the skill yourself, end here with no state change.
            If the user typed the command, continue (the `--by user` command
            never loads the configuration) and apply no other presence rule
            this turn.
        - n: 2
          action: >-
            Decide whether to continue. If you invoked the skill yourself, end
            without changing state when `behaviors["autonomous-on-handover"]`
            is off, or when the user's message is not an explicit hand-over.
            If the user typed `/presence-kit:autonomous`, continue whatever the
            configuration says.
        - n: 3
          action: >-
            State the granted goal in one sentence in the user's terms: the
            text of Arguments above when it is not empty, else the work the
            user handed over in this conversation. If the user typed the
            command and no goal can be identified, ask once what the goal is
            before going on; the state command refuses an empty goal.
        - n: 4
          action: >-
            Only when the user typed `/presence-kit:autonomous`: set the state
            with `--by user`, quoting the goal so the shell passes it as one
            argument, then go to step 6.
          tool: '"${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${CLAUDE_PLUGIN_ROOT}/scripts/presence.py" state set autonomous --by user --goal "<granted goal>"'
          expected: '`{"written": true, "by": "user", "state": {...}}`.'
          on_failure: >-
            An `{"error": ...}` reply: report its `message` once and change
            nothing further.
        - n: 5
          action: >-
            Only when you invoked the skill yourself: set the state with
            `--by model`, quoting the goal the same way.
          tool: '"${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${CLAUDE_PLUGIN_ROOT}/scripts/presence.py" state set autonomous --by model --goal "<granted goal>"'
          expected: '`{"written": true, "by": "model", "state": {...}}`.'
          on_failure: >-
            A `{"skipped": ...}` reply means stop: change nothing, do not retry
            with `--by user`, and report the skip once. An `{"error": ...}`
            reply: report its `message` once and change nothing further.
        - n: 6
          action: >-
            Say in one line that the goal is recorded. Then work the goal under
            the autonomous mode in the presence model, with the away
            behaviors that are on (the waiting rule included; start each
            waiter agent on the model named by `values.waiter_model`). Keep a
            line for each load-bearing decision you take, for the recap.
        - n: 7
          action: >-
            Finish with one recap. When the Stop hook's `ending-recap` line
            names a prompt id, check the goal: if it is done, invoke the Skill
            `presence-kit:recap` with `autonomous-done <pid>` using that id;
            if not, keep working. With `ending-recap` off no ending recap
            applies.
      gotchas:
        - "`--by user` belongs to the user's typed command only; using it from your own invocation bypasses `autonomous-on-handover`."
        - A `skipped` reply is final for this invocation; the other `--by` is not a retry.
    - id: revoke_autonomous
      name: Revoke the hand-over
      keywords: [revoke, cancel autonomous, ask me again, stop deciding for me, take back, normal mode]
      goal: The granted goal is cleared and decisions go back to the user.
      steps:
        - n: 1
          action: Run step 1 above (the configuration) first.
        - n: 2
          action: >-
            When the user revokes the hand-over, clear the goal. Presence is
            not changed by this command.
          tool: '"${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${CLAUDE_PLUGIN_ROOT}/scripts/presence.py" state set normal'
          expected: '`{"written": true, "by": "user", "state": {...}}`.'
          on_failure: "An `{\"error\": ...}` reply: report its `message` once."
```
