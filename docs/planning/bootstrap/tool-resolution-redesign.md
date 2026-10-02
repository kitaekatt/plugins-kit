# Tool Resolution Redesign

Superseded in part: the `BOOTSTRAP_BIN_<TOOL>` session-env export described below was removed in bootstrap 0.143.0 (admission rule: `plugins/bootstrap/skills/bootstrap/references/manifest-reference.md`, "What bootstrap writes to the session env"). The `tool_paths.json` record and `resolve` are unchanged.

Move bootstrap from a "find tools on PATH" model to a "record absolute paths during bootstrap, use those paths directly" model. Plugins stop depending on `shutil.which("git")` and start using `resolve("git")` (Python) or `$BOOTSTRAP_BIN_GIT` (shell).

(Removed in bootstrap 0.143.0; see the banner above.)

This document is the design contract. Implementation lands in phases; the first phase (foundational mechanism, additive only) ships alongside this doc.

## Goals

1. **Eliminate PATH as a correctness contract for bootstrap-managed tools.** PATH stays useful for interactive shells and third-party callers, but no `bootstrap_lib` or plugin code path should fail because a process inherited a stale PATH.
2. **Make tool resolution a file-existence check.** `resolve("git")` returns the absolute path bootstrap recorded; the caller can `os.path.isfile()` it if it wants to verify. No `which`, no PATH walking, no registry merging at call sites.
3. **Migrate without breaking existing plugins.** The new resolution API coexists with the old `shutil.which` model. Plugins migrate at their own pace.

## Non-goals

- Replacing the OS PATH or modifying user environment outside the engine's own process. We do not write to the registry, we do not edit user `.bashrc` / `.zshrc`, we do not install global shims.
- Managing user-installed tools. If the user has their own `git`, we ignore it. We install our own.
- Cross-version coexistence. Each tool has one canonical absolute path at a time; upgrades replace the file at the same location.

## Contract

### State file

Path: `~/.claude/plugins/data/plugins-kit/bootstrap/tool_paths.json`

Shape:

```json
{
  "_schema_version": 1,
  "tools": {
    "git":    {"path": "/home/user/.local/bin/git",       "recorded_at": "2026-05-18T20:14:33Z"},
    "gh":     {"path": "/home/user/.local/bin/gh",        "recorded_at": "2026-05-18T20:14:34Z"},
    "ffmpeg": {"path": "/home/user/.local/bin/ffmpeg",    "recorded_at": "2026-05-18T20:14:35Z"},
    "uv":     {"path": "/home/user/.local/bin/uv",        "recorded_at": "2026-05-18T20:14:30Z"}
  }
}
```

Written atomically (temp file + rename). Engine owns writes; plugins read-only.

### Python API

```python
from bootstrap_lib.tool_paths import resolve, record, all_paths

resolve("git")        # -> "/home/user/.local/bin/git" or None
record("git", path)   # engine-only; appends/replaces an entry, persists atomically
all_paths()           # -> dict[str, str] for diagnostics / debug
```

Lookup is exact-match on tool name. No fuzzy matching, no fallbacks (callers decide whether to fall back to `shutil.which`).

### Shell access

Session-bootstrap exports `BOOTSTRAP_BIN_<TOOL>` for every recorded tool. Tool names are uppercased and `-` becomes `_`. Examples:

```sh
"$BOOTSTRAP_BIN_GIT" status
"$BOOTSTRAP_BIN_FFMPEG" -i input.mp4 ...
```

(Removed in bootstrap 0.143.0; see the banner above.)

Unset variable means bootstrap hasn't recorded a path for that tool yet — caller should fall back to `command -v` and surface a clear error if missing.

## Engine integration

Two existing tool-check loops in `bootstrap_lib/engine.py`:

1. **Self-setup tools** (`_process_self_setup`, ~line 600)
2. **Per-plugin tools** (`_process_manifest`, ~line 1230)

After a successful `check_tool` (initial pass or post-install recheck), the engine calls `tool_paths.record(name, resolved_path)`. `resolved_path` is whatever `check_tool` matched — either the `installPath`/`name` combination or `shutil.which(name)`. The path is recorded once per session per tool; subsequent records for the same tool update the entry only if the path differs.

### Migration of existing tools

No manifest changes required for phase 1. The engine records paths for every tool that resolves successfully under the existing model. This populates `tool_paths.json` and the env vars without changing how tools are installed.

Phase 2 (separate work) introduces a new manifest field, `download`, alongside the existing `install` field:

```json
{
  "name": "git",
  "download": {
    "windows":  {"url": "https://...", "sha256": "...", "archive_path": "bin/git.exe"},
    "macos":    {"url": "https://...", "sha256": "...", "archive_path": "git"},
    "ubuntu":   {"url": "https://...", "sha256": "...", "archive_path": "git"}
  },
  "install": {
    "windows": "winget install --id Git.Git -e --source winget"
  }
}
```

Resolution order, target architecture:
1. If `download` is defined for the current OS: fetch + extract to `~/.local/bin/<name>`, verify hash, record path.
2. Else if `install` is defined: run the package-manager command (the legacy path), recheck via `shutil.which`, record path.
3. Else: failure.

Per-tool migration: once a tool has a working `download` recipe that has shipped and stabilized, its `install` block is removed.

## Why per-tool env vars, not a single bin dir on PATH?

We already prepend `~/.local/bin` to PATH in the shell hook, so in principle plugins could just call `git` and rely on resolution. We don't do that as the contract because:

- It's the failure mode we just spent this entire investigation fixing. As soon as a plugin's PATH inheritance chain breaks (Rider terminal, sub-subprocess, exec'd shim) the call fails. Per-tool env vars make the contract explicit and survive any inheritance chain that preserves env vars (i.e. all of them).
- It documents the dependency. A plugin's shell script using `"$BOOTSTRAP_BIN_GIT"` is self-evidently bootstrap-dependent; a script using `git` is ambiguous.
  (Removed in bootstrap 0.143.0; see the banner above.)
- It catches regressions. If a tool isn't recorded, `$BOOTSTRAP_BIN_GIT` is empty and the call fails loudly. If we relied on PATH, the same situation would silently fall back to whatever `git` the user happens to have, which is exactly the kind of silent divergence we're trying to eliminate.
  (Removed in bootstrap 0.143.0; see the banner above.)

## Open questions tracked elsewhere

- **Download model schema** (phase 2): `archive_path` semantics for non-archived single binaries; how to express multi-file extractions (e.g. ffmpeg with shared libraries); checksum format. Tracked in the eventual download-recipe work, not this doc.
- **Concurrent writes**: today's engine runs once per SessionStart and isn't multi-process. If that changes, `tool_paths.json` needs file-locking. Out of scope until it matters.

## Phase 1 deliverables (shipped)

1. `plugins/bootstrap/bootstrap_lib/tool_paths.py` with `resolve` / `record` / `all_paths` / `tool_env_var_name` / `export_tool_env_vars` / `canonical_data_dir` API.
2. Engine records resolved paths after both tool-check loops.
3. `export_tool_env_vars()` appends `BOOTSTRAP_BIN_<TOOL>` lines to `$CLAUDE_ENV_FILE` so Claude Code picks them up, mirroring `venv_check.export_venv_env_var`.
   (Removed in bootstrap 0.143.0; see the banner above.)
4. Tests for `tool_paths.py` covering record/resolve, atomic write, corrupt-file recovery, env-var name convention, `$CLAUDE_ENV_FILE` export behavior, canonical-dir redirect.
5. Note in `dependency-philosophy.md` pointing at this doc for the target architecture.

## Phase 2 deliverables (shipped)

1. `plugins/bootstrap/bootstrap_lib/downloader.py` with `download_and_install()` — stdlib-only fetch via urllib, streaming sha256, archive extraction (zip / tar / tar.gz / tar.xz), atomic install at `<target_dir>/<binary_name>`, Windows `.exe` auto-suffix, POSIX executable bit.
2. `download` manifest field supported by both engine tool-check loops, ahead of the legacy `install` fallback. Schema:
   ```json
   {
     "name": "jq",
     "download": {
       "windows": {
         "url": "https://...",
         "sha256": "...",
         "binary_name": "jq.exe",
         "archive_path": "bin/jq.exe",
         "archive_type": "zip"
       }
     }
   }
   ```
   `binary_name`, `archive_path`, `archive_type` are optional. Resolution order per tool: initial `check_tool` → `download` (if defined for OS) → legacy `install` (if defined) → failure. The package-manager `install` block stays in each tool entry as a fallback; remove it per-tool once the `download` recipe ships and stabilizes.
3. Tests for the downloader using `file://` URLs and on-disk fixtures (no network) covering single-file download, sha256 mismatch, explicit binary_name, executable bit, zip extraction, tar.gz extraction, archive-type detection, atomic replace of existing file.
4. Consumer migration to use recorded paths:
   - `plugins/git-kit/scripts/bootstrap.py` resolves `gh` via `tool_paths.resolve()` first, falls back to `shutil.which`.
   - `plugins/claude-ui-kit/scripts/statusline.sh` uses `${BOOTSTRAP_BIN_JQ:-jq}` instead of bare `jq`.
   - `plugins/p4-kit/scripts/prepare_review.py` resolves `p4` via `tool_paths.resolve()` first, falls back to bare `"p4"`.
   - `plugins/unreal-kit/.../p4cli.py::find_p4` checks `tool_paths.resolve()` before its existing `shutil.which` + hardcoded-paths chain.

## Phase 3 deliverables (shipped)

1. **Per-arch lookup.** Manifest authors can key `download` entries by `os-arch` (e.g. `macos-arm64`, `windows-amd64`, `ubuntu-amd64`) in addition to plain `os`. The engine prefers `os-arch` and falls back to `os`. `platform_detect.detect_arch()` normalizes `x86_64`/`AMD64`/`x64` to `amd64` and `aarch64` to `arm64`.
2. **Live recipes for jq and gh.**
   - `plugins/bootstrap/bootstrap.json` — jq 1.8.1 download blocks for `windows-amd64`, `macos-amd64`, `macos-arm64`, `ubuntu-amd64`. Legacy `install` block retained as fallback.
   - `plugins/git-kit/bootstrap.json` — gh 2.92.0 download blocks for the same four target tuples. Archive extraction (`zip` on Windows/macOS, `tar.gz` on Ubuntu) handled by the downloader.

## Not done (open items)

- **ffmpeg download recipe.** Recipe slot is supported by the engine; no concrete URL/sha256 wired up yet. ffmpeg on Windows is a ~100MB zip — close to the rubric's "under 50MB" boundary and worth a deliberate decision on whether to bundle vs. respect the system install. Currently ffmpeg is not declared as a tool dep in any plugin's `bootstrap.json`, so this is latent rather than blocking.
- **Tools intentionally on system install** (per the dependency philosophy rubric): `git`, `p4`, `curl`, `tar`, `claude`. These stay on `install`-only entries; their absolute paths are still recorded in `tool_paths.json` when `shutil.which` succeeds.
- **Recipe freshness.** jq 1.8.1 and gh 2.92.0 are pinned by sha256. There's no auto-update mechanism — a deliberate bump-the-version commit is required when a new release lands. Future work could add a CI job that checks for upstream releases and opens a PR.
