# Dependency Philosophy

Why bootstrap manages its own dependencies the way it does, and what that implies for manifest authors, engine maintainers, and plugin authors.

## Principles

1. **Local-first for self-contained CLI tools; respect the system for toolchains and integrations.** Small single-binary CLI tools (jq, gh, ffmpeg, uv, ripgrep-class things) we download into `~/.local/bin` and manage ourselves — disk usage is cheap and reproducibility across machines isn't. For toolchains, ecosystem tools, and anything that integrates with the OS, we use the system install. See the "When to download vs respect" rubric below.

2. **Take responsibility for the full execution chain.** When a plugin invokes a tool, every link from "user typed `claude`" to "the binary runs" is bootstrap's responsibility — the interpreter, the venv, the binary itself, and the path that resolves the binary's location. We do not assume any of those links exist; we ensure them.

3. **Repair PATH at the start of any process we own, and again after we modify it.** `repair_path()` reads HKLM + HKCU PATH directly from the registry via `winreg` and merges them into `os.environ["PATH"]`, dedup'd. Call it (a) at engine startup before any subprocess fan-out, and (b) immediately after any operation that may have updated registry PATH (winget installs, our own PATH-mutating remediations). Without this, an installer can update PATH in the registry but a long-running parent process keeps its stale snapshot, and rechecks miss.

4. **Find-or-download, never find-and-trust.** If a tool isn't where we put it, we download our own copy rather than asking the user to install it or trying to discover where they did. Telling a user to "restart your IDE so it picks up a new PATH" is contrary to this principle — it makes the user responsible for a link in the chain we should own.

## When to download vs respect the system install

Not every tool fits the "we install our own copy" model. The line:

**Download our own copy** when ALL of these hold:
- Single self-contained binary (or small bundle with all deps inside)
- Under ~50 MB compressed
- No OS-level integration (no services, drivers, registry hooks, credential helpers, shell extensions)
- No shared state with the user's other tooling (no shared config, keys, tokens)
- Security-update cadence is slow and the tool isn't a common CVE target

**Respect the system install** when ANY of these hold:
- It's a toolchain or ecosystem (git, node, ruby, jdk, go, rust)
- It integrates with the OS (kernel drivers, daemons, shell extensions, credential managers, file associations)
- It's licensed or per-user-account (Perforce, anything corporate)
- The user has strong opinions about which version to use (git config, npm tokens, ssh keys all live next to it)
- It's part of OS infrastructure (curl, tar, bash, sh)
- It's in an active security-update stream (git, openssl, curl) where we'd inherit CVE-tracking duty

Applied to common tools:

| Tool | Side | Why |
|------|------|-----|
| jq, gh, ffmpeg, ripgrep, fd, fzf, uv | Download | Single binary, no integration, slow release cadence |
| Python (our standalone runtime) | Download | Deliberate runtime isolation |
| git | System | Toolchain (git-gui, ssh-keygen, msys deps), CVE stream, ssh/gpg integration |
| p4 | System | Licensed, server-coupled, IDE integrations expect the registered install |
| curl, tar | System | OS infrastructure we already depend on |
| node, npm, docker, jdk | System | Toolchain / daemon / ecosystem |
| claude | System | It's the host process running us |

The dual-path engine supports both — per-tool choice via the `download` and `install` blocks. The choice isn't all-or-nothing; it's per tool entry.

## Implications for manifest authors

- Use the rubric above to decide whether a tool gets a `download` block (we manage our own copy under `~/.local/bin`) or only an `install` block (we use the system install, package-manager-driven).
- For download-side tools: provide `url`, `sha256`, and `archive_path` (when the URL points to an archive). Never declare a download target outside `~/.local/`. The engine assumes everything it manages lives under that prefix; placing things elsewhere defeats the cleanup, upgrade, and verify paths.
- For system-side tools: provide an `install` command per supported OS (winget, brew, apt) or `manual` if there's no unattended install. The engine will check that the tool resolves on PATH and record its absolute path — but won't try to manage the install location.
- A failure mode named `installed_but_path_stale` (visible in bootstrap logs and fix-all guidance) means a system install ran but we still couldn't find the binary afterward. For system-side tools this usually means the installer put the binary in a location not in HKLM/HKCU Path — the right fix is usually to add an `installPath` hint to the tool entry, not to tell the user to restart anything. For tools that fit the download rubric, the right fix is to add a `download` block so we stop relying on PATH at all.
- **Tool → PATH linkage (owning the chain, P4 in practice).** When a tool resolves on disk (via `installPath` or a directory `which` found) but that directory is not on the persistent PATH, the engine adds the directory to PATH itself — RC files + Windows User PATH + the live process — rather than declaring success-and-forgetting or emitting a "restart your shell" instruction. "Present on disk but unreachable by bare name" is, for a consumer that invokes the tool by name, not installed. This is the operational form of principle 2 (own the full execution chain including the path that resolves the binary) and principle 4 (never make the user responsible for a link we own). Manifest authors get this for free: a tool entry with an `installPath` no longer needs a parallel `path_entries` entry to be callable. See manifest-reference.md "Tool → PATH linkage."
- **Install exit codes are advisory; the re-check is authoritative.** Package managers exit non-zero for benign "already installed / no upgrade available" states (winget exit 43 is the canonical example). The engine therefore re-checks the tool after every install attempt regardless of exit code, and only records a failure when the tool is still unresolved afterward. Reading a non-zero install exit as failure produced false `install_failed` log lines for tools that were in fact already present.

## Structured managers vs opaque command strings

For the `install` block, prefer the **structured manager object** over an opaque
command string:

- A **plain package** — anything installed by naming it to a package manager —
  gets the structured form: `{"scoop": "bucket/pkg"}`, `{"brew": "formula"}` /
  `{"brew": {"cask": "…"}}`, `{"apt": "pkg"}`. Structured entries get uniform
  detection, uniform elevation handling, and one obvious spelling — the "one
  correct way" for the common case.
- An **opaque command string** is reserved for installs that genuinely *are*
  commands, not packages: curl-pipe installers, `npm install -g …`, `tic`
  terminfo compilation, a distro's deb-repo setup. When such a command needs
  privileges, spell it as `{"command": "…", "elevated": true}` so it routes
  through the elevation queue instead of being attempted in the non-interactive
  hook. A bare string is `{"command": "…", "elevated": false}`.

The rule of thumb: if you would tell a person "install package X", it's a
structured manager entry; if you would hand them a command to paste, it's a
`command` string (with `elevated` set when it needs root/admin). Do not add
version pinning or upgrade fields to the manager backends — pinning is the
`download` block's job, and presence (not version) is what bootstrap checks for
manager-backed tools.

See manifest-reference.md "`install` — Per-OS Install Methods" for the exact
forms, precedence, and elevation mechanics.

## User overrides

Anything in this contract is overridable through the layered `bootstrap.json` hierarchy (priorities: project `bootstrap.local.json` > project `bootstrap.json` > `~/.claude/bootstrap.local.json` > `~/.claude/bootstrap.json` > plugin manifest). Tool entries with the same `name` deep-merge across layers, so users can pin individual fields without restating the whole entry.

Common override patterns:

**Point at a personal or internal mirror.** Useful when GitHub is blocked, throttled, or you keep an audited copy of release artifacts inside your network.

```json
{
  "tools": [{
    "name": "jq",
    "download": {
      "ubuntu-amd64": {"url": "https://my-mirror.internal/jq/1.8.1/jq-linux-amd64"}
    }
  }]
}
```

The plugin's sha256 is preserved; only the URL changes. (If your mirror serves a re-hashed copy, override the `sha256` field too.)

**Force a specific version.** Pin to an older release for reproducibility or to dodge a regression. Override both `url` and `sha256` for the affected `os-arch` keys.

**Disable a download and use the system install.** Drop the recipe by overriding `download` to `{}`; the engine falls through to the `install` block. Or override `installPath` to point at where your system install lives.

```json
{ "tools": [{ "name": "gh", "download": {} }] }
```

**Add a new arch the upstream didn't ship.** If you build an arm64 Linux binary yourself, add it without disturbing the upstream amd64 entry:

```json
{
  "tools": [{
    "name": "jq",
    "download": {
      "ubuntu-arm64": {"url": "https://my-builds.internal/jq-arm64", "sha256": "..."}
    }
  }]
}
```

Things to know:

- The merge is recursive on dicts. `tools[name=X]` is a dict; `tools[name=X].download` is a dict; `tools[name=X].download.<os-arch>` is a dict — all deep-merge. Lists at the leaf (e.g. `venv.check_imports`) concatenate.
- `bootstrap.local.json` files are gitignored — that's the right place for a personal mirror URL or a CI-only credential override.
- The override mechanism is the SAME mechanism that lets plugins layer onto bootstrap and projects layer onto users. There's nothing tool-specific about it.

## Implications for the engine

- Resolution of a managed tool should not depend on PATH lookups for correctness. **The target architecture is: store the absolute path of every managed tool during bootstrap (the location we put it), and invoke it directly from that path at use time.** PATH is a convenience for interactive shells and third-party callers; it is not a contract our own code should depend on.
- Until that migration is complete, the fallback is: `repair_path()` → `shutil.which()` → if missing, download to a known location under `~/.local/bin` → record the absolute path → use the absolute path from then on.
- Every check that may install or update a managed tool must call `repair_path()` between the install step and any subsequent `shutil.which()` recheck. The two install/recheck loops in `bootstrap_lib/engine.py` (self-setup tools and per-plugin tools) are the canonical examples.

## Current vs. target architecture

Today the engine is a hybrid:

- Some tools (Python, uv) are already downloaded and managed under `~/.local/`. These match the target architecture.
- Other tools (git, gh, ffmpeg) are still installed via package managers (winget on Windows, brew/apt elsewhere). These rely on PATH and intermittently hit the `installed_but_path_stale` failure mode when the installer places the binary in a directory that isn't in HKLM/HKCU Path.

The migration direction is: move package-manager-based tools to direct download under `~/.local/bin`, store their absolute paths in bootstrap state, and have callers use those absolute paths instead of relying on PATH. Once a tool is on this model, its checks can shrink from "is it on PATH? did the install update PATH?" to "does the file exist at the path we recorded?" — a much simpler invariant.

The design contract for this migration lives in [`docs/planning/bootstrap/tool-resolution-redesign.md`](../../../../../docs/planning/bootstrap/tool-resolution-redesign.md). Phase 1 (foundational mechanism, additive) has shipped: `bootstrap_lib/tool_paths.py` records every successfully-resolved tool's absolute path, and `BOOTSTRAP_BIN_<TOOL>` env vars are exported via `$CLAUDE_ENV_FILE` for shell consumers. Plugin Python code should use `from bootstrap_lib.tool_paths import resolve; resolve(tool_paths.canonical_data_dir(), "git")`; plugin shell scripts should use `"$BOOTSTRAP_BIN_GIT"`. Existing `shutil.which("git")` call sites can migrate at their own pace.

## Why not just rely on PATH being correct?

PATH is a process-inherited environment variable. On Windows especially, a long-lived parent process (an IDE, a shell session, the user's desktop environment) captures PATH at launch and never sees later registry updates. Installers can succeed, the registry can be correct, and our process can still see a stale PATH that doesn't include the new entry. We can mitigate this with `repair_path()`, but we can't eliminate it — and every line of code that relies on `shutil.which()` is a line that can silently fail in this exact way.

The simpler model is: don't rely on PATH for the tools we own. Store the path; use the path.
