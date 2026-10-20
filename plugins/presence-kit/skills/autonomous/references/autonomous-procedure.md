# autonomous procedure

Two procedures follow: record the hand-over and work the goal, or revoke the
hand-over. The model is `references/presence.md` in the presence-kit plugin
root (its Authority and Modes sections); this procedure sets the state and
leaves what autonomous means to that file.

Who invoked the skill decides `--by`: the user typed `/presence-kit:autonomous`
(`--by user`), or you invoked the Skill `presence-kit:autonomous` yourself
because the user's own message handed the work over (`--by model`). The user's
own command always works; your own invocation honors the configuration.

## Record the hand-over and work the goal

1. **Read the configuration before anything else.** `behaviors` maps each id to
   on or off; `values.waiter_model` names the waiter model. An id that is off
   does not apply.

   ```
   "${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${PRESENCE_KIT_ROOT:?presence-kit is not provisioned}/scripts/presence.py" config
   ```

   Expect one JSON object with `behaviors`, `values` and `layers`. An
   `{"error": "config", ...}` object: report its `message` once. If you invoked
   the skill yourself, end here with no state change. If the user typed the
   command, continue (the `--by user` command never loads the configuration)
   and apply no other presence rule this turn.

2. **Decide whether to continue.** If you invoked the skill yourself, end
   without changing state when `behaviors["autonomous-on-handover"]` is off, or
   when the user's message is not an explicit hand-over. If the user typed
   `/presence-kit:autonomous`, continue whatever the configuration says.

3. **State the granted goal** in one sentence in the user's terms: the text of
   the skill's arguments when it is not empty, else the work the user handed
   over in this conversation. If the user typed the command and no goal can be
   identified, ask once what the goal is before going on; the state command
   refuses an empty goal.

4. **Only when the user typed `/presence-kit:autonomous`:** set the state with
   `--by user`, quoting the goal so the shell passes it as one argument, then
   go to step 6.

   ```
   "${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${PRESENCE_KIT_ROOT:?presence-kit is not provisioned}/scripts/presence.py" state set autonomous --by user --goal "<granted goal>"
   ```

   Expect `{"written": true, "by": "user", "state": {...}}`. An
   `{"error": ...}` reply: report its `message` once and change nothing
   further.

5. **Only when you invoked the skill yourself:** set the state with `--by model`,
   quoting the goal the same way.

   ```
   "${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${PRESENCE_KIT_ROOT:?presence-kit is not provisioned}/scripts/presence.py" state set autonomous --by model --goal "<granted goal>"
   ```

   Expect `{"written": true, "by": "model", "state": {...}}`. A
   `{"skipped": ...}` reply means stop: change nothing, do not retry with
   `--by user`, and report the skip once. An `{"error": ...}` reply: report its
   `message` once and change nothing further.

6. **Say in one line that the goal is recorded.** Then work the goal under the
   autonomous mode in the presence model, with the away behaviors that are on
   (the waiting rule included; start each waiter agent on the model named by
   `values.waiter_model`). Keep a line for each load-bearing decision you take,
   for the recap.

7. **Finish with one recap.** When the Stop hook's `ending-recap` line names a
   prompt id, check the goal: if it is done, invoke the Skill
   `presence-kit:recap` with `autonomous-done <pid>` using that id; if not, keep
   working. With `ending-recap` off no ending recap applies.

## Revoke the hand-over

1. Run step 1 above (the configuration) first.

2. When the user revokes the hand-over, clear the goal. Presence is not changed
   by this command.

   ```
   "${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${PRESENCE_KIT_ROOT:?presence-kit is not provisioned}/scripts/presence.py" state set normal
   ```

   Expect `{"written": true, "by": "user", "state": {...}}`. An
   `{"error": ...}` reply: report its `message` once.

## Gotchas

- `--by user` belongs to the user's typed command only; using it from your own
  invocation bypasses `autonomous-on-handover`.
- A `skipped` reply is final for this invocation; the other `--by` is not a
  retry.
