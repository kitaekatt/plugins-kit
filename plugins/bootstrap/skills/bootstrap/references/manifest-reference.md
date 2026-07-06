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
  "env_vars": [
    {"name": "DEVROOT", "value": "~/Dev"}
  ],
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
    "subdir": "python",
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

## `project_venv` — Project's Own Python Environment

A **layered** manifest (`~/.claude/bootstrap.json` or `<project>/.claude/bootstrap.json`) declares `project_venv` to have bootstrap provision the *project's* venv — synced from the project's own `pyproject.toml` via `uv sync`, verified with `check_imports`. It runs only when the engine has a `--project-dir` (silently skipped otherwise), and never exports a `*_VENV` env var (the venv belongs to the project, not a plugin).

Fields (all optional):

- `extras` — dependency extras (`uv sync --extra <name>` each).
- `check_imports` — module names that must import inside the venv.
- `subdir` — a **project-relative** subdirectory that becomes BOTH the uv-sync project target and the `.venv` parent: `<project>/<subdir>/pyproject.toml` → `<project>/<subdir>/.venv`. Absent = the project root (`<project>/pyproject.toml` → `<project>/.venv`). A `subdir` that is absolute or resolves outside the project is a descriptive `project_venv` failure — no fallback to the root.

Example — env-config, whose Python package lives under `python/`:

```json
{
  "project_venv": {
    "subdir": "python",
    "extras": ["dev"],
    "check_imports": ["yaml"]
  }
}
```

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

## `env_vars` — Persistent Environment Variables

Declares environment variables that software provisioned by bootstrap needs.
Valid in every manifest layer; entries merge by `name` (higher layer's value
wins), so a machine-local `bootstrap.local.json` can override a single
variable's value with a one-line entry.

```json
{
  "env_vars": [
    {"name": "DEVROOT", "value": "~/Dev"}
  ]
}
```

Both fields are required strings. A leading `~` in `value` expands to the
user's home at apply time, so committed manifests stay identity-free.

**Processed first.** `env_vars` is the first phase in every manifest pass —
install commands in any later phase (e.g. a tool `install` invoking
`$DEVROOT/...`) see the variables.

**Semantics per entry**, every pass:

1. **Live export**: set in the engine process (same-pass install commands
   inherit it) and appended as an export line to `$CLAUDE_ENV_FILE` (same
   reach as the `<PLUGIN>_VENV` export — subsequent Bash tool invocations
   in the session see it).
2. **Persistence** (skipped when already in the wanted state, which logs an
   ok entry): on macOS the `export NAME="value"` line is written/updated
   **in place** in `~/.zshrc` and `~/.bashrc` (Ubuntu: `~/.bashrc`) — a
   value change replaces the existing line rather than appending a stale
   duplicate. On Windows the variable is written to the User-scope registry
   (`HKCU\Environment`), same direct-winreg idiom as the PATH linkage. The
   post-set re-check is authoritative; a persistence failure surfaces as a
   fix-all item.

**PATH is not an env_vars concern**: PATH modification belongs exclusively to
`path_entries` and the tool→PATH linkage. Do not declare PATH (or PATH-like
prepend/append edits) as an `env_vars` entry.

**`env_vars` is bootstrap.json's alone.** Environment variables are a
provisioning concern (software can require a variable to run correctly), so
`env_vars` lives here and nowhere else — the sibling `env.json` personalization
manifest (below) has **no** `env_vars` section, and no `env.json` section
touches PATH. This keeps a variable present on every pass where `bootstrap.json`
runs, including passes where the env gate skips `env.json` entirely.

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

- an **opaque command string** — a shell command run verbatim, or one of the
  two string sentinels, `manual` and `skip`;
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
| `"skip"` (sentinel) | any | Not applicable on this OS: the entry is skipped entirely (no check, no install, verbose-only log line). Use for tools wanted only on some OSes. Omitting the OS key instead means "must already resolve on this OS" and surfaces a FAILED item when it does not. Canonical object form: `{"skip": true}`. Do not declare both `"skip"` and a same-OS `download` block. |

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

0. **skip** (`install.<os> == "skip"`) — the entry is not processed on this OS
   (no check, no install, no failure; a verbose-only log line).
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
- **apt** always needs root — see elevation below. The backend runs `apt-get
  update` **once per pass**, immediately before the first *direct* apt install it
  performs, so a stale/empty package index does not fail an installable package.
  It is a single per-pass guard — not per package, and not on the deferred path
  (that update instead leads the emitted remediation script).

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
| `env_vars` | `name` |
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

---

# The `env.json` Personalization Manifest (sibling file)

`env.json` is a **separate manifest file** processed by the same engine, in the
same SessionStart pass, immediately **after** the layered `bootstrap.json`
manifest (so `env_vars` → tools → fonts → path → project_venv have all run — every
variable and binary a personalization references already exists) and **before**
the per-plugin manifests. It is `bootstrap.json`'s *identity-bearing* sibling:
where `bootstrap.json` stays deliberately identity-free (any unseen client can
read it), `env.json` requires a `machines` registry and refuses to run on a
machine it does not recognize. It is where a single user's personal machine
configuration lives — symlinked dotfiles, shell-rc lines, macOS preferences,
login items, and the bespoke check/fix scripts that finish a machine's setup.

Three traits distinguish it from `bootstrap.json`:

1. A **required `machines` registry** and per-entry `os`/`hosts` filters — entries
   are keyed by machine identity, and an unknown machine is a hard error.
2. It is **gated** by a dedicated `env_state.json` stamp: unlike `bootstrap.json`
   (re-checked every session because upstream software drifts underneath it),
   `env.json` runs only when its merged content changed, its last pass was not
   clean, the engine was upgraded, or a reset was requested.
3. **Backwards-readable from v1** (same discipline as `bootstrap.json`): unknown
   top-level sections are ignored with a verbose log line, user files are never
   rewritten on disk, and an engine too old to know `env.json` skips the file
   entirely.

All of `env.json`'s failure types are **manual-attention** items (never
auto-fixable in the fix-all sense): the engine has *already* run each fix in the
same pass. What surfaces is the residue that the fix could not resolve — a
persistent failure that keeps the phase re-running (via the gate) every session
until it converges. `env.json` failures never block `bootstrap.json` provisioning
(tools, fonts, venvs); failure isolation is per-item, as everywhere in the engine.

## File homes and 4-layer precedence

`env.json` uses the same four-layer model as `bootstrap.json`, lowest priority
first:

| Priority | File | Checked in? |
|----------|------|-------------|
| 4 (highest) | `<project>/.claude/env.local.json` | No (gitignored) |
| 3 | `<project>/.claude/env.json` | Yes |
| 2 | `~/.claude/env.local.json` | No (per-machine) |
| 1 (lowest) | `~/.claude/env.json` | Yes |

The **primary tracked home is `~/.claude/env.json`** (the user layer). "It is my
configuration" — a single user's machine setup, tracked in the claude-settings
repo, applied on every session on every machine. The project layers are supported
by the engine as a generic capability; a project may add its own `env.json`, but
the personal fleet content rides in the user layer.

The four layers are merged (via `merge_env_manifests`) before processing, exactly
like `bootstrap.json`'s layers; the merged manifest is what the gate hashes and
the phase processes.

## Merge semantics and identity keys

`env.json` follows the **same merge discipline** as `bootstrap.json` (see *Merge
Semantics* above): identity-keyed array union, dict deep-merge, `path_entries`-style
string-list union, scalar override, and **explicit `null` is treated as absent**
(a higher layer can override a value but cannot null-out a lower layer's key). Only
the identity keys differ — this is the exact set from
`bootstrap_lib/manifest_merge.py::_ENV_IDENTITY_KEYS` (code is authoritative):

| Section | Identity key |
|---------|--------------|
| `symlinks` | `name` |
| `shell_rc` | `name` |
| `macos_defaults` | `domain` + `key` (composite) |
| `macos_hotkeys` | `id` |
| `login_items` | `name` |
| `env_checks` | `name` |

`env.json` has **no** string-list sections. The `machines` registry is a plain
dict keyed by hostname, so it deep-merges generically — a higher layer can add a
machine or override one machine's fields without disturbing the rest.

## The `machines` registry

Any `env.json` that declares entries **must** carry a `machines` registry;
entries are keyed by machine identity, so processing on an unrecognized machine
is refused rather than guessed.

```json
{
  "machines": {
    "christina-mac.local": {"os": "macos"},
    "5090RTX":  {"os": "ubuntu"},
    "2060S":    {"os": "ubuntu"},
    "RTX5090W": {"os": "windows",
                 "skip_repos": ["llm-dev", "claude-settings"]},
    "ricoprime": {"os": "ubuntu", "skip_repos": ["llm-dev"]}
  }
}
```

- **Keys are hostnames.** The current host (`socket.gethostname()`) resolves
  **exact-match first, then the domain-stripped short form**
  (`hostname.split(".", 1)[0]`) — one rule, mirroring terminalcolor-init's
  precedent. The resolved *registry key* is what `hosts` filters name.
- **`os` is required per machine** and cross-checked against the engine's
  `detect_os()`. A declared-vs-detected mismatch is a hard error — it catches a
  hostname collision across dual-boot installs (same hostname, different OS) or a
  registry typo, before any personalization runs.
- **Unknown machine = hard error, no fallback.** If the current hostname resolves
  to no registry entry, the whole env phase fails with one descriptive item
  (`Unknown machine '<h>'. Known machines: ... Add it to ~/.claude/env.json under
  'machines'.`). `bootstrap.json` provisioning is unaffected — software still
  installs; personalization refuses to guess.
- **`hosts`-filter validation (typo protection).** Every hostname referenced by
  *any* entry's `hosts` filter, in any section, must exist as a `machines` key.
  This is validated section-agnostically at parse time — including filters in
  sections this engine version does not yet handle (hostnames are registry facts,
  not section semantics) — and runs *before* host resolution, so a filter typo
  surfaces even on a machine that is itself unregistered.
- **Per-host data fields ride along** for env-config's own consumers: `skip_repos`
  (the repo-sync host axis), `kitty_shortcuts` (setup-ssh-keys display). These are
  opaque to the engine — it reads only `os` — but travel with the registry so a
  single source of truth serves both.

Each of the four registry violations (missing registry, hosts-filter typo, unknown
machine, os mismatch) is a single descriptive persistent failure with an agent-facing
`fix-all` message; none is auto-fixable — the registry is the user's to correct.

## Entry filters: `os` and `hosts`

Every entry in every `env.json` array accepts two optional filters:

- `os: ["macos", "ubuntu", ...]` — apply only on these OSes.
- `hosts: ["5090RTX", ...]` — apply only on these machines (names are `machines`
  registry keys, not raw hostnames — they are the resolved key).

Omitted = applies everywhere `env.json` applies. Both present = **intersection**.
An entry filtered out on this machine logs a verbose skip line (it is *not* a
failure and does not affect the pass result). These are hostname lists, not tags
(a five-machine fleet; tags would be YAGNI).

## The env gate (`env_state.json`)

`env.json` runs only when it needs to. The engine keeps a dedicated stamp,
`env_state.json`, in bootstrap's data dir, recording exactly three fields:

```json
{"manifest_sha256": "<hash>", "engine_version": "<v>", "last_result": "clean|failed"}
```

- `manifest_sha256` — sha256 of the **canonical merged manifest** (sorted-key,
  compact JSON over the *full* merged dict, unknown keys included). "Modified"
  therefore means the merged content changed in any way — an edit, an addition, or
  a removal in *any* of the four layers.
- `engine_version` — the engine that last stamped.
- `last_result` — `clean` (every applicable entry ended ok) or `failed` (any
  failure at all, including a deferred `needs_elevation` or a check-only
  manual-attention item).

**The phase RUNS iff any of** (else it logs one verbose `env: up to date` line and
is skipped entirely):

1. **no stamp** — first run (an explicit reset recreates this state by deleting the
   stamp);
2. **the merged-manifest hash differs** from the stamp (any layer changed);
3. **the last result was not `clean`** — any failure, including `needs_elevation`,
   re-runs the phase every session until green, which is what makes the
   elevation-queue convergence loop work ("run the script, next session's re-check
   clears it");
4. **the engine version changed** — a new engine may understand a section the old
   one ignored, so re-interpret;
5. **a reset was requested** (below).

A **parse error** in any layer also forces the pass and stamps it `failed`, so a
broken `env.json` re-runs every session until the JSON is fixed. A missing/corrupt
stamp is treated as absent (reopen the gate and re-converge). When no `env.json`
exists in any layer, there is nothing to gate or stamp — the phase is a no-op.

### `env-reset-cooldown.sh` — the "re-converge my machine" lever

`plugins/bootstrap/scripts/env-reset-cooldown.sh` deletes the env stamp so the next
session runs the phase. Because the per-project **bootstrap cooldown** gates the
*whole* SessionStart pass (env phase included), the reset script **also clears that
cooldown** (by calling `bootstrap-reset-cooldown.sh`) — otherwise "next session runs
the env phase" would not hold inside the cooldown window. `--status` prints the
current stamp without writing. This is the explicit "check everything now" command;
env-config's thin `update.sh` may wrap it.

Note the env stamp is **independent** of the bootstrap cooldown and is the **only**
gate for the env phase — editing `env.json` never needs `bootstrap-reset-cooldown.sh`
(the merged-hash gate self-detects the edit).

### The drift tradeoff, stated

The stamp records the *manifest hash*, not the *machine's observed state*, and
**the hostname is deliberately NOT part of the stamp**. Two consequences, both by
design:

- **Out-of-band drift is not auto-healed** until an `env.json` edit, a failure, an
  engine upgrade, or a reset opens the gate. A hand-edited rc line, a deleted
  symlink, or a changed macOS default sits un-reconverged until then. This is the
  deliberate trade: personalization changes rarely, so it does not warrant
  `bootstrap.json`'s every-session cadence; the reset script is the explicit lever
  when you *do* want a full re-check.
- **A machine rename with an unchanged manifest stays gated.** Because the hostname
  is not in the stamp, renaming the host does not by itself reopen the phase — the
  merged manifest is identical, so the gate still reads "up to date." That is
  out-of-band drift like any other, healed by a manifest edit or an explicit reset.

## The five declarative feature sections

Five sections carry personal data as **pure configuration**; the engine implements
each mechanism exactly once as a check → fix → **authoritative re-check** pair
(env_var_check's shape). Common contract across all five:

- **Checks are unprivileged and side-effect free.** The gate is an *optimization,
  never a semantic guarantee* — checks still run repeatedly during failure
  convergence, after every manifest edit, and on every reset, so a check that
  mutates state or prompts is a defect regardless of how rarely the gate opens.
- **Fixes are idempotent** — a second pass performs no writes.
- **The re-check is authoritative**, with no trust exceptions (unlike
  `bootstrap.json`'s brew-cask leniency). A fix's own success/failure is advisory;
  the post-fix re-check decides.
- **Per-entry filters** (`os`/`hosts`) and **per-item failure isolation** apply
  throughout; every failure persists across sessions (keeping the gate open).
- Each entry is validated: a malformed entry is a descriptive persistent failure,
  not a crash and not a guess.

### `symlinks`

Ensure `target` is a symlink resolving to `source` (env-config's
ConfigLinkManager semantics).

```json
{"name": "starship-config",
 "source": "~/.claude/dotfiles/starship.toml",
 "target": "~/.config/starship.toml", "backup": true}
```

| Field | Required? | Meaning |
|-------|-----------|---------|
| `name` | Yes | Identity key |
| `source` | Yes | The tracked file the link points at. Must exist — a link "pointing at" a missing source **fails** (a dangling link means the manifest references a file not on disk, a content error to surface) |
| `target` | Yes | Where the link is created |
| `backup` | No (default false) | When a **real file** already sits at `target`, preserve it as a timestamped `.backup_<ts>` sibling before linking; else it is removed |

Paths expand `~` and `$VARS` (an unresolved `$VAR` is an error — declare it via
`bootstrap.json` `env_vars`). A **directory** at `target` is never replaced (a
descriptive failure). An existing symlink (wrong or dangling) is replaced without
backup — a link carries no content worth keeping. A `source == target` entry is
refused (it would self-reference).

### `shell_rc` — two modes: `ensure` and `forbid`

Assert things about `~/.bashrc` and `~/.zshrc`. Exactly one mode per entry
(`content` XOR `forbid`); declaring both, or neither, is a descriptive failure.

**ensure (`content`)** — the block must be present in **every existing** rc file
(the fix appends to every one, so this is the honest postcondition; env-config's
weaker grep-any-file check is deliberately tightened here). The literal
`SHELL_NAME` is substituted per file (`bash` in `.bashrc`, `zsh` in `.zshrc`), so
`starship init SHELL_NAME` renders correctly in each. On a **fresh machine** (no rc
file at all), the fix creates the platform default first — `~/.zshrc` on macOS,
`~/.bashrc` elsewhere — then appends. A block is only ever appended when absent, so
it never doubles.

```json
{"name": "starship-init", "content": "eval \"$(starship init SHELL_NAME)\""}
```

**forbid (`forbid`)** — a regex that must **not** match any line of any existing rc
file; the fix comments matching lines out (`# ` prefix), never deleting them. No rc
file = trivially clean.

```json
{"name": "no-term-override", "forbid": "^\\s*(export\\s+)?TERM=",
 "os": ["macos", "ubuntu"]}
```

> **The `forbid` pattern owns comment-exclusion.** The fix comments out a matching
> line by prefixing `# `. If the *pattern itself* also matches an already-commented
> line, the next pass would re-mutate it (prepending another `# `). The engine does
> **not** special-case comments — the pattern must. Anchor it so a commented line
> cannot match, exactly as the canonical `TERM` example does:
> `^\s*(export\s+)?TERM=` cannot match a `#`-prefixed line because `^\s*` is
> followed by an optional `export` and then `TERM=`, none of which admit a leading
> `#`. Write every `forbid` pattern to exclude comments this way.

**Authoring rule (spec directive 3): `shell_rc` never carries PATH lines** — PATH
belongs exclusively to `bootstrap.json` (`path_entries` + tool→PATH linkage).

### `macos_defaults` — macOS only

`defaults read`/`write` assertions. On any non-macOS host the entire section
no-ops with a verbose skip line (entries may also carry `os` filters, but the
mechanism itself only exists on macOS).

```json
{"domain": "NSGlobalDomain", "key": "InitialKeyRepeat", "value": 25}
```

| Field | Required? | Meaning |
|-------|-----------|---------|
| `domain` | Yes | `defaults` domain |
| `key` | Yes | Preference key |
| `value` | Yes | `bool` (written `-bool`, read as `0`/`1`), `int` (`-int`), or `string` (`-string`). Any other type is an invalid entry |

`domain` + `key` is the composite identity key. After **any** successful write in
the section, the standard preference-cache flush (`killall cfprefsd SystemUIServer`)
runs once for the pass — best-effort (the writes are already committed and
re-checked; the flush only nudges running apps).

### `macos_hotkeys` — macOS only

Remap symbolic hotkeys (the `com.apple.symbolichotkeys` domain).

```json
{"id": 28, "parameters": [48, 29, 1179648], "enabled": true,
 "description": "Screenshot: save screen to file (cmd+shift+0)"}
```

| Field | Required? | Meaning |
|-------|-----------|---------|
| `id` | Yes | Integer hotkey id; the identity key |
| `parameters` | Yes | Non-empty list of ints (key code, modifiers) |
| `enabled` | No (default true) | Whether the hotkey is active |
| `description` | No | Used as the log label |

Check is one side-effect-free plist export compare. Fix is **one**
export → mutate → import round-trip for the whole failing batch, then the cache
flush / process restarts, then a fresh export re-checks each fixed entry (the
re-check is authoritative). An `id` **absent** from the plist is a descriptive
failure — the fix only mutates *existing* hotkey slots; it never fabricates one.

### `login_items` — macOS only

Register an app to launch at login (via System Events).

```json
{"name": "Tailscale", "path": "/Applications/Tailscale.app",
 "hidden": false, "os": ["macos"]}
```

| Field | Required? | Meaning |
|-------|-----------|---------|
| `name` | Yes | Login-item name (identity key) and the name checked against the current login-item list |
| `path` | Yes | App bundle path (expands `~`/`$VARS`) |
| `hidden` | No (default false) | Start hidden |

> **Deviation from env-config:** a **missing app** (path not on disk) is a
> **persistent failure**, not env-config's warning-skip. Under the gate a silent
> skip would stamp the pass `clean` and never converge once the app is later
> installed; a failure keeps the phase re-running until the app appears (bootstrap
> tools run earlier in the same pass) — the gate's convergence loop working as
> designed.

## `env_checks` — the generic check/fix contract

One mechanism covers every non-declarative item — the escape hatch for anything the
five declarative features do not model (gpu-stack, ssh-server, sudoers, plank,
repo-sync, a perforce rider, check-only reminders). An entry is a named `check`
command with an optional `fix` command.

```json
{"name": "ssh-key",
 "check": "test -f ~/.ssh/id_ed25519 -o -f ~/.ssh/id_rsa",
 "fix":   "ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N '' -C \"$USER@$(hostname)\""}
```

| Field | Required? | Meaning |
|-------|-----------|---------|
| `name` | Yes | Identity key |
| `check` | Yes | Command; **exit 0 = configured**, non-zero = not configured. Must be unprivileged and side-effect free |
| `fix` | No | Command run when the check fails. Omitted = a **check-only** entry (manual-attention only) |
| `os` / `hosts` | No | The standard entry filters |
| `elevated` | No (default false) | The fix needs privileges — routed through the elevation queue, never attempted in-pass without privileges |
| `timeout` | No (default 600s) | Per-**command** timeout in seconds (positive int). Contract scripts may drive real installs (gpu-stack), so the bound is generous but never absent |
| `description` | No | The user-facing instruction for a check-only entry's manual-attention item |

**Dispatch per applicable entry:**

1. Run `check`. **Exit 0** → configured (verbose ok, done).
2. **The check could not run at all** — timeout, missing shell, OS error (returncode
   is `None`, not a non-zero exit): a **persistent failure**, and the **fix is NEVER
   attempted**. State is unknown; a check that hangs or cannot run is a
   contract-script defect to surface, not a "not configured" to converge on. (This
   is the third check outcome, distinct from the exit-0 / exit-nonzero grammar.)
3. **Failing and no `fix`** — a **check-only** manual-attention item (`name` +
   `description` + the check's last output line). It keeps the pass `failed`, so the
   phase re-runs until resolved. (Consumers: cuda-toolkit on RTX5090W, reboot-flag.)
4. **`elevated: true` and privileges are missing** — the fix is **never attempted**.
   It is deferred into the elevation queue with the standard
   `{method: "command", command: "<fix>"}` descriptor, so it lands in the same per-OS
   remediation script `bootstrap.json` already writes (bash / self-elevating `.bat`).
   Because a failed pass reopens the gate, the next session's re-check picks up
   out-of-band completion — no new surfacing channel. **Elevated fixes go through the
   queue, never self-elevate in-pass.**
5. **Otherwise run `fix`** (its exit code is **advisory**), then **RE-RUN `check`**.
   The re-check is authoritative, with **no trust exceptions**. Passing → fixed;
   still failing → a persistent failure whose message is the fix's last non-empty
   stdout/stderr line (descriptive errors are the script's job).

> **Odd-reading message, by design:** because the re-check is the sole authority, a
> `fix` that *times out* (returncode `None`, detail `timed out after 600s`) but whose
> **re-check nonetheless passes** counts as **fixed** — and the success line echoes
> the fix's last detail, so it can read `env_check <name>: fixed - timed out after
> 600s`. That is correct: the fix's own outcome is advisory; the passing re-check is
> what "fixed" means.

**A "contract script" is the recommended packaging** for a multi-step item — one file
implementing `check` and `fix` verbs — not a separate engine feature. It lives in
claude-settings under `scripts/env/` and is invoked via a `~`-anchored command.
Conventions (enforced by review, not the engine): idempotent under repeated fix;
`check` exits 0-or-nonzero only (no inverted or tri-state grammar — the old
`checks.yaml` `exit_0_means` machinery does not carry over); multi-step fixes stop at
natural barriers (a reboot) with a clear message and rely on the reopened gate +
re-check to continue next session; print what you did.

### Script resolution: opaque shell strings, not plugin-rooted paths

This is the deliberate contrast with `bootstrap.json`'s `script` phase (whose
`script.path` is joined onto bootstrap's plugin root). `env_checks` `check`/`fix`
values are **opaque shell strings run verbatim through the engine's bash shim** —
there is **no engine-side path joining at all**. A command like
`bash ~/.claude/scripts/env/sudoers.sh check` is resolved **by the shell**
(tilde-anchored to the user's home), not by the engine. On Windows/MSYS the command
runs via Git Bash when available, so Unix syntax works everywhere. This sidesteps the
layered-`script` plugin-root caveat by construction: an `env_checks` command can
reference any file the shell can find, because the engine never tries to locate it.
