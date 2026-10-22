# Claude Code plugin platform reference

Static Claude Code platform facts that plugin development in this repo relies on but rarely needs inline: the hook-JSON output contract and the plugin cache/registry layout on disk. These are CC platform behavior, not plugins-kit-specific -- the root `CLAUDE.md` links here instead of carrying the tables. When in doubt, the upstream docs are the source of truth.

## Hook JSON Format

**Official docs**: https://code.claude.com/docs/en/hooks (canonical reference). When in doubt, fetch this URL -- it is the source of truth.

On exit 0, stdout is parsed as JSON. Exit 2 = blocking error (stderr fed to Claude). Other exits = non-blocking error. JSON is only processed on exit 0.

**Universal fields** (all events):

| Field | Default | Description |
|-------|---------|-------------|
| `continue` | `true` | If `false`, Claude stops entirely. Takes precedence over other decisions |
| `stopReason` | none | Message shown to user when `continue` is `false`. Not shown to Claude |
| `suppressOutput` | `false` | If `true`, hides stdout from verbose mode |
| `systemMessage` | none | Shown to user only -- Claude never sees it |

**Event-specific decision control**:

| Event | Decision pattern | To Claude |
|-------|-----------------|-----------|
| SessionStart | None | `hookSpecificOutput.additionalContext` or plain text stdout |
| UserPromptSubmit | `decision: "block"` + `reason` | `hookSpecificOutput.additionalContext` or plain text stdout |
| PreToolUse | `hookSpecificOutput.permissionDecision` (allow/deny/ask) | `hookSpecificOutput.additionalContext` |
| PostToolUse | `decision: "block"` + `reason` | `hookSpecificOutput.additionalContext` |
| PostToolUseFailure | None | `hookSpecificOutput.additionalContext` |
| Stop | `decision: "block"` + `reason` | `reason`, or `hookSpecificOutput.additionalContext` (shown as "Stop hook feedback") |
| SubagentStop | `decision: "block"` + `reason` | `reason` (`hookSpecificOutput` not probed) |
| SubagentStart | None | `hookSpecificOutput.additionalContext` |
| Notification | None | `hookSpecificOutput.additionalContext` |
| PermissionRequest | `hookSpecificOutput.decision.behavior` (allow/deny) | -- |
| ConfigChange | `decision: "block"` + `reason` | -- |

`hookSpecificOutput` always requires `hookEventName` set to the event name.

> **plugins-kit note -- bootstrap background mode.** The bootstrap engine writes output to a pending file, which the UserPromptSubmit hook reads and re-emits as its own stdout. Stop hooks accept `hookSpecificOutput.additionalContext` (see below), but the engine re-emits through UserPromptSubmit, which runs at the start of a turn. (This wrinkle is also summarized inline in the root `CLAUDE.md`.)

### Stop and UserPromptSubmit payloads and limits

Observed on Claude Code 2.1.302 (Windows, 2026-10-16) with scratch Stop and UserPromptSubmit hooks that dump stdin, under `claude -p` with one background `sleep`:

- Every payload carries `prompt_id`. The Stop and UserPromptSubmit payloads of one prompt share it; the turn started by the background task's completion notice got a new one.
- The Stop payload carries `stop_hook_active`, `last_assistant_message`, `background_tasks` and `session_crons`. `background_tasks` is an array of `{id, type, status, description, command}`; it held one `running` shell task when Claude ended its first turn, and `[]` at the final stop. `session_crons` was `[]`.
- Stop fires when a turn ends even while a background task runs, and again after the task's notice starts a new turn.
- The task-completion notice arrived as a UserPromptSubmit whose `prompt` is a `<task-notification>` block. The payload has no field that marks a human origin.

From the canonical hooks reference only (not probed): UserPromptSubmit also fires for scheduled tasks, background-agent reports and inter-session messages, so none of these payloads separates a human prompt from an injected one. Stop continuations (`decision: "block"`) are capped at 8 consecutive (`CLAUDE_CODE_STOP_HOOK_BLOCK_CAP`); the count resets when Claude calls a tool. Stop `additionalContext` support also comes from that reference; the probe only dumped payloads, so the "Stop hook feedback" display was not observed here.

## Session environment: telling an attended session from an unattended one

A hook process, and every child it spawns, inherits the session's environment. Two variables
answer "is a human at the prompt", and only one of them is usable:

| Session | `CLAUDE_CODE_SESSION_ATTENDED` | `CLAUDE_CODE_ENTRYPOINT` |
|---|---|---|
| interactive `claude` | `1` | `cli` |
| `claude -p "..."` | `0` | `sdk-cli` |
| `claude --bg "..."` | `0` | `cli` |

Measured on Claude Code 2.1.273 (macOS) with scratch SessionStart and UserPromptSubmit hooks, run
with every inherited `CLAUDE_*` variable stripped so the child could not copy the parent's values.
The hooks saw exactly the variables a Bash-tool child sees.

**Gate on `CLAUDE_CODE_SESSION_ATTENDED == "1"` and nothing else, failing closed** on unset, `0`,
or any other value. `CLAUDE_CODE_ENTRYPOINT` stays `cli` for a background session, so an
entrypoint-based check treats an unattended session as attended and fires a prompt nobody can
answer. The binary computes the attended value from an internal predicate when it spawns a
session rather than copying it in, and it recognizes many entrypoint values besides these
(`sdk-ts`, `sdk-py`, `claude-vscode`, `remote`, `local-agent`, `github-action`, ...), which is the
second reason not to enumerate them.

`CLAUDE_CODE_SESSION_ID` carries the session id and is what a per-session marker file should be
keyed on: the Python bootstrap engine takes no session-id argument, so the environment is the only
place it can read one.

bootstrap's profile prompt is the consumer of both (`bootstrap_lib/profiles.py::should_prompt`).

## Session identity across resume, measured

Measured 2026-10-21 on Claude Code 2.1.306, macOS (Darwin 25.6, arm64), with a throwaway plugin
loaded through `--plugin-dir` whose `hooks.json` carried four SessionStart entries: no matcher,
matcher `resume`, matcher `clear`, and an `async: true` entry that sleeps 3 s before logging. Each
entry appended the hook input, `CLAUDE_CODE_SESSION_ID`, `CLAUDE_CODE_BRIDGE_SESSION_ID`,
`CLAUDE_PID` and the hook's `$PPID` to a log; a Bash tool command in the session printed the same
variables. Interactive sessions were driven through a pseudo-terminal (`/clear` and `/exit` typed
at the prompt); `claude -p` runs with `--resume`, `--continue` and `--fork-session` were a
cross-check and gave the same ids and sources. Ids below are the first 8 characters; S1, S2 and S3
are distinct session ids and P1..P5 distinct processes.

| Case | hook `source` | hook `session_id` = Bash `CLAUDE_CODE_SESSION_ID` | `CLAUDE_PID` | `transcript_path` | matcher entries that fired |
|---|---|---|---|---|---|
| new session | `startup` | S1 | P1 | `<project dir>/S1.jsonl` | none (no-matcher and async only) |
| `/clear` in that session | `clear` | S2 (new) | P1 (unchanged) | `<project dir>/S2.jsonl` | `clear` |
| `/exit` | process exits; ids unchanged | - | - | - | SessionEnd not probed |
| `claude --resume S1` (the pre-clear id) | `resume` | S1 (kept) | P2 (new) | `S1.jsonl` | `resume` |
| `claude --resume S2` | `resume` | S2 (kept) | P3 (new) | `S2.jsonl` | `resume` |
| `claude --continue` | `resume` | S2 (the most recent session in that directory) | P4 (new) | `S2.jsonl` | `resume` |
| `claude --resume S2 --fork-session` | `fork` | S3 (new) | P5 (new) | `S3.jsonl` | none: neither `resume` nor `clear` |

- `CLAUDE_PID` in the Bash tool, in a hook command, and the hook's `$PPID` were the same number
  in every case: the Claude process itself. It changed on every new process and only then.
- The hook input's `session_id` equalled `CLAUDE_CODE_SESSION_ID` in the hook's own environment
  and in the Bash tool, in every case, including the first hook after `/clear`.
- Hook input fields: `session_id`, `transcript_path`, `cwd`, `hook_event_name`, `source` on every
  source; `resume` and `fork` also carried `scratchpad_dir`, `seconds_since_last_response`,
  `context_tokens`, `prompt_cache_likely_expired` and `estimated_cache_write_usd`.
- The async entry (3 s sleep) ran to completion and logged in every interactive case (startup,
  `clear`, `resume`, `fork`) and in the `-p` runs, which lasted longer than 3 s. A `-p` run that
  ends sooner was not tested.
- `CLAUDE_CODE_BRIDGE_SESSION_ID` is not a session key. A nested child inherits the parent's value
  when the parent exports one. In a clean interactive session it was absent from the SessionStart
  hook environment on `startup`, `resume` and `fork`, present in the Bash tool environment and (after
  `/clear`) in the hook environment, and unchanged across `/clear`. Across resume it was reused in
  two runs (`--resume S2`, `--continue`) and new in two (`--resume S1`, fork).
- Measurement trap: a nested interactive `claude` that inherits `CLAUDE_CODE_CHILD_SESSION` (and
  the other `CLAUDE_CODE_*` variables of its parent) wrote no transcript file, although the hook
  input named a `transcript_path`. The runs above removed every `CLAUDE_CODE_*` variable, `CLAUDE_PID`
  and `CLAUDE_PLUGIN_DATA` from the child environment and kept `HOME`.
- Not measured: Windows (Git Bash and PowerShell `CLAUDE_PID` ancestry, SessionStart behaviour);
  the in-session `/resume` picker; SessionEnd hooks; the content of the `clear` input beyond
  `source`, `session_id` and `transcript_path`.

What this settles for `docs/planning/task-kit-dashboard/working-sessions-plan.md`:

| Hypothesis | Result |
|---|---|
| The session id survives a plain `--resume` and `--continue`, and SessionStart `resume` carries it | Confirmed on macOS: same id, new `CLAUDE_PID`, new process, same transcript file. |
| An `async: true` plugin SessionStart command hook completes in an interactive session | Confirmed on macOS (3 s hook, every source). |
| `/clear` changes the session id, keeps `CLAUDE_PID`, and fires SessionStart with `source: clear` | Confirmed on macOS; the plugin `clear` matcher fired and its input carried the new id and a new `transcript_path`. |
| A plugin SessionStart hook with matcher `resume` fires for `--resume` and `--continue` | Confirmed; both report `source: resume`. |
| `--resume --fork-session` behaves like a resume | Refuted: `source` is `fork`, the id is new, and neither the `resume` nor the `clear` matcher fires. A hook that must see forks needs an entry that matches `fork` (or no matcher). Records of the original session keep the original id. |

## Plugin Cache and Registry Layout

Claude Code stores plugin data under `~/.claude/plugins/`:

| Path | Purpose |
|------|---------|
| `cache/{marketplace}/{plugin}/{version}/` | Cached plugin files (copied from marketplace clone) |
| `marketplaces/{marketplace}/` | Git clone of marketplace repo |
| `installed_plugins.json` | Registry of installed plugins (version, gitCommitSha, installPath, scope) |
| `known_marketplaces.json` | Registry of known marketplaces (source, installLocation, lastUpdated, autoUpdate) |
| `data/{plugin}/` | Per-plugin runtime data (config, logs, venv) |

### installed_plugins.json format (canonical statement)

Several scripts in this repo parse the registry independently (`scripts/dev-tree.py`, `scripts/cache_status.py`, awesome-kit's `generate.py`, the bootstrap engine); this section is the canonical description of its shape. A consumer that guesses the key format wrong fails with a `KeyError` at runtime (that was cache-kit's X1 bug -- it assumed `plugins-kit:cache-kit`).

```json
{
  "version": 2,
  "plugins": {
    "<plugin>@<marketplace>": [
      {
        "version": "0.16.0",
        "installPath": "~/.claude/plugins/cache/<marketplace>/<plugin>/<version>",
        "installedAt": "2026-04-12T14:28:20.656Z",
        "lastUpdated": "2026-06-09T22:24:00.999Z",
        "scope": "user",
        "gitCommitSha": "488d683b3554..."
      }
    ]
  }
}
```

- **Keys are `<plugin>@<marketplace>`** (e.g. `bootstrap@plugins-kit`) -- *not* `<marketplace>:<plugin>` (that colon form is the *display/settings* ref used by `enabledPlugins` and skill names). Split on the first `@`.
- **Each value is a list** of install entries; the first entry is the active install. Code must index `[0]`, not treat the value as a dict.
- `installPath` points into the version-keyed cache dir -- reading a manifest through `installPath` always sees the *published* content for that version, never the dev tree.
