# afk procedure

Follow these steps in order. They put this session into afk and apply the away
behaviors. The model is `references/presence.md` in the presence-kit plugin
root; this procedure sets the state and leaves what afk means, and how to
behave while away, to that file.

Who invoked the skill decides `--by`: the user typed `/presence-kit:afk`
(`--by user`), or you invoked the Skill `presence-kit:afk` yourself, because
you recognized the user is leaving (`--by model`). The user's own command
always works; your own invocation honors the configuration.

1. **Read the configuration before anything else.** `behaviors` maps each id to
   on or off; `values.waiter_model` names the waiter model. An id that is off
   does not apply.

   ```
   "${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${PRESENCE_KIT_ROOT:?presence-kit is not provisioned}/scripts/presence.py" config
   ```

   Expect one JSON object with `behaviors`, `values` and `layers`. An
   `{"error": "config", ...}` object: report its `message` once. If you invoked
   the skill yourself, end here with no state change. If the user typed the
   command, continue at step 3 (it never loads the configuration) and apply no
   other presence rule this turn.

2. **Decide whether to continue.** If you invoked the skill yourself, end
   without changing state when `behaviors["afk-on-departure"]` is off, or when
   the user is not actually leaving (for example "afk" appears in a quotation or
   in a question about afk). If the user typed `/presence-kit:afk`, continue
   whatever the configuration says.

   One exception: when a presence-kit hook line for this prompt (id
   `afk-on-departure`, beginning "set the user afk" or "kept the user afk")
   already set afk and the user is not leaving, undo it with the command below,
   tell the user once in one line, follow that line's own return-recap
   instruction if it has one, and end here.

   ```
   "${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${PRESENCE_KIT_ROOT:?presence-kit is not provisioned}/scripts/presence.py" state set present
   ```

   Expect `{"written": true, "by": "user", "state": {...}}` with presence
   present; run it only in the undo case. An `{"error": ...}` reply: report its
   `message` once and change nothing further.

3. **Only when the user typed `/presence-kit:afk`:** set the state with
   `--by user`, then go to step 5.

   ```
   "${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${PRESENCE_KIT_ROOT:?presence-kit is not provisioned}/scripts/presence.py" state set afk --by user
   ```

   Expect `{"written": true, "by": "user", "state": {...}}`. An
   `{"error": ...}` reply: report its `message` once and change nothing
   further.

4. **Only when you invoked the skill yourself:** set the state with `--by model`.

   ```
   "${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${PRESENCE_KIT_ROOT:?presence-kit is not provisioned}/scripts/presence.py" state set afk --by model
   ```

   Expect `{"written": true, "by": "model", "state": {...}}`. A
   `{"skipped": ...}` reply means stop: change nothing, do not retry with
   `--by user`, and report the skip once. An `{"error": ...}` reply: report its
   `message` once and change nothing further.

5. **Say in one line that afk is on.** Then cover any running work as the
   presence model describes, for the behaviors that are on: the waiting rule
   (start each waiter agent on the model named by `values.waiter_model`), terse
   interim messages, parked questions, and a notification only when the user's
   input is needed. Independent work continues.

6. **End with the recap.** When the Stop hook's `ending-recap` line names a
   prompt id and a recap trigger, invoke the Skill `presence-kit:recap` with
   that trigger and id. With `ending-recap` off no ending recap applies.

## Gotchas

- `--by user` belongs to the user's typed command only; using it from your own
  invocation bypasses `afk-on-departure`.
- A `skipped` reply is final for this invocation; the other `--by` is not a
  retry.
