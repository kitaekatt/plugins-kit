# claude-ui-kit

Opinionated defaults and customization skills for Claude Code's UI surfaces. Currently ships the **statusline** (the bar at the bottom of the prompt) with threshold-aware default colors and a `/statusline` skill for customizing it. Future home for other UI tweaks (notifications, output formatting, etc.) as the surface area grows.

## Status line

When the plugin is installed (and no other `statusLine` is already configured), it writes a `statusLine` block into the project's `.claude/settings.local.json` (per-user, gitignored/p4ignored — safe in source-controlled projects) pointing at the bundled script. The default shows:

```
📁 dirname  │  ▇ Fable  │  🧠 96%  │  🔋 88%  │  📅 62%
```

All percentages are **capacity remaining** — higher is better, lower triggers warning colors.

- **▇ model + effort** — model display name (version stripped: "Fable 5" → "Fable"), prefixed with a meter glyph for the session's reasoning effort: `▁` low, `▃` medium, `▅` high, `▇` xhigh, `█` max. The glyph is omitted for models without the effort parameter. Hide the whole segment with `STATUSLINE_SHOW_MODEL=0` (env var, see below).
- **🧠 context remaining** — turns orange at 70%, red at 30%
- **🔋 5-hour budget remaining** — turns orange at 30%, red at 10%
- **📅 7-day budget remaining** — gray (no thresholds)

If a `<cwd>/.local-data/claude-ui-kit/systemmessage.<keyword>.txt` file exists, the most recently modified one is appended to the line as `💬 <message>` (capped at 20 chars). Plugins write these to surface short alerts; deleting the file clears the alert.

Override thresholds via env vars in `settings.json` (values are in "% remaining" — colors trigger at-or-below):

```json
{
  "env": {
    "STATUSLINE_CTX_ORANGE_AT": "60",
    "STATUSLINE_CTX_RED_AT": "20"
  }
}
```

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

- **Skips entirely** if you already have any `statusLine` configured in `~/.claude/settings.json`, the project's `.claude/settings.json`, or the project's `.claude/settings.local.json` — UNLESS that statusLine points at this plugin (then it refreshes the path on upgrade).
- **Surfaces a fix-all prompt** if a non-plugin statusLine is detected, so you can type `replace my status line` to switch.
- **Stays quiet permanently** if the user customizes via `/statusline` (a marker file in the plugin data dir disables further automatic management).

## /statusline skill

Run `/statusline` in any session. The skill reads your active statusline script, summarizes what it displays, and asks if you want to change anything. It only acts on what you ask for — it won't pitch themes, gradients, or other concepts unless you bring them up.

Example interactions:

- *"Change the context % to a yellow progress bar"* — done.
- *"Drop the 7-day number"* — done.
- *"Reset to default"* — restores the plugin default and clears any customization.

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
