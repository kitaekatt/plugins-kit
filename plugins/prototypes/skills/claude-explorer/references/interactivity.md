# Claude Explorer -- aesthetic and interactivity

The visual language and the action-menu interaction layer. Read when designing UI or implementing the file-queue + drain protocol that lets browser clicks reach the running Claude Code session.

## Aesthetic

Omarchy-style: cohesive dark theme, monospace headings (system monospace stack), sharp/minimal borders, no rounded corners, information-dense without clutter. One coherent color palette (Catppuccin Mocha or similar -- every accent drawn from it). Keyboard-first navigation: `j`/`k` to move, `Enter` to open the focused container, `Esc` to collapse, `/` to search. Every pixel serves information; no decorative chrome.

The visual language is "terminal-respecting browser" -- a reader who likes monospace, tiling, and dark themes should feel at home. Light theme is opt-in via the operator config, not the default.

## Interactivity (planned, not in v1)

Beyond reading, each node carries an **action menu**. Clicks queue an action; the running Claude Code session picks it up on the next user prompt.

The mechanism is constrained by what Claude Code's hook system exposes today. Per the research at `tmp/content-explorer-interactive-research.md`:

- **Hooks cannot inject user-typed slash commands.** A `UserPromptSubmit` hook can only emit `additionalContext` (which Claude sees alongside the prompt) or `block`. There is no "type this as if the user typed it" mechanism.
- **The file-queue + drain pattern is the supported path.** The browser writes a JSON action to `~/.claude/.local-data/prototypes/claude-explorer/queue/*.json`. A `UserPromptSubmit` hook drains the queue, emits an `additionalContext` instruction telling Claude what action to perform, and renames each consumed file to `*.consumed`. Mirrors the bootstrap plugin's pending/displayed handshake (`plugins/bootstrap/hooks/userpromptsubmit/bootstrap-display.sh`) and unreal-kit's `ue-console-cmd.sh:49-51`.
- **The honest UX:** clicks fire on the next user prompt, not in real time. The browser shows queued actions with a status badge (`queued` -> `consumed` -> `acknowledged` once Claude reports back). For invocations the user wants to literally type into a prompt (e.g. populate a slash-command line they then edit), the click copies the command to the clipboard rather than queueing -- no harness round-trip needed.

This UX gating is a real constraint, not a placeholder. Building real-time button-to-execution would require harness changes that do not exist today. The file-queue path is shippable; the real-time path is not.

## Action menu shape

| Node kind | Actions |
|---|---|
| skill | run (copy slash-command to clipboard), audit (queue `/md-domain audit skill <path>`), open path |
| reference doc | open path, copy path |
| script | open path, copy path |
| plugin | update (queue `/plugin update <plugin>`), disable, reload, open path |
| marketplace | update (queue `/plugin marketplace update <name>`), remove, open path |
| CLAUDE.md / json / yaml | open path, copy path |

Actions that copy to clipboard execute immediately in the browser (JS `navigator.clipboard.writeText`). Actions that queue write the JSON action file and update the badge. The action set is configurable per-node-kind in the operator config.

## Queue file format

Browser-written JSON action files at `~/.claude/.local-data/prototypes/claude-explorer/queue/<timestamp>-<nonce>.json`:

```json
{
  "version": 1,
  "kind": "queue",
  "node": {"kind": "plugin", "name": "p4-kit", "path": "..."},
  "action": "update",
  "command": "/plugin update p4-kit",
  "queued_at": "2026-05-15T12:34:56Z"
}
```

The drain hook reads each file, emits an `additionalContext` block telling Claude to perform the action (e.g. "the user queued an update of p4-kit; run /plugin update p4-kit"), renames the file to `*.consumed`, and lets Claude run the command on this turn.
