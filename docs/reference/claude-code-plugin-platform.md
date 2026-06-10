# Claude Code plugin platform reference

Static Claude Code platform facts that plugin development in this repo relies on but rarely needs inline: the hook-JSON output contract and the plugin cache/registry layout on disk. These are CC platform behavior, not plugins-kit-specific — the root `CLAUDE.md` links here instead of carrying the tables. When in doubt, the upstream docs are the source of truth.

## Hook JSON Format

**Official docs**: https://code.claude.com/docs/en/hooks (canonical reference). When in doubt, fetch this URL — it is the source of truth.

On exit 0, stdout is parsed as JSON. Exit 2 = blocking error (stderr fed to Claude). Other exits = non-blocking error. JSON is only processed on exit 0.

**Universal fields** (all events):

| Field | Default | Description |
|-------|---------|-------------|
| `continue` | `true` | If `false`, Claude stops entirely. Takes precedence over other decisions |
| `stopReason` | none | Message shown to user when `continue` is `false`. Not shown to Claude |
| `suppressOutput` | `false` | If `true`, hides stdout from verbose mode |
| `systemMessage` | none | Shown to user only — Claude never sees it |

**Event-specific decision control**:

| Event | Decision pattern | To Claude |
|-------|-----------------|-----------|
| SessionStart | None | `hookSpecificOutput.additionalContext` or plain text stdout |
| UserPromptSubmit | `decision: "block"` + `reason` | `hookSpecificOutput.additionalContext` or plain text stdout |
| PreToolUse | `hookSpecificOutput.permissionDecision` (allow/deny/ask) | `hookSpecificOutput.additionalContext` |
| PostToolUse | `decision: "block"` + `reason` | `hookSpecificOutput.additionalContext` |
| PostToolUseFailure | None | `hookSpecificOutput.additionalContext` |
| Stop / SubagentStop | `decision: "block"` + `reason` | `reason` only (no `hookSpecificOutput`) |
| SubagentStart | None | `hookSpecificOutput.additionalContext` |
| Notification | None | `hookSpecificOutput.additionalContext` |
| PermissionRequest | `hookSpecificOutput.decision.behavior` (allow/deny) | — |
| ConfigChange | `decision: "block"` + `reason` | — |

`hookSpecificOutput` always requires `hookEventName` set to the event name.

> **plugins-kit note — bootstrap background mode.** The bootstrap engine writes output to a pending file, which the UserPromptSubmit hook reads and re-emits as its own stdout. Stop hooks do not support `hookSpecificOutput`, so UserPromptSubmit is used to inject `additionalContext` for Claude. (This wrinkle is also summarized inline in the root `CLAUDE.md`.)

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

Several scripts in this repo parse the registry independently (`scripts/dev-tree.py`, `scripts/cache_status.py`, awesome-kit's `generate.py`, the bootstrap engine); this section is the canonical description of its shape. A consumer that guesses the key format wrong fails with a `KeyError` at runtime (that was cache-kit's X1 bug — it assumed `plugins-kit:cache-kit`).

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

- **Keys are `<plugin>@<marketplace>`** (e.g. `bootstrap@plugins-kit`) — *not* `<marketplace>:<plugin>` (that colon form is the *display/settings* ref used by `enabledPlugins` and skill names). Split on the first `@`.
- **Each value is a list** of install entries; the first entry is the active install. Code must index `[0]`, not treat the value as a dict.
- `installPath` points into the version-keyed cache dir — reading a manifest through `installPath` always sees the *published* content for that version, never the dev tree.
