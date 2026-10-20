# Recap procedure

Follow these steps in order. The arguments are the ones the recap skill was
invoked with: empty (a bare invocation) or `<trigger> <pid>`, where the pid is
the prompt id that a presence-kit hook line or nudge names (`prompt <id>`).
Use it exactly as given; never invent one.

The other documents named below sit in the same folder as this one. Read each
only when its text is not already in your context this session.

**A long recap is a failed recap.** An away recap is read cold by someone who
saw none of the work: outcomes, not activity, in as few lines as the content
allows.

1. **Read the configuration before anything else.** An id that is off does not
   apply.

   ```
   "${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${PRESENCE_KIT_ROOT:?presence-kit is not provisioned}/scripts/presence.py" config
   ```

   Expect one JSON object with `behaviors`, `values` and `layers`. An
   `{"error": "config", ...}` object: report its `message` once. If you invoked
   the skill yourself, end here with no state change. If the user typed the
   command, write the recap but skip the `recap-done` call, and say so in one
   line.

2. **Only when you invoked the skill yourself:** end without changing state
   when the row's id is off, which is `recap-on-return` for `return` and
   `ending-recap` for `afk-ending` and `autonomous-done`. The user's own
   `/presence-kit:recap` always runs.

3. **Read the session state.** `afk_since`, `goal`, `goal_since`, `greeted` and
   `unresolved` feed the window, the row and the "Waiting on you" section.

   ```
   "${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${PRESENCE_KIT_ROOT:?presence-kit is not provisioned}/scripts/presence.py" state get
   ```

   Expect `{"exists": true|false, "state": {...}}`; `exists` false means the
   defaults (present, nothing recorded). Then select the row, its window and
   its closing call from `recap-rows.md`. A bare invocation takes its row from
   `presence` and takes the pid from this turn's presence-kit hook line.

4. **Write Phase 1 for the row,** over its window, in text only and never with
   AskUserQuestion. Use the template, the content rules and the
   decision-selection rules in `recap-templates.md`. A `return` with `greeted`
   true writes no Phase 1 text. "Waiting on you" lists every `unresolved` entry
   and every parked question. Write it before step 5, which clears
   `unresolved` on a return.

5. **Every away row ends here, after the recap text:** record it with the
   row's trigger and the pid, then carry on with the user's message. If no hook
   line gave a prompt id, do not invent one: skip this call and say so in one
   line.

   ```
   "${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${PRESENCE_KIT_ROOT:?presence-kit is not provisioned}/scripts/presence.py" recap-done <trigger> <pid>
   ```

   Expect `{"recorded": "<trigger>", "prompt_id": "<pid>", "state": {...}}`. An
   `{"error": ...}` reply means the recap is not recorded; report its `message`
   once.

6. **If the next user message is a contentless reply,** follow
   `recap-interview.md`. This follows every row, `greeted` included.

## Gotchas

- A return with `greeted` true gets no Phase 1 text, but still gets the
  `recap-done return <pid>` call and, after a contentless reply, the interview.
- Recap-shaped prose without `recap-done` satisfies nothing; the Stop hook asks
  again.
- Phase 1 is text only; an AskUserQuestion call before a contentless reply
  removes the reader's option to answer in one sentence.
