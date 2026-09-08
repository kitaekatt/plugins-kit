# claude-ui-kit

Your status line shows how much context and rate-limit headroom you have
left, in color, before you hit a wall -- plus a skill to customize it.
Ships the **statusline** (the bar at the bottom of the prompt) with
threshold-aware default colors and a `/statusline` skill for customizing it.
The home for UI tweaks (notifications, output formatting, etc.) in this
marketplace.

## Install

```
/plugin marketplace add kitaekatt/plugins-kit
/plugin install claude-ui-kit
```

## Status line

When the plugin is installed (and no other `statusLine` is already configured), it writes a `statusLine` block into the user-global, TRACKED `~/.claude/settings.json` (shared across every machine that syncs it, not the project or a `.local.json` file) pointing at the bundled script. The command is written machine-independently for exactly this reason -- see `scripts/install_statusline.py` for why. The default shows:

```
📁 dirname  │  ▇ Fable  │  🧠 96%  │  🔋 88%  │  📅 62%
```

All percentages are **capacity remaining** -- higher is better, lower triggers warning colors.

- **▇ model + effort** -- model display name (version stripped: "Fable 5" -> "Fable"), prefixed with a meter glyph for the session's reasoning effort: `▁` low, `▃` medium, `▅` high, `▇` xhigh, `█` max. The glyph is omitted for models without the effort parameter. Hide the whole segment with `STATUSLINE_SHOW_MODEL=0` (env var, see below).
- **🧠 context remaining** -- turns orange at or below 70%, red at or below 30%
- **🔋 5-hour budget remaining** -- turns orange at or below 30%, red at or below 10%
- **📅 7-day budget remaining** -- turns orange at or below 30%, red at or below 10%

If a `<cwd>/.local-data/claude-ui-kit/systemmessage.<keyword>.txt` file exists, the most recently modified one is appended to the line as `💬 <message>` (capped at 20 chars). Plugins write these to surface short alerts; deleting the file clears the alert.

Override thresholds via env vars in `settings.json` (values are in "% remaining" -- colors trigger at-or-below):

```json
{
  "env": {
    "STATUSLINE_CTX_ORANGE_AT": "60",
    "STATUSLINE_CTX_RED_AT": "20"
  }
}
```

The full env-var list: `STATUSLINE_CTX_ORANGE_AT` (default `70`), `STATUSLINE_CTX_RED_AT`
(default `30`), `STATUSLINE_SESS_ORANGE_AT` (default `30`), `STATUSLINE_SESS_RED_AT`
(default `10`), `STATUSLINE_WEEK_ORANGE_AT` (default `30`), `STATUSLINE_WEEK_RED_AT`
(default `10`).

The model + effort segment is on by default; disable it the same way:

```json
{
  "env": {
    "STATUSLINE_SHOW_MODEL": "0"
  }
}
```

## On/off

The plugin's installation is the on/off switch. To use it, add it to your project or user `bootstrap.json`:

```json
{
  "plugins": [
    {"ref": "plugins-kit:claude-ui-kit", "enabled": true}
  ]
}
```

To opt out, set `"enabled": false` (or just don't list it).

## Conflict avoidance

The bootstrap install script:

- **Surfaces a fix-all failure** (not a silent skip) if you already have any foreign `statusLine` configured in `~/.claude/settings.json`, the project's `.claude/settings.json`, or the project's `.claude/settings.local.json` -- UNLESS that statusLine points at this plugin (then it refreshes the path on upgrade). Answer the prompt to keep your existing statusLine or switch to claude-ui-kit's; the answer is remembered so you are not asked again, and you can always type `replace my status line` later to switch.
- **Stays quiet permanently** if the user customizes via `/statusline` (a marker file in the plugin data dir disables further automatic management).

## Segment API (contributing a cell from another plugin)

Other plugins add a cell to the bar WITHOUT touching `statusLine.command`:
drop one entry into `segments/` (sibling of `scripts/` in this plugin's data
dir, `~/.claude/plugins/data/<marketplace>/claude-ui-kit/segments/`). ui-kit
owns composition -- the separator and ordering (lexical by filename; use
`NN-` prefixes) -- contributors own content. Two entry kinds:

- `*.txt` -- first line rendered while fresh (mtime within
  `STATUSLINE_SEGMENT_TXT_TTL` seconds, default 300), capped at 60 chars.
- `*.sh` -- executed with the statusline stdin JSON on stdin under a hard
  per-segment timeout (`STATUSLINE_SEGMENT_TIMEOUT`, default 2s, via `timeout`
  or `gtimeout`). Emit your own ANSI, one line, no leading separator. Stdout
  is NOT appended verbatim: it is normalized first line only, capped at 120
  characters, with CR stripped and a RESET appended, so a segment that
  ignores the single-line contract degrades itself, not the bar. Empty
  output, non-zero exit, timeout, or no `timeout(1)` binary on PATH all
  render as an absent cell -- the last case disables every `*.sh` segment and
  the bar shows one `[segments off: no timeout(1)]` marker instead of
  rendering nothing with no explanation.

Contract for `*.sh` entries: **pure cache reader**. Read pre-computed local
state; never fetch, poll, or block on the network. Collect data in your own
out-of-band process (a daemon, a hook, a cron) and write it somewhere cheap
to read. Uninstalling a contributor should remove its segment file; a
segment whose backing plugin is gone should exit 0 silently, which renders
as no cell.

## /statusline skill

Run `/statusline` in any session. The skill reads your active statusline script, summarizes what it displays, and asks if you want to change anything. It only acts on what you ask for -- it won't pitch themes, gradients, or other concepts unless you bring them up.

Example interactions:

- *"Change the context % to a yellow progress bar"* -- done.
- *"Drop the 7-day number"* -- done.
- *"Reset to default"* -- restores the plugin default and clears any customization.

When you customize, the skill copies the script to `~/.claude/statusline.sh` (or `<project>/.claude/statusline.sh` for a project-scoped version) and points settings.json there, so bootstrap won't overwrite your edits.

## Layout

```
claude-ui-kit/
  .claude-plugin/
    plugin.json
  bootstrap.json              # sync_to_data + script entry-point
  scripts/
    statusline.sh             # the default status line script (synced to data dir)
    install_statusline.py     # bootstrap script: writes settings.json conditionally
  skills/
    statusline/
      SKILL.md                # /statusline skill definition
      references/
        components.md         # what data the script can read
        styling.md            # ANSI palettes, progress bars, gradients, themes
```
