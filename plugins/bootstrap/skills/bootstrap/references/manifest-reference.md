# Bootstrap Manifest Reference (`bootstrap.json`)

A declarative configuration file covering automatable operations. The engine reads the manifest and calls library primitives directly — no plugin code needed.

## Schema

```json
{
  "tools": [
    {"name": "git"},
    {"name": "uv", "install": {"macos": "curl -LsSf https://astral.sh/uv/install.sh | sh", "ubuntu": "curl -LsSf https://astral.sh/uv/install.sh | sh", "windows": "powershell -c \"irm https://astral.sh/uv/install.ps1 | iex\""}},
    {"name": "node", "installPath": "~/.local/share/node", "install": {"macos": "brew install node"}}
  ],
  "fonts": [
    {
      "name": "JetBrainsMono Nerd Font",
      "match": "*JetBrainsMono*NerdFont*",
      "download": {
        "url": "https://github.com/ryanoasis/nerd-fonts/releases/download/v3.4.0/JetBrainsMono.tar.xz",
        "sha256": "ef552a3e638f25125c6ad4c51176a6adcdce295ab1d2ffacf0db060caf8c1582",
        "archive_type": "tar.xz"
      }
    }
  ],
  "path_entries": ["~/.local/bin"],
  "venv": {
    "check_imports": ["yaml", "upyrc"]
  },
  "git_deps": [
    {
      "url": "https://github.com/octocat/Hello-World",
      "branch": "master",
      "sparse_paths": ["README"],
      "commit": "abc1234567890abcdef1234567890abcdef123456"
    }
  ],
  "sync_to_data": [
    {"src": "lib", "dst": "lib"}
  ],
  "shared_libs": [
    {"name": "openrouter_kit", "src": "lib"}
  ],
  "shared_lib_imports": ["openrouter_kit"],
  "json_entries": [
    {
      "reference": "known_marketplaces.json",
      "target": "~/.claude/plugins/known_marketplaces.json",
      "merge_fields": ["source", "autoUpdate"],
      "preserve_fields": ["lastUpdated", "installLocation"]
    }
  ],
  "ini_settings": [
    {
      "file": "${uproject_dir}/Config/UserEngine.ini",
      "section": "/Script/PythonScriptPlugin.PythonScriptPluginSettings",
      "settings": {"bRemoteExecution": "True", "bIsDeveloperMode": "True"}
    }
  ],
  "pypi_packages": [
    {
      "package": "unreal-stub",
      "extract_to": "${plugin_root}/skills/ue-python-api/stubs/unreal.py",
      "extract_pattern": "*.py"
    }
  ],
  "marketplaces": [
    {"name": "plugins-kit", "source": "https://github.com/user/plugins-kit.git", "alwaysUpdate": true},
    {"name": "team-plugins", "pin": "f7f6276a"}
  ],
  "plugins": [
    {"ref": "plugins-kit:unreal-kit", "enabled": true},
    {"ref": "plugins-kit:bootstrap", "min_version": "0.9.1"}
  ],
  "project_venv": {
    "extras": ["dev"],
    "check_imports": ["pytest"]
  },
  "project_config": {
    "file": ".local-data/p4-kit/config.yaml",
    "legacy_file": ".claude/p4-kit.yaml",
    "required_fields": {
      "P4PORT": {"user_msg": "Perforce server address", "agent_msg": "Ask the user for P4PORT and write it to {config_path}"},
      "P4USER": {"user_msg": "Perforce username", "agent_msg": "Ask the user for P4USER and write it to {config_path}"},
      "DEFAULT_AGENT": {"user_msg": "Default review agent", "agent_msg": "Ask the user for DEFAULT_AGENT", "default": "claude-opus"}
    },
    "autodetect": "custom_bootstrap.py autodetect"
  },
  "config": {
    "file": "config.yaml",
    "defaults_source": "defaults/config.yaml",
    "required_fields": {
      "uproject_path": {"user_msg": "Set path to your .uproject file", "agent_msg": "Ask user for .uproject path and write it to config.yaml as uproject_path"}
    },
    "autodetect": "scripts/autodetect.py detect"
  },
  "script": {
    "path": "scripts/bootstrap.py",
    "entry_point": "bootstrap"
  }
}
```

Every field is optional — include only what the plugin needs.

Three schema gotchas worth calling out:

- **`install` keys are exactly the `detect_os()` values** — `macos`, `windows`, `ubuntu`. The lookup is exact: a `darwin` or `linux` key silently never matches.
- **`autodetect` is a string**, `"<script_path> <function_name>"` (e.g. `"scripts/autodetect.py detect"`), for both `config` and `project_config`. A dict form is not understood.
- **`python_stub_check` is not a manifest field** — it lives only under `self_setup` in bootstrap's own `config.json` (see its section below).

## `venv` — Per-Plugin Python Environment

A plugin declares a `venv` section to request a bootstrap-managed Python environment. The engine creates and syncs `<plugin_data_dir>/.venv` from the plugin's `pyproject.toml` (via `uv sync --project <plugin_root>`), then verifies each listed import works.

```json
{
  "venv": {
    "check_imports": ["yaml", "upyrc"]
  }
}
```

### `<PLUGIN>_VENV` environment variable export

When `CLAUDE_ENV_FILE` is set (always true under SessionStart hooks), a successful venv check also appends an export line of the form:

```sh
export <PLUGIN_NAME_UPPER>_VENV=<absolute path to venv python>
```

`<PLUGIN_NAME_UPPER>` is the plugin manifest name uppercased with hyphens replaced by underscores. Examples:

```sh
export UNREAL_KIT_VENV=/Users/christina/.claude/plugins/data/plugins-kit/unreal-kit/.venv/bin/python
export BOOTSTRAP_VENV=/Users/christina/.claude/plugins/data/plugins-kit/bootstrap/.venv/bin/python
```

**Consumer pattern** — scripts re-exec themselves under the plugin's venv without reconstructing bootstrap's data-dir layout:

```python
import os, sys
from pathlib import Path

_venv = os.environ.get("UNREAL_KIT_VENV")
if not _venv:
    sys.stderr.write("ERROR: UNREAL_KIT_VENV not set. Is bootstrap running?\n")
    sys.exit(1)
if Path(sys.executable).resolve() != Path(_venv).resolve():
    os.execv(_venv, [_venv] + sys.argv)
```

**Reach**: Exports in `CLAUDE_ENV_FILE` are sourced by Claude Code before every subsequent Bash tool invocation. They do NOT automatically propagate to hook script invocations — hook scripts that need the venv must either re-derive the path or source `$CLAUDE_ENV_FILE` themselves. For the common case (scripts called via Bash or re-exec'd via `os.execv`), the variable is always set.

**Fail-fast semantics**: if bootstrap cannot create the venv, no export line is written. Consumer scripts then error out on the unset var rather than re-exec'ing an invalid interpreter path.

## `shared_libs` / `shared_lib_imports` — Cross-Plugin First-Party Libraries

These two keys let one plugin reuse another plugin's first-party Python library **without declaring a dependency on the owning plugin** (reuse-by-availability). The engine shares the library SOURCE via a `.pth` file; it does NOT install third-party dependencies — each importing plugin declares those itself in its own `pyproject.toml` (a static test, `tests/bootstrap/test_dependency_completeness.py`, catches omissions).

**Owner side — `shared_libs`** (the plugin that owns the library):

```json
{
  "shared_libs": [
    {"name": "openrouter_kit", "src": "lib"}
  ]
}
```

- `name` — the importable top-level package name. Identity key for layered merge.
- `src` — directory (relative to the plugin root) that contains the package; the package itself lives at `<plugin_root>/<src>/<name>/`. Use `"."` when the package sits directly under the plugin root (e.g. `bootstrap_lib`).

For each entry the engine: syncs the package source to a **stable, version-independent** location, `~/.claude/plugins/data/plugins-kit/_shared_libs/<name>/<name>/` (a clean re-sync that prunes deleted/renamed modules, content-hash cached); then writes a `<name>.pth` (pointing at `_shared_libs/<name>/`) into the **standalone Python's** site-packages and verifies `import <name>`.

**Consumer side — `shared_lib_imports`** (a plugin that wants the library on its own venv):

```json
{
  "shared_lib_imports": ["openrouter_kit"]
}
```

A plain string list of library names (deduplicated-unioned across config layers). For each name the engine writes a `<name>.pth` into THIS plugin's own venv (`<plugin_data_dir>/.venv`) pointing at the shared location, then verifies the import. The consumer names only the LIBRARY, never the owning plugin — the location is derived from the name, so reuse stays decoupled from the owner.

**Stable location, not versioned**: the `.pth` points at the version-independent `_shared_libs/<name>/`, so an owner version bump re-syncs one directory and every `.pth` (standalone + all consumer venvs) keeps resolving without a rewrite.

**Ordering / eventual consistency**: the consumer link runs AFTER the `venv` handler (so the venv exists as the `.pth` target) but a consumer may be processed before its owner in a given session. A not-yet-published library is a soft skip (logged, not a failure) that self-heals on the next session; the runtime `bootstrap_guard` covers the installed-but-not-yet-provisioned window.

**Source only**: a `.pth` shares first-party SOURCE, not third-party deps. If `openrouter_kit` needs `openai`, the plugin that imports it (under the interpreter that runs the importing script) must declare `openai` in its own `pyproject.toml` + `venv.check_imports`.

## `fonts` — Per-User Font Installation

A plugin declares a `fonts` array to ensure a font (e.g. a Nerd Font for
statusline glyphs) is installed. Installation is **unprivileged on every
platform** — no UAC, no `sudo` — so it runs silently inside the
non-interactive SessionStart hook:

| OS | Install location | Registration |
|----|------------------|--------------|
| Windows | `%LOCALAPPDATA%\Microsoft\Windows\Fonts` | HKCU `…\CurrentVersion\Fonts` + `AddFontResourceW` |
| macOS | `~/Library/Fonts` | none needed |
| Linux | `~/.local/share/fonts` | `fc-cache -f` |

```json
{
  "fonts": [
    {
      "name": "JetBrainsMono Nerd Font",
      "match": "*JetBrainsMono*NerdFont*",
      "download": {
        "url": "https://github.com/ryanoasis/nerd-fonts/releases/download/v3.4.0/JetBrainsMono.tar.xz",
        "sha256": "ef552a3e638f25125c6ad4c51176a6adcdce295ab1d2ffacf0db060caf8c1582",
        "archive_type": "tar.xz"
      }
    }
  ]
}
```

| Field | Required? | Description |
|-------|-----------|-------------|
| `name` | Yes | Display name; also the merge identity key |
| `match` | No (defaults to `name`) | Case-insensitive glob matched against installed font **filenames** (scanned across per-user and system font dirs). When a match exists, the font is considered installed and nothing is downloaded |
| `download.url` | Yes (to install) | Archive of font faces (`.zip` / `.tar.gz` / `.tar.xz`). All `.ttf`/`.otf` members are extracted and flattened to basename |
| `download.sha256` | Yes (to install) | Verified before extraction; a mismatch installs nothing |
| `download.archive_type` | No | Autodetected from the URL extension when omitted |

**Behavior**: detect → (present) log an ok entry / (absent) download, verify,
extract every face into the per-user font dir, register, re-check. Fonts are
OS-agnostic, so `download` is normally a flat `{url, sha256}`; a per-OS nesting
(`{"windows": {...}, "macos": {...}}`) is still honored for the rare case it's
needed. A missing font is **cosmetic** (glyphs fall back to ASCII/emoji), so a
failed download logs an action line and retries next session rather than
surfacing a blocking fix-all item. After install, restart the terminal so it
picks up the new font.

## Variable Expansion

Variable references are expanded by the engine from plugin context and config:

| Variable | Source |
|----------|--------|
| `${plugin_root}` | Plugin's install path |
| `${data_dir}` | Plugin's data directory |
| `${uproject_dir}` | From plugin config (if applicable) |

## `install` — Per-OS Install Methods

The `install` block answers "how do I install this tool on OS X" per OS. Its
keys are exactly the `detect_os()` values — `macos`, `ubuntu`, `windows` (an
exact-match lookup; a `darwin`/`linux` key silently never fires). Each per-OS
**value** is one of two shapes:

- an **opaque command string** — a shell command run verbatim, or the `manual`
  sentinel;
- a **structured manager object** — one of `scoop` / `brew` / `apt` / `command`.

Both shapes are normalized to a canonical object at parse time
(`_normalize_tool_entry`, in memory — never rewritten to disk), so a manifest
may mix strings and objects freely and every legacy spelling keeps parsing.

```json
{
  "name": "kitty",
  "install": {
    "macos":   {"brew": {"cask": "kitty"}, "check": "test -d /Applications/kitty.app"},
    "ubuntu":  {"apt": "kitty"},
    "windows": {"scoop": "extras/kitty"}
  }
}
```

### The object forms

| Form | OS | Meaning |
|------|----|---------|
| `{"scoop": "bucket/pkg"}` | windows | Install via Scoop (userspace, no admin). `bucket/pkg` is the existing Scoop grammar. |
| `{"brew": "formula"}` | macos | `brew install <formula>` (string shorthand = formula name). |
| `{"brew": {"cask": "name"}}` | macos | `brew install --cask <name>`. |
| `{"brew": {"formula": "name", "tap": "user/repo"}}` | macos | `brew install <tap/>name`; `formula` XOR `cask`, `tap` optional. |
| `{"apt": "pkg"}` | ubuntu | `apt-get install -y <pkg>` (string package name only; elevation implied). |
| `{"command": "…", "elevated": true\|false}` | any | Opaque shell command; `elevated: true` routes it through the elevation queue when privileges are missing. |
| `"…"` (bare string) | any | Exactly equivalent to `{"command": "…", "elevated": false}`. |
| `"manual"` (sentinel) | any | No unattended installer: bootstrap **verifies** the tool resolves on PATH but never tries to install it. Not a fix-all item. |

**Legacy `download.<os-arch>.scoop` is deprecated but still read.** The
normalizer promotes the host-resolved `download.…​.scoop` entry to
`install.<os>.scoop` in memory (and strips it from the `download` block), so old
manifests behave identically. New manifests should declare `install.<os>.scoop`
directly. Scoop takes precedence over any command spelled at the same
`install.<os>` key (this matches the dispatch order below), so an entry that
declares both `install.windows: "manual"` and a scoop package installs via
Scoop.

### Runtime precedence

For a tool that is not already resolvable, the engine dispatches an ordered
strategy table (`_INSTALL_STRATEGIES`), taking the **first** that applies:

1. **resolve** — `installPath` candidates → `check` command → `which` (see Tool
   resolution below). A resolved tool records its path, is linked onto PATH, and
   nothing is installed.
2. **scoop** (`install.<os>.scoop`) — Windows only.
3. **brew** (`install.macos.brew`) — macOS only.
4. **apt** (`install.ubuntu.apt`) — Ubuntu only.
5. **url download** (`download.url` + `download.sha256`) — our own copy under
   `~/.local`. A failed download **falls through** to step 6.
6. **install command** (`install.<os>.command`, or the `manual` sentinel).

The three managers (scoop/brew/apt) are **mutually exclusive by OS** — each
strategy reads only its own host's install value — and all run **before** url
download, matching the per-OS ladder "manager > download > command". After any
install attempt the tool is re-checked regardless of the installer's exit code.

### Manager availability

- **scoop** is provisioned **lazily** — the first scoop-backed entry installs
  Scoop (no admin, no UAC). A scoop failure surfaces a per-item `scoop_failed`.
- **brew** is **never auto-installed** (`ensure_brew` is detect-only). When brew
  is missing while a brew-backed entry is pending, the entry surfaces a
  `brew_failed` item AND signals the elevation queue to lead the macOS
  remediation script with the official Homebrew installer (one user-run step;
  brew entries then install unattended on the next pass).
- **apt** always needs root — see elevation below.

**Declare a `check` command on cask entries.** A GUI cask (e.g.
`google-chrome`, `kitty`) usually has no CLI binary on PATH, so the resolve step
can't detect it and the re-check after `brew install --cask` also can't. For a
cask the engine therefore **trusts brew's success without a passing re-check**
(cask-only leniency; a *formula* whose re-check fails is still a `brew_failed`).
The consequence: a cask **without** a `check` re-runs `brew install --cask …`
on **every** post-cooldown pass (a slow no-op). Add a `check` (e.g.
`"test -d /Applications/kitty.app"`) so the resolve step short-circuits once the
app is present.

### Elevation behavior

Bootstrap runs as a **non-interactive SessionStart hook**, so it must never
prompt for a sudo password or trigger a UAC dialog. Before running any operation
that needs privileges it probes: `sudo -n true` (root/passwordless-sudo) on
Ubuntu/macOS, an admin-token check on Windows.

- **Privileges available** → the operation runs **directly** (unchanged
  behavior). On Christina's Ubuntu machines env-config's sudoers rules make
  `sudo -n` pass, so apt entries install silently in the hook.
- **Privileges missing** → the strategy **defers** instead of attempting. It
  records a persistent per-item `needs_elevation` failure carrying a structured
  `elevation` descriptor (`{method: apt|command|brew_installer, …}`).

At the end of the pass the engine harvests every `elevation` descriptor for the
current OS into one queue and writes **ONE** remediation script:

- **Location**: `<data_dir>/elevate/install-elevated.{sh|bat}`.
- **Regenerated every pass** from the current queue, and **deleted** when the
  queue is empty — the script disappears once the deferred ops succeed.
- **Ubuntu**: bash, `set -euo pipefail`, a leading `apt-get update` then
  `apt-get install -y <all queued packages>`, then each deferred elevated
  `command`. Run with `sudo bash <path>`.
- **macOS**: bash; installs Homebrew first (official installer) when brew was the
  missing prerequisite, then any deferred commands. Run with `bash <path>` as
  the admin user.
- **Windows**: a self-elevating `.bat` (UAC relaunch via
  `Start-Process -Verb RunAs`, `fsutil` admin detect, success-only self-delete),
  written CRLF, containing the deferred commands. Deferred commands are labeled
  as comments (zero execution surface in the label itself).

One aggregate `elevation_script` fix-all item names the script path and what it
will do; the per-item `needs_elevation` failures keep persisting on their own.
Both clear via the normal re-check on the next session (or `fix-all`) once the
user has run the script.

### Non-Ubuntu Linux fails fast

`detect_os()` confirms Ubuntu via `/etc/os-release` (any `ID`/`ID_LIKE`
mentioning `ubuntu`, which keeps Ubuntu-on-WSL and apt-based derivatives
working). A genuinely different Linux distribution — or a host with no readable
`/etc/os-release` — raises `UnsupportedPlatformError` with a descriptive message
rather than silently receiving Ubuntu/apt install commands that would be wrong
for it. This is a deliberate, user-ratified behavior change.

## Tool resolution: `installPath`, `check`, and PATH linkage

A tool entry is resolved in this order: **`installPath` candidates** (file
exists) → **`check` command** (exit 0) → **`shutil.which(name)`** (on PATH).
First hit wins. A tool that resolves on disk but whose directory is not on PATH
is **auto-linked onto PATH** by the engine (see "Tool → PATH linkage" below) —
"installed but not reachable by name" is treated as actionable, not done.

### `installPath` — one dir or a list

Tells the engine where the binary lives (or will live after install). Solves the
chicken-and-egg case where a tool is installed to a known directory not yet on
PATH at check time. Accepts a single string **or a list of candidate dirs**
(tried in order — useful when an installer may land in more than one place):

```json
{"name": "node", "installPath": "~/.local/share/node", "install": {"windows": "..."}}
{"name": "draw.io", "installPath": ["/c/Program Files/draw.io", "$LOCALAPPDATA/Programs/draw.io"]}
```

- Supports `~` and `$VAR` / `${VAR}` expansion.
- The engine checks `<dir>/<name>` (and `<dir>/<name>.exe` on Windows) for each
  candidate before falling back to the `check` command, then `shutil.which()`.
- The same `installPath` is used for the recheck after install.

### `check` — a presence command

Optional shell command whose **exit code 0 means "present."** Use it when a
tool's presence can't be expressed as name-on-PATH or a fixed install dir (app
bundles, a `--version` smoke test, multiple acceptable locations). Runs via the
same bash-on-Windows shim as `install`, so Unix syntax works regardless of the
launching shell.

```json
{"name": "draw.io",
 "check": "command -v draw.io || test -f \"/c/Program Files/draw.io/draw.io.exe\"",
 "install": {"windows": "winget install --id JGraph.Draw"}}
```

A `check`-resolved tool yields no concrete binary path, so it is not recorded in
`tool_paths.json` and gets no PATH auto-link (the engine has no directory to add)
— prefer `installPath` when you know the directory, since that path *is*
recorded and linked.

### Tool → PATH linkage

When a tool resolves via `installPath` (or `which` from a dir off the persistent
PATH) but its directory isn't on PATH, the engine adds that directory to PATH
itself — shell RC files + Windows User PATH (registry) + the live process PATH —
and logs `tool: on disk but not on PATH — added <dir>`. This is the linkage
between `tools[]` and `path_entries[]`: a resolved tool pulls its own dir onto
PATH, so you don't have to declare a separate `path_entries` entry, and a tool
that's present-but-unreachable becomes reachable without any "restart your shell"
instruction (per dependency-philosophy.md principle 4).

### Install exit codes are advisory

After running a tool's `install` command, the engine **re-checks regardless of
the installer's exit code.** Some installers exit non-zero for "already
installed / no upgrade available" (winget exit 43); the re-check, not the exit
code, decides whether the tool is present. Only when the re-check still fails is
a failure recorded (`install_failed` if the installer also errored,
`installed_but_path_stale` if it reported success but the binary is still
unfindable).

## `self_setup.python_stub_check`

A Windows-only check that detects Microsoft Store Python stubs (or any other shadowing `python.exe`) sitting in front of the bootstrap-installed standalone Python on PATH. When a stub is detected, bootstrap writes a self-elevating `fix_python_path.bat` script to the user's Desktop and surfaces a friendly fix-all message instructing the user to run it as administrator. The check is a no-op on non-Windows and on Windows machines whose first `python.exe` is already the standalone one.

This field lives only under `self_setup` (in `defaults/config.json` for the bootstrap plugin) — it is not a per-plugin manifest entry.

| Key | Default | Description |
|-----|---------|-------------|
| `good_python_dir` | `~/.local/share/python-standalone/python` | Directory that should win on PATH. The check passes when the first `python.exe` resolved by `shutil.which` lives here. |
| `stub_markers` | `["WindowsApps"]` | Substrings that identify a shadowing stub. If the first `python.exe` on PATH contains any of these (case-insensitive), the check fails and a remediation script is written. |
| `script_output_dir` | `~/Desktop` | Where to write `fix_python_path.bat`. Created if missing. The script is overwritten on every run so template updates land. |

The fix script self-elevates via UAC (`powershell Start-Process -Verb RunAs`), prepends `good_python_dir` to the **System** PATH (HKLM Environment), and is idempotent — re-running it after the fix is in place is a no-op. Modifying System PATH requires administrator privileges, which is why the engine cannot do this itself.

## `project_config` Section

A per-project config file (under `<cwd>/.local-data/<plugin>/config.yaml`) discovered or populated by an autodetect script. Runs before the `config` section so discovered values can be synced into the data-dir config. If autodetect returns `None` and the file is absent, downstream project-scoped phases (e.g. `ini_settings`) are skipped for that plugin.

```json
{
  "project_config": {
    "file": ".local-data/p4-kit/config.yaml",
    "legacy_file": ".claude/p4-kit.yaml",
    "required_fields": {
      "P4PORT": {"user_msg": "Perforce server", "agent_msg": "Ask for P4PORT, write to {config_path}"},
      "DEFAULT_AGENT": {"user_msg": "Review agent", "agent_msg": "Ask for DEFAULT_AGENT", "default": "claude-opus"}
    },
    "autodetect": "custom_bootstrap.py autodetect"
  }
}
```

### `legacy_file` — one-shot path migration

If the manifest declares `legacy_file`, the engine checks whether `<cwd>/<legacy_file>` exists at session start. If it does and `<cwd>/<file>` does not, the engine moves the file to the new path (creating parent dirs as needed) and emits a `project config: migrated <old> -> <new>` action entry. The downstream load/autodetect/required-fields flow then runs against the new path. The migration is idempotent — once the file lives at the new path, subsequent sessions see the legacy file as absent and skip the move.

Use `legacy_file` only when an existing path is being relocated (e.g. moving project config out of `.claude/` and into `.local-data/`); it is not a general-purpose alias.

### `required_fields` — two forms

Both forms are supported; the dict form is preferred for new plugins.

**Dict form** (preferred) — mirrors `config.required_fields`:

| Key | Required? | Description |
|-----|-----------|-------------|
| `user_msg` | Yes (for fix-all) | User-facing description shown when the field is missing |
| `agent_msg` | Yes (for fix-all) | Instructions to the agent. `{config_path}` is expanded to the absolute per-project file path |
| `default` | No | If set, used when the field is absent from both the file and autodetect output. Never overrides an already-populated value |

**String-list form** (legacy) — a flat list of field names. Fields populated by autodetect are synced to the data-dir config; missing fields are left to the separate `config` section for fix-all handling.

### Defaulting behavior (dict form)

1. Autodetect runs (if declared) and contributes any fields it discovers.
2. For any declared field still missing, if a `default` is set, the engine writes it to the project file and logs a `project config: applied defaults [...]` action entry (never silent — see "Every check must log its outcome" in engine-internals).
3. Any field that is still missing **and** has no default becomes a fix-all entry using its `user_msg`/`agent_msg`. The `type` on the failure record is `project_config`.
4. Final values are synced to the plugin's data-dir `config.yaml` so host-side tools can read a single location.

### When to use `project_config` vs `config`

- **`project_config`** holds per-project values that are machine- or developer-specific (the `.uproject` path on this developer's box, this developer's Perforce username), so they live under `<project>/.local-data/<plugin>/config.yaml` and are gitignored. Good for: project-scoped identifiers each developer fills in for themselves, and any per-project default the user may want to override (e.g. `DEFAULT_AGENT`).
- **`config`** holds machine-global values that don't belong in version control (API keys, local install paths). Lives in `~/.claude/plugins/data/<plugin>/config.yaml`.
- The razor: if it should be checked into source control, it goes in `<project>/.claude/`. If it shouldn't, it goes in `<project>/.local-data/<plugin>/` (project-scoped) or `~/.claude/plugins/data/<plugin>/` (user-scoped).

Values set in `project_config` are automatically mirrored into the data-dir config after the project_config phase, so downstream code that reads the data-dir config (e.g. for simple getenv-style lookups) works unchanged.

## `marketplaces` Entry Fields

Each entry in the `marketplaces` array declares a marketplace the engine should ensure is registered (and optionally kept fresh or pinned).

| Field | Required? | Description |
|-------|-----------|-------------|
| `name` | Yes | Marketplace name; also the merge identity key |
| `source` | For registration | Git URL passed to `claude plugin marketplace add` when the marketplace is not yet registered. Optional when it is already registered — the common case for a pin-only override in a user layer |
| `remove` | No | When truthy (or `enabled: false`), deregister the marketplace via `claude plugin marketplace remove` (which also uninstalls its plugins). **Takes precedence over every other field** — `source`/`pin`/`alwaysUpdate` are meaningless for a marketplace being torn down. Idempotent: an already-absent marketplace is a verbose-only ok, so the directive can live in a checked-in layer forever without erroring once the removal has happened. See below |
| `alwaysUpdate` | No | Refresh the marketplace **clone/listing** against its remote every session. NOTE: this does **not** bump *installed plugin versions* — for that the marketplace needs Claude Code's `autoUpdate: true` (set via an `extraKnownMarketplaces` block in a settings.json). Declaring a marketplace here with only `alwaysUpdate` keeps the listing fresh while installed plugins stay pinned — see the `plugin_autoupdate_propagation` fact in SKILL.md. **Ignored while `pin` is set** (a one-line warning action is emitted) |
| `pin` | No | Git committish (SHA or tag) that snapshots the ENTIRE marketplace repo at a moment in time — see below |

### `remove`

Deregisters a marketplace and uninstalls the plugins that came from it. The
canonical use is **tearing down a stray or superseded marketplace across a team**
— declare the removal in a checked-in layer (e.g. `<project>/.claude/bootstrap.json`)
and every teammate's next bootstrap pass removes it through the normal process,
no manual `claude plugin marketplace remove` on each machine.

```json
{
  "marketplaces": [
    {"name": "old-fork", "remove": true}
  ]
}
```

**Behavior**: if the marketplace is not registered, the engine logs a verbose-only
`already removed` ok and does nothing (idempotent — safe to leave the directive in
place permanently). If it is registered, the engine runs `claude plugin marketplace
remove <name>`, logs a `removed` action on success, or records a fix-all failure on
error. `enabled: false` is accepted as a synonym for `remove: true` (mirrors the
`plugins[]` entry's `enabled` field).

**Resurrection**: a `remove` directive only deregisters; it does not block a later
re-add. If some other manifest layer (or a manual `marketplace add`) re-registers
the same marketplace, it comes back. Removal is durable only when nothing else
re-adds it — confirm no bootstrap manifest declares the marketplace before relying
on a one-time teardown.

**Cooldown note**: when the directive lives in a *layered* `bootstrap.json`
(`~/.claude/bootstrap.json` or `<project>/.claude/bootstrap.json`), editing it does
not auto-bypass the per-project cooldown, so run `bash
plugins/bootstrap/scripts/bootstrap-reset-cooldown.sh` (or wait out the cooldown)
for the removal to apply on the next session.

### `pin`

A `pin` checks the marketplace clone out at a fixed commit, so routine publishes stop trickling in. Pinning the whole repo (rather than per-plugin versions) keeps shared libraries and inter-plugin dependencies mutually consistent by construction — every plugin version the engine sees comes from one coherent snapshot of `marketplace.json`.

```json
{
  "marketplaces": [
    {"name": "plugins-kit", "pin": "f7f6276a"}
  ]
}
```

**Behavior**: the engine resolves the committish in the clone (`git rev-parse`, with a `git fetch` + retry when the commit/tag postdates the clone's last fetch), then `git checkout --detach`s the resolved SHA when HEAD differs. It also forces `autoUpdate: false` in `known_marketplaces.json` (so Claude Code's own refresh doesn't pull the clone off the pin) and records the pin — including the pre-pin `autoUpdate` value — in `~/.claude/plugins/data/plugins-kit/bootstrap/marketplace_pins.json`. An unresolvable pin (bad SHA, missing clone) surfaces as a fix-all failure.

**Unpin**: remove the `pin` field. On the next bootstrap pass the engine restores the clone's default branch, runs the normal marketplace update, restores the recorded `autoUpdate` value, and removes the marker entry.

**Recommended placement**: the user layer, `~/.claude/bootstrap.json`. Marketplaces merge by `name` across layers, so a pin-only entry (no `source`) is a one-line override on top of whatever plugin manifest registered the marketplace. A pin declared by any layer wins for the whole engine run — an unpinned entry for the same marketplace in a plugin's own `bootstrap.json` (e.g. bootstrap's `alwaysUpdate` entry) will not unpin or update past it.

**The pin workflow**:

1. Pin to a known-good snapshot (`"pin": "<sha-or-tag>"` in `~/.claude/bootstrap.json`).
2. When ready to take updates, drop the `pin` field — the next pass restores the branch and updates everything.
3. Test the updated state.
4. Re-pin to the new HEAD SHA.

**Cooldown note**: editing a *layered* `bootstrap.json` does not auto-bypass the per-project cooldown (it touches neither plugin registry file), so after changing a pin run `bash plugins/bootstrap/scripts/bootstrap-reset-cooldown.sh` (or wait out the cooldown) for the change to apply.

**Semantics worth knowing**:

- A pin freezes FUTURE drift but **never downgrades** plugins already past the snapshot (the version check is directional). A plugin ahead of the pinned marketplace logs a verbose-only "ahead of the pinned marketplace" notice and is left alone.
- A `min_version` constraint that the pinned snapshot cannot satisfy fails with a message saying so and suggesting you drop the pin.
- The first session after pinning can race Claude Code's own marketplace auto-update once (CC may refresh the clone before the engine forces `autoUpdate: false`); the pin re-checkout self-heals on the next pass.

## `plugins` Entry Fields

Each entry in the `plugins` array declares a plugin the engine should ensure is installed and enabled.

| Field | Required? | Description |
|-------|-----------|-------------|
| `ref` | Yes | Plugin reference in `marketplace:plugin` format |
| `enabled` | No (default `true`) | If `false`, the engine disables the plugin |
| `scope` | No (default `"user"`) | Installation scope (`user` or `project`) |
| `min_version` | No | Minimum required installed version — see below |
| `install` | No (default `"auto"`) | `"auto"` (default) or `"manual"` — see below |

### `install`

Declares how the engine should treat the plugin's installation lifecycle.

- **`"auto"`** (default) — the engine ensures the plugin is installed, scoped, enabled, and up to date on every run. Existing behavior; entries without this field behave identically to before.
- **`"manual"`** — the engine **never installs, enables, disables, or moves scope** for this plugin. The user is expected to opt in with `claude plugin install <plugin>@<marketplace>`. Once installed, the engine still keeps the plugin up to date via `claude plugin update`. The user owns install state; bootstrap owns version freshness.

Use `"manual"` for plugins that should be available in the marketplace but opt-in per developer. Common cases:

- Admin/utility plugins (e.g. `claude-admin`, `claude-sandbox`) — installed only by team members who actually use them.
- Plugins gated by access (license keys, private credentials) — installing them for someone who can't authenticate creates noisy failures.

**Output examples** (install: manual):

When not installed:
```
plugin spryfox-plugins:claude-admin: not installed (install: manual; run `claude plugin install claude-admin@spryfox-plugins` to enable)
```

When installed and up to date:
```
plugin spryfox-plugins:claude-admin: up to date (install: manual)
```

When installed and a new version is available:
```
plugin spryfox-plugins:claude-admin: updated 0.1.0 -> 0.2.0 (install: manual)
```

**Interactions:**

- `enabled` and `scope` are ignored when `install: "manual"` — the user owns those decisions.
- `min_version` is currently honored only for `install: "auto"` entries. If you need a minimum version on a manual plugin, that constraint has to be communicated to the user out-of-band; the engine does not force-update a manual plugin to satisfy it (would defeat the purpose of "user owns install").

### `min_version`

Declares that the installed plugin must be at least this version. When the constraint is not satisfied, the engine runs `claude plugin update <ref>` and rechecks. If the update succeeds and the installed version now satisfies the constraint, processing continues. If the constraint remains unsatisfied (e.g. the marketplace does not yet have a version new enough), the engine records a failure that surfaces as a fix-all item.

**Output examples**:
```
plugin plugins-kit:bootstrap: installed 0.8.3 < required 0.9.1, running `claude plugin update bootstrap@plugins-kit`
plugin plugins-kit:bootstrap: updated to 0.9.1 (satisfies >= 0.9.1)
```
```
plugin plugins-kit:bootstrap: installed 0.8.3 < required 0.9.1, update failed - <reason>
```

**Comparison semantics**: Numeric dotted versions only (e.g. `0.9.1`, `1.2.3`). Non-numeric parts coerce to 0, so pre-release suffixes like `0.9.1-rc1` are not handled reliably. If you need full specifier grammar (`~=`, `<`), file an issue — this starts as minimum-only.

**Chicken-and-egg for bootstrap itself**: A plugin may declare `plugins-kit:bootstrap` with a `min_version`. This only takes effect once an engine new enough to read the `min_version` field is already running. Older bootstrap engines ignore the field (forward-compatible). If bootstrap itself is too old to recognize the field, the constraint is silently not enforced — consumers should treat the field as advisory in that scenario.

**Layering**: `min_version` participates in the standard merge-by-identity rule (identity key `ref`). If the same plugin ref appears in multiple layers with different `min_version` values, the highest-priority layer wins (it is a scalar field, not a list).

## Script Section

The `script` field declares an optional Python module that runs after manifest processing:

```json
{
  "script": {
    "path": "scripts/bootstrap.py",
    "entry_point": "bootstrap"
  }
}
```

The engine imports the module and calls the entry point function. The script runs in-process within a try/except. See [engine-internals.md](./engine-internals.md) for details on script execution.

**Caveat — `script.path` in a layered (user/project) manifest resolves against
bootstrap's plugin root, not the project.** `_run_script_phase` computes
`script_path = os.path.join(plugin_root, script_def["path"])`
(`bootstrap_lib/engine.py`), and when the layered user/project manifest is
processed the engine passes bootstrap's own `--plugin-root` as `plugin_root`
(not the project directory). So a `script` declared in a `<project>/.claude/bootstrap.json`
or `~/.claude/bootstrap.json` cannot reference a project-relative file — its
`path` is joined onto bootstrap's install directory and, unless a file happens
to exist there, the phase logs `script: skipped (<path> not found)`. The
`script` field is reliable only inside a **per-plugin** `bootstrap.json`, where
`plugin_root` is that plugin's own install directory. (Layered-manifest scripts
receive `project_dir` on their `_ScriptContext`, but the *module path itself* is
still resolved against bootstrap's root.)

## Layered Config Model

The engine supports a 4-layer `bootstrap.json` model — following the same pattern as Claude Code's `settings.json` / `settings.local.json`. This lets users and projects declare bootstrap requirements without creating a plugin.

### Layer Priority

| Priority | File | Scope | Checked in? |
|----------|------|-------|-------------|
| 4 (highest) | `<project>/.claude/bootstrap.local.json` | Project-local | No (gitignored) |
| 3 | `<project>/.claude/bootstrap.json` | Project | Yes |
| 2 | `~/.claude/bootstrap.local.json` | User-local | N/A |
| 1 (lowest) | `~/.claude/bootstrap.json` | User | N/A |

### Merge Semantics

- **Arrays** (plugins, marketplaces, tools, etc.): Unioned by identity key (`ref` for plugins, `name` for marketplaces/tools). When the same identity appears in multiple layers, higher-priority layer's fields win.
- **Objects** (venv, config, project_venv, etc.): Deep-merged, higher priority wins for conflicting keys.
- **path_entries**: Simple string list union (deduplicated, order preserved).
- **Scalars**: Higher priority wins.
- **Explicit `null` is treated as absent**, not as a value — a higher-priority layer cannot null-out a key declared by a lower layer; it can only override it with a non-null value.

### Identity Keys

Each array type below has an identity key used for deduplication during merge.
This table is the exact set of identity-keyed sections in
`bootstrap_lib/manifest_merge.py::_IDENTITY_KEYS` (code is authoritative):

| Array | Identity key |
|-------|-------------|
| `tools` | `name` |
| `marketplaces` | `name` |
| `plugins` | `ref` |
| `fonts` | `name` |
| `json_entries` | `file` |
| `ini_settings` | `file` + `section` (composite) |
| `pypi_packages` | `package` |
| `shared_libs` | `name` |

`path_entries` and `shared_lib_imports` are plain string lists — unioned and
deduplicated (order preserved), not identity-keyed.

**Sections NOT identity-keyed** (e.g. `git_deps`, `sync_to_data`): they are
absent from `_IDENTITY_KEYS`, so `merge_manifests` falls through to plain list
**concatenation** across layers — entries are appended, not deduplicated or
merged by any key. If the same `git_deps`/`sync_to_data` entry is declared in
two layers it appears twice in the merged result. Declare such entries in a
single layer to avoid duplicates.

> Note: `json_entries` merges by the `file` key, but the phase itself reads the
> entry's `target` field (see the json_entries schema above); the merge and the
> phase use different field names. This is the current code behavior, documented
> here so the table matches `_IDENTITY_KEYS` exactly.

### Example

User-level `~/.claude/bootstrap.json` — personal tools across all projects:
```json
{
  "tools": [
    {"name": "uv"},
    {"name": "git"},
    {"name": "gh"}
  ],
  "path_entries": ["~/.local/bin"]
}
```

Project-level `<project>/.claude/bootstrap.json` — project-specific requirements:
```json
{
  "tools": [
    {"name": "node", "install": {"macos": "brew install node"}}
  ],
  "marketplaces": [
    {"name": "team-plugins", "source": "https://github.com/team/plugins.git"}
  ]
}
```

The engine merges these layers before processing plugin `bootstrap.json` files (step 4). Layered configs set up the ecosystem (what marketplaces and plugins to use); plugin bootstrap.json files configure individual plugins.

### Migration from user-bootstrap.json

The legacy `user-bootstrap.json` in the data dir is still processed (lowest priority) but emits a deprecation notice. Move its contents to `~/.claude/bootstrap.json`.
