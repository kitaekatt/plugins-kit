# Security

Claude Code plugins run trusted code on your machine: their skills, hooks, and
scripts execute with your user privileges. This document inventories exactly
what the plugins in this marketplace read, write, download, and execute, so you
can decide whether to trust them. The heaviest actor is the **bootstrap**
plugin, which provisions dependencies for every other plugin.

## What bootstrap writes

- **`~/.claude/plugins/data/<marketplace>/<plugin>/`** -- per-plugin state:
  Python virtualenvs (`.venv/`), bootstrap logs (`bootstrap.log`), config
  files, run-throttle stamps, and cooldown markers. This is the only tree
  bootstrap writes plugin data into.
- **`~/.local/bin/`** -- bootstrap-managed tool binaries (e.g. `jq`, `gh`) and
  the `bootstrap-reset-cooldown` helper.
- **`~/.local/share/python-standalone/`** -- a standalone CPython runtime, used
  when no suitable Python is already installed.

## What bootstrap changes on PATH (and why)

Bootstrap-installed tools must be discoverable from any shell, not just the one
that ran the session. To achieve that persistently:

- **Windows:** appends `~/.local/bin` and the standalone-Python directory to the
  **User** `Path` in the registry, via PowerShell `SetEnvironmentVariable`
  (User scope, never Machine/system). No admin rights, no system-wide change.
- **macOS / Linux:** adds the same entries to your shell rc files
  (`~/.bashrc`, and `~/.zshrc` on macOS) using managed, clearly-marked blocks.

## What bootstrap downloads

All downloads are over HTTPS from official upstream release pages:

- **`jq`** -- from the jqlang GitHub releases, pinned version, **sha256-verified**
  against a hash pinned in the manifest.
- **`gh`** (GitHub CLI, used by git-kit) -- from the `cli/cli` GitHub releases,
  pinned version, **sha256-verified** against a pinned hash.
- **Standalone CPython** -- from the indygreg `python-build-standalone` GitHub
  releases, pinned version, **sha256-verified** against a hash pinned per
  platform. A checksum mismatch discards the download and aborts the install; it
  is never extracted.
- **`uv`** -- installed by piping Astral's official installer
  (`https://astral.sh/uv/install.sh`) to a shell. Integrity is handled by that
  installer; bootstrap does not add its own hash check for uv.

## Network use by other plugins

- **llm-scripting-kit** -- only when you configure an API key: for an
  OpenRouter endpoint it validates the key against `GET /auth/key` at
  `openrouter.ai` (other endpoints use a `GET /models` probe, or skip when
  `account_check: none`) and caches a hash of the validation result locally. No
  key configured means no network call.

Other plugins (git-kit, p4-kit, etc.) invoke tools you already use (`git`,
`gh`, `p4`) against endpoints you have configured; they add no telemetry of
their own.

## What it does NOT do

- **No telemetry.** Nothing reports usage, timing, or contents anywhere.
- **No exfiltration.** No project data, source, or credentials leave your
  machine. Network use is limited to the release downloads and the optional
  OpenRouter key validation above.
- **No writes outside the listed locations.** Bootstrap does not modify system
  directories, other users' files, or arbitrary project paths; plugin state is
  confined to `~/.claude/plugins/data/`, `~/.local/bin`, and
  `~/.local/share/python-standalone`.
- **No elevation without asking.** It does not silently run as root/admin; when
  a step genuinely needs your action, it asks.

## Reporting a vulnerability

Please open an issue at
<https://github.com/kitaekatt/plugins-kit/issues>. Include the plugin, version,
and steps to reproduce. For a sensitive report, note that in the issue and we
will arrange a private channel.
