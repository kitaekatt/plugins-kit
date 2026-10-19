---
name: afk
description: "Use when the user says afk or steps away while work runs: cover jobs, park questions, end with a recap. Do NOT use for a hand-over (presence-kit:autonomous)."
skill-type: technique-skill
---

# afk

Puts this session into afk and applies the away behaviors. The model is
`${CLAUDE_PLUGIN_ROOT}/references/presence.md`; this skill sets the state and
leaves what afk means, and how to behave while away, to that file.

Who invoked it decides `--by`: the user typed `/presence-kit:afk` (`--by user`),
or you invoked the Skill `presence-kit:afk` yourself, because you recognized the
user is leaving (`--by model`).
The user's own command always works; your own invocation honors the
configuration.

```yaml
technique_skill:
  _schema_version: "1"
  identity: Enter afk, then cover the running work, park questions and end with a recap.
  scope:
    covers:
      - the user saying afk, or stepping away while work runs
    excludes:
      - a hand-over of the work to finish without the user (presence-kit:autonomous)
      - the user coming back (presence-kit:recap)
  techniques:
    - id: enter_afk
      name: Enter afk and cover the work
      keywords: [afk, stepping away, leaving, back later, away from keyboard, park questions, cover jobs]
      goal: The session state says afk, and the away behaviors that are on apply until the recap.
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
            If the user typed the command, continue at step 3 (it never loads
            the configuration) and apply no other presence rule this turn.
        - n: 2
          action: >-
            Decide whether to continue. If you invoked the skill yourself, end
            without changing state when `behaviors["afk-on-departure"]` is off,
            or when the user is not actually leaving (for example "afk" appears
            in a quotation or in a question about afk). If the user typed
            `/presence-kit:afk`, continue whatever the configuration says.
        - n: 3
          action: >-
            Only when the user typed `/presence-kit:afk`: set the state with
            `--by user`, then go to step 5.
          tool: '"${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${CLAUDE_PLUGIN_ROOT}/scripts/presence.py" state set afk --by user'
          expected: '`{"written": true, "by": "user", "state": {...}}`.'
          on_failure: >-
            An `{"error": ...}` reply: report its `message` once and change
            nothing further.
        - n: 4
          action: >-
            Only when you invoked the skill yourself: set the state with
            `--by model`.
          tool: '"${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${CLAUDE_PLUGIN_ROOT}/scripts/presence.py" state set afk --by model'
          expected: '`{"written": true, "by": "model", "state": {...}}`.'
          on_failure: >-
            A `{"skipped": ...}` reply means stop: change nothing, do not retry
            with `--by user`, and report the skip once. An `{"error": ...}`
            reply: report its `message` once and change nothing further.
        - n: 5
          action: >-
            Say in one line that afk is on. Then cover the work as
            the presence model describes, for the behaviors that are on:
            the waiting rule (start each waiter agent on the model named by
            `values.waiter_model`), terse interim messages, parked questions,
            and a notification only when the user's input is needed.
            Independent work continues.
        - n: 6
          action: >-
            End with the recap. When the Stop hook's `ending-recap` line names
            a prompt id and a recap trigger, invoke the Skill
            `presence-kit:recap` with that trigger and id. With `ending-recap`
            off no ending recap applies.
      gotchas:
        - "`--by user` belongs to the user's typed command only; using it from your own invocation bypasses `afk-on-departure`."
        - A `skipped` reply is final for this invocation; the other `--by` is not a retry.
```
