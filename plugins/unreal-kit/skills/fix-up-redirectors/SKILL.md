---
name: fix-up-redirectors
author: christina
skill-type: technique-skill
description: Use when cleaning up Unreal ObjectRedirector assets in a P4-backed UE project. Do NOT use for non-P4 projects.
disable-model-invocation: false
argument-hint: "[mode] [scope] -- mode: 'orphaned_safe' for orphans only; scope: e.g. /Game/Art (omit for whole project). With no args, lists common operations and asks."
---

# Fix Up Redirectors

Unreal's editor command `Fix Up Redirectors in Folder` stalls on the first file someone else has checked out. This skill instead classifies every redirector by P4 safety, fixes only the safe ones in a fresh CL, and tells you who to ping for the rest.

## Two redirector categories

Not every redirector needs the same treatment:

- **Fix-up redirectors** (target exists, has referencers): the standard case. Referencers must be re-saved to point at the redirector's target before the redirector .uasset can be deleted.
- **Orphaned redirectors** (target gone, zero referencers): pure dead pointers. No rewriting needed — just `p4 delete` the .uasset (and its `.umap` sibling, for level redirectors). Much faster than the fix-up path because there's no UE referencer load/save work.
- **Referenced-broken** (target gone, has referencers): genuinely broken. Deleting the redirector would leave dangling refs. Manual cleanup required; the skill flags these but does not touch them.

The classifier emits the fix-up safe set and (optionally) the orphaned safe set as separate JSON files. The apply script consumes either shape.

## Blast Radius and Reference Detection

Redirectors are pointers, and pointers get referenced from places the on-disk asset graph cannot see. This skill's reference scan is heuristic by construction: it catches the cases that have been observed and encoded into the scanner, and quietly misses the rest. A clean scan is a *hint* that a fix is safe, not proof. Treat the residual risk as real even when every check passes -- the cost of an unfixed reference is a silent runtime failure (missing asset at load time, broken Blueprint pin, dangling soft ref) that often only shows up under specific gameplay conditions.

### Known reference channels

What the skill sees today:

- **Hard refs in other `.uasset` files** -- caught by phase-1 discovery via the UE asset registry. The redirector's own referencer list comes from here. Reliable for assets that import each other through standard UE serialization.
- **Soft object paths in other `.uasset` files** -- also caught by phase-1 discovery (the asset registry tracks soft refs). Phase 4's `rename_referencing_soft_object_paths` handles the rewrite.
- **Literal asset-path strings in source files** -- caught by `scripts/scan_code_references.py` (engine in `lib/code_refs.py`). Default extensions: `.cpp`, `.h`, `.hpp`, `.c`, `.cc`, `.cxx`, `.inl`, `.cs`, `.py`, `.ini`, `.uplugin`, `.uproject`. Pattern: regex match for `/Mount/...`-shaped strings, narrowed by (a) mount must be a real `.uproject`/`.uplugin`/`/Engine` mount discovered on disk, (b) the path must resolve to a real `.uasset` or `.umap`. The double filter is what gives the cache its signal-to-noise -- without it, false positives (test fixtures, `/Script/...` class paths, doc URLs, include paths) flood the cache.
- **Level-redirector `.umap` siblings** -- the apply script's delete-only mode pairs a redirector `.uasset` with its `.umap` sibling so the depot never ends up with one half of the pair.

What the skill does NOT see (each one is a real channel that has bitten projects):

- **Configs that aren't in the default extension list.** `.yaml`, `.json`, `.csv`, `.toml`, `.xml`, and any project-specific data formats are ignored unless the user passes `--extensions` to include them. Any project that drives content from data tables or YAML configs has a coverage gap here.
- **Dynamically constructed asset paths.** `FString::Printf("/Game/Items/%s", ItemName)` produces a path at runtime that no static scanner can match. The literal `/Game/Items/` substring won't resolve to an asset on disk, so the scan ignores it.
- **Indirect references through C++ class names.** A `UCLASS()` referenced as `/Script/MyModule.UMyClass` is a class path, not a content path -- and the scanner deliberately filters `/Script/...`. If a redirector is held alive only by a Blueprint that derives from a code class that itself names the asset by string, the chain is invisible to both phases.
- **References inside `.uasset` files that the asset registry doesn't expose** (custom serialization, third-party plugin formats). Phase 1 trusts the registry; anything outside the registry is a blind spot.
- **Redirect chains.** A target that is *itself* a redirector resolves to the eventual asset at load time, but the discovery snapshot only sees one hop. Chains longer than one are rare but possible after a series of renames.
- **References in non-source artifacts** -- `.csv` config tables, perforce-only docs, automated test manifests, build scripts in shells other than the scanned set, anything generated at build time and not committed.
- **References in code outside the scan root.** The scanner walks `--root` (default cwd). A monorepo with sibling tools that reference game content from outside the project tree won't be covered.

### Strategy: directory-sample, soak, then purge

Because the residual risk is real, the apply path is structured as a series of progressively wider commits, not one big purge:

1. **Directory-sampled test slice.** Use `pick_one_per_dir.py` to reduce the safe set to one redirector per package directory. This exercises every directory shape in the safe set (which is where most "this folder has unusual referencers" surprises live), with a CL small enough to revert in one command. In a reference run, 2839 fix-up redirectors collapsed to a 241-redirector subset; 68 orphaned redirectors collapsed to 18.
2. **Apply, soak, verify.** Submit the test slice. Run the same verification you'd run for any content change: smoke playtest, automated tests where they exist, visual diff on referencer assets if any were rewritten. Soak for at least a build cycle so any reference channel the scanner missed has time to surface as a load error or visual regression.
3. **Full purge.** Only after the test slice is clean, re-run the apply on the original safe set (already-fixed redirectors are no-ops, so the second pass is naturally idempotent).

The reason this works: a missed reference channel that breaks N assets in the test slice is cheap to revert (one CL, scoped to ~1% of the directories). The same channel breaking N assets in the full purge is expensive to revert (thousands of files, possibly across many directories whose referencers also got rewritten). Sampling concentrates the blast where reverts are still cheap.

The recommended workflow:

```
classify -> code-ref filter -> directory-sample -> apply test CL
        -> soak (smoke playtest, tests, visual diff) -> apply full purge
```

The fix-up safe set and the orphaned safe set both pass through this pipeline; only the per-direction details differ (orphan path skips the code-ref filter because there are no referencers).

### When to Extend Coverage

When a regression escapes the heuristic -- a missing-asset error, a broken Blueprint, a dangling soft ref after a clean fix-up run -- the playbook is:

1. **Identify the missed channel.** What kind of file held the reference that the scan didn't see? Was it an extension not in `DEFAULT_EXTENSIONS` (e.g. a `.yaml` config, `.csv` data table)? A dynamic path construction? A redirect chain longer than one hop? An asset format outside the registry?
2. **Extend the scan logic.** Most channels live in `lib/code_refs.py`:
   - New file extension -> add to `DEFAULT_EXTENSIONS` (or document that the user must pass `--extensions` for that channel).
   - New path *shape* (e.g. asset paths embedded in JSON quoted strings with extra escaping, or a project-specific naming convention) -> extend `_PATH_RE` or add a parallel matcher; keep the mount + on-disk filter so signal-to-noise stays high.
   - New mount source (e.g. a non-standard plugin layout) -> extend `discover_mount_points`.
   - Channels that aren't text-pattern-matchable (dynamic construction, registry blind spots) -> document the gap in this section instead of pretending the scanner covers it; the honest "we don't see this" is more useful than a false sense of safety.
3. **Invalidate the cache.** This is the easy step to forget. The cache lives at `./.local-data/code_references.yaml` and the filter reuses it for 24 hours by default. After extending coverage, either delete the cache file or pass `--max-age-hours 0` to `filter_safe_by_code_refs.py` so the next run regenerates it. Without this, the filter still reads the pre-fix scan and the regression repeats.
4. **Re-run the filter** (`scripts/filter_safe_by_code_refs.py`) and confirm the previously-missed reference now drops the affected redirector(s) from the safe set.
5. **Update this section.** Move the new channel from "does NOT see" to "what the skill sees today" and note any new extension/flag the user has to pass.

The skill's value is the explicit map of what's covered and what isn't. Every regression that prompts a coverage extension should also prompt an edit to this section so future Claude knows whether the channel is in scope before promising a clean fix.

## When to Use

- Periodic content hygiene (every couple of weeks, or before a content freeze)
- After a rename/move pass that left redirectors behind
- When `Fix Up Redirectors in Folder` keeps failing on locked files
- Cleaning up orphaned redirectors left behind by deleted assets (the orphan path is cheap; consider running it routinely)

## Prerequisites

- Perforce CLI on PATH (`p4`)
- The unreal-kit plugin installed; `ue-runner` available
- A working dir for outputs (the skill defaults to `tmp/redirectors/` in cwd)

## Arguments and modes

The skill takes up to two positional args: an optional **mode keyword** and an optional **scope** (a UE content path like `/Game/Art`). Either, both, or neither may be present.

| Invocation | Mode | Scope | Behavior |
|---|---|---|---|
| `/fix-up-redirectors` | (none -- show menu) | -- | Print the "Common operations" menu below and ask which the user wants. Do NOT start any phase. |
| `/fix-up-redirectors /Game/Art` | full (default) | `/Game/Art` | Run all phases: discover, classify, report, code-ref filter, apply both fix-up safe set and (if user opts in at Phase 3) orphaned safe set. |
| `/fix-up-redirectors orphaned_safe` | orphan-only | `/Game` | Skip the fix-up path entirely. Discover, classify, report orphan counts, then Phase 4 against `orphaned.json` (the apply script auto-detects delete-only mode from the input shape). **Skip Phase 3.5** (orphans have no referencers, so source-code references can't apply). |
| `/fix-up-redirectors orphaned_safe /Game/Art` | orphan-only | `/Game/Art` | Same as orphan-only, but scoped. |

Anything that isn't the literal string `orphaned_safe` is treated as a scope. The mode keyword, if present, must come first.

Future mode keywords (e.g. `fix_up_safe` to skip the orphan path, `referenced_broken` to dump a manual-cleanup report) plug into this same table -- add a row, branch the affected phases.

## Common operations (printed when invoked with no args)

When the user types `/fix-up-redirectors` with no args, print exactly this menu and ask which they want -- do NOT start any phase yet:

```
Fix Up Redirectors -- common operations:

  /fix-up-redirectors
      Show this menu.

  /fix-up-redirectors orphaned_safe
      Delete the "orphaned safe" redirectors (target gone, zero referencers,
      not checked out by anyone). Pure p4 deletes -- no referencer rewrites,
      no code-ref filter. Cheap and routine; consider running every couple
      of weeks.

  /fix-up-redirectors /Game/SomePath
      Full pipeline scoped to a sub-path: classify every redirector under
      /Game/SomePath, run the code-ref filter, apply fix-ups for the safe
      set, optionally delete orphans. Use this for content hygiene after a
      rename/move pass in a specific area.

  /fix-up-redirectors
  (with no scope, after picking a mode)
      Same as above but over all of /Game. Recommended only when the safe
      set is small or after a successful directory-sampled test slice.

  /fix-up-redirectors orphaned_safe /Game/SomePath
      Orphan deletes scoped to a sub-path.

Which would you like to run?
```

Pick the matching mode + scope from the user's reply and re-enter the skill at Phase 1.

## Step tracking

The full pipeline has six phases (Discover, Classify, Report, Code-ref filter, Apply, Final report). Track progress in a TodoWrite list. Per-mode skips:

- **full mode**: all six phases.
- **orphan-only mode**: Discover, Classify, Report (orphan-focused), **skip Phase 3.5**, Apply (`--mode=delete-only`), Final report.

## Recommended: per-directory subset for broad purges

For any safe set with more than ~100 redirectors, run the per-directory subset reducer first and apply that smaller set as a test pass. The reducer picks exactly one redirector per unique package directory, deterministically.

Why this works: most "this might break something" scenarios are directory-shaped (a particular folder has unusual referencers, soft refs, or naming quirks). A one-per-directory slice exercises every directory shape without committing to a multi-thousand-file edit. In a reference run, 2839 safe redirectors collapsed to a 241-redirector subset that ran in ~19 minutes vs. multi-hour for the full purge — and surfaced any breakage early, when it could still be reverted cheaply.

Use the reducer on either the fix-up safe set or the orphaned safe set; the input/output JSON shape is the same.

```bash
~/.claude/plugins/data/plugins-kit/unreal-kit/.venv/Scripts/python.exe \
  "${CLAUDE_PLUGIN_ROOT}/skills/fix-up-redirectors/scripts/pick_one_per_dir.py" \
  --in tmp/redirectors/safe_filtered.json \
  --out tmp/redirectors/safe_per_dir.json
```

Then point Phase 4 at `safe_per_dir.json` instead. After the test CL submits cleanly, run Phase 4 again on the original safe set (with the per-dir entries removed if you want a strict residual, or just re-run the whole thing — already-fixed redirectors are no-ops in the second pass).

## Precondition - bootstrap must have provisioned unreal-kit

Before invoking the plugin venv interpreter (`~/.claude/plugins/data/plugins-kit/unreal-kit/.venv/Scripts/python.exe`) in any phase below, confirm `~/.claude/plugins/data/plugins-kit/unreal-kit/bootstrap.log` exists. If it does not, the venv interpreter path won't exist either and the command fails opaquely (no Python starts, so the in-script guard can't fire). Tell the user "the bootstrap plugin hasn't provisioned unreal-kit -- install/enable plugins-kit:bootstrap and start a new session" and stop.

## Phase 1 - Discover redirectors (UE Python)

Run discovery via the plugin's `ue-runner`. Pass scope via `SCOPE` env var.

```bash
mkdir -p tmp/redirectors
MSYS_NO_PATHCONV=1 SCOPE="${1:-/Game}" \
  "${CLAUDE_PLUGIN_ROOT}/skills/ue-python-api/scripts/ue-runner.cmd" \
  "${CLAUDE_PLUGIN_ROOT}/skills/fix-up-redirectors/scripts/discover_redirectors.py" \
  --copy-output tmp/redirectors/
```

> `MSYS_NO_PATHCONV=1` is required on Windows Git Bash so it doesn't translate `/Game/...` into a Windows path before Python sees it.

The discovery YAML lands at `tmp/redirectors/redirectors_discovery.yaml`. It contains, per redirector: package name, on-disk file, target package, target-exists flag, all referencer files (hard + soft), and a flag for level referencers.

## Phase 2 - Classify safety (host Python)

The classifier is a host-side script (no Unreal needed). Run it from the project root so `p4` picks up the right `P4USER`/`P4CLIENT` from `.p4config.txt`:

```bash
~/.claude/plugins/data/plugins-kit/unreal-kit/.venv/Scripts/python.exe \
  "${CLAUDE_PLUGIN_ROOT}/skills/fix-up-redirectors/scripts/classify_safety.py" \
  --discovery tmp/redirectors/redirectors_discovery.yaml \
  --out-safe tmp/redirectors/safe.json \
  --out-orphaned tmp/redirectors/orphaned.json \
  --out-report tmp/redirectors/report.json
```

The host-side classifier needs `pyyaml`, which the bootstrap engine installs into the unreal-kit plugin's venv at `~/.claude/plugins/data/plugins-kit/unreal-kit/.venv/`. Invoke that venv's Python directly -- the path is stable across plugin versions and resolves the right interpreter regardless of cwd. **Do NOT use `uv run python`** unless the cwd has a matching `pyproject.toml` listing `pyyaml`; from a project root that doesn't (the common case for this skill, since you run from your Unreal project's root for `p4` to pick up `.p4config.txt`), `uv` falls back to a basic Python without `pyyaml` and the script crashes with `ModuleNotFoundError`. On macOS/Linux the venv path is `~/.claude/plugins/data/plugins-kit/unreal-kit/.venv/bin/python`.

The classifier runs `p4 opened -a` once for the workspace, then buckets each redirector:

- `safe` — fix-up safe set: target exists, neither the redirector nor any of its referencers is opened by anyone (levels are included)
- `blocked` — at least one file is opened by a teammate (or you, in another CL); records the user(s)
- `broken` — the redirector's target asset is missing. Sub-buckets:
  - `orphaned_safe` — target gone AND zero referencers AND the redirector .uasset itself is unlocked. Safe to `p4 delete` directly. Emitted to `--out-orphaned` if provided.
  - `orphaned_blocked` — orphaned but the redirector itself is checked out by someone. Re-run later.
  - `referenced_broken` — target gone but referencers exist. Manual cleanup needed; the skill never touches these.
- `non_writable` — at least one referencer file isn't in the local workspace mapping (plugin content we can't edit)

The report also tracks how many `safe` redirectors touch a `.umap` referencer, just for visibility.

`--out-orphaned` is optional. Skip it if you only care about fix-up redirectors; pass it whenever you want to also clean up orphans (recommended for routine hygiene runs).

> Phase 2 does NOT consult the code-references cache. That happens lazily in Phase 4 prep below, so we don't pay for a full source scan unless the user actually decides to apply.

## Phase 3 - Present the report

Read `tmp/redirectors/report.json` and print this exact summary:

```
Scanning <scope>... <total> redirectors found.

  <N>  safe to fix    (<M> touch levels)
  <N>  blocked by P4 checkouts:
       @<user1>  <count>  (CL <#>, CL <#>)
       @<user2>  <count>  (default CL)
       ...
  <N>  broken (target missing):
       <N>  orphaned (zero referencers, safe to delete)
       <N>  orphaned but checked out (re-run later)
       <N>  referenced-broken (manual cleanup needed)
  <N>  in non-writable mounts (plugin content; skipped)
```

Then ask, depending on mode:

- **full mode**: **"Want me to fix the N safe ones in a new CL? [y/N]"** -- and, if there are orphaned redirectors, separately ask whether to delete them in another CL (delete-only mode is independent of the fix-up path; it can run before, after, or instead).
- **orphan-only mode**: only ask about the orphan path: **"Want me to delete the N orphaned redirectors in a new CL? [y/N]"** -- the fix-up safe count is informational; do not offer to fix it in this run (the user explicitly chose `orphaned_safe`).

Do NOT proceed without explicit yes.

## Phase 3.5 - Filter against code references (host Python, only at apply time)

**Skip this phase entirely in orphan-only mode.** Orphans have zero referencers by definition, including zero source-code referencers, so there's nothing for the filter to drop. Running it would just regenerate the cache for no benefit.

A redirector that's still referenced from C++/C#/Python source must NOT be fixed - the code would silently start pointing at a missing asset. We treat code references the same way we treat P4 checkouts: a hard block.

The cache lives at `./.local-data/code_references.yaml` (per-project, not checked in). It's only required when applying. The filter script regenerates it transparently if it's missing or older than 24 hours; otherwise it reuses the cached scan:

```bash
~/.claude/plugins/data/plugins-kit/unreal-kit/.venv/Scripts/python.exe \
  "${CLAUDE_PLUGIN_ROOT}/skills/fix-up-redirectors/scripts/filter_safe_by_code_refs.py" \
  --safe-in tmp/redirectors/safe.json \
  --safe-out tmp/redirectors/safe_filtered.json \
  --report-out tmp/redirectors/code_refs_report.json \
  --max-age-hours 24
```

The scan walks the cwd by default. Override with `--root <path>` if your code lives elsewhere. Default extensions cover C/C++/C#/Python/INI plus `.uproject`/`.uplugin`; override with `--extensions` (comma-separated) to include configs (`.yaml`, `.json`) if your project encodes asset paths in data.

If any redirectors get dropped here, re-print an updated count to the user before proceeding:

```
Code-ref filter: <kept>/<total> remain after dropping <dropped> referenced from source.
```

To force a fresh scan ahead of time (e.g. you just renamed a bunch of assets in source):

```bash
~/.claude/plugins/data/plugins-kit/unreal-kit/.venv/Scripts/python.exe \
  "${CLAUDE_PLUGIN_ROOT}/skills/fix-up-redirectors/scripts/scan_code_references.py"
```

## Phase 4 - Apply fixups (UE Python, after approval)

**Important: SAFE_JSON must be an absolute path.** UE commandlets run from a different cwd than the user's shell (typically `<project>/Binaries/Win64`), and a relative SAFE_JSON path silently misses the file. The apply script normalizes whatever it gets to absolute via `os.path.abspath`, so passing a relative path *usually* works — but pass an absolute path explicitly when scripting from CI or any setting where the cwd is unclear.

For the fix-up safe set, use `safe_filtered.json` from Phase 3.5, NOT the raw `safe.json`:

```bash
SAFE_JSON="$PWD/tmp/redirectors/safe_filtered.json" \
  "${CLAUDE_PLUGIN_ROOT}/skills/ue-python-api/scripts/ue-runner.cmd" \
  "${CLAUDE_PLUGIN_ROOT}/skills/fix-up-redirectors/scripts/apply_fixups.py"
```

For the orphaned safe set (delete-only), point `SAFE_JSON` at `orphaned.json` from Phase 2. The script auto-detects the input shape and switches to delete-only mode (no referencer load/save, no code-ref filter required because orphans have no referencers):

```bash
SAFE_JSON="$PWD/tmp/redirectors/orphaned.json" \
  "${CLAUDE_PLUGIN_ROOT}/skills/ue-python-api/scripts/ue-runner.cmd" \
  "${CLAUDE_PLUGIN_ROOT}/skills/fix-up-redirectors/scripts/apply_fixups.py"
```

To prepend a project-specific CL tag (e.g. for naming conventions like `[Mix, Tool]`), pass it via env:

```bash
CL_DESC_SUFFIX="[Mix, Tool]" SAFE_JSON="$PWD/tmp/redirectors/safe_filtered.json" \
  "${CLAUDE_PLUGIN_ROOT}/skills/ue-python-api/scripts/ue-runner.cmd" \
  "${CLAUDE_PLUGIN_ROOT}/skills/fix-up-redirectors/scripts/apply_fixups.py"
```

The apply script does (fix-up mode):

1. Creates a new pending CL with description `Fix up redirectors: <N> assets in <scope>` (plus `CL_DESC_SUFFIX` if set).
2. `p4 edit -c <CL>` every non-redirector referencer file.
3. UE: load each referencer (resolves redirectors at link time), rewrite soft refs via `rename_referencing_soft_object_paths`, force-save each package.
4. `EditorAssetLibrary.delete_asset` on each redirector to release UE's file handle, then GC, then `p4 reopen -c <CL>` to herd UE-auto-opened deletes into our pending CL (with `p4 delete -c <CL>` as fallback for any not auto-opened).
5. Saves a manifest at `<project>/Saved/PythonOutput/redirectors_apply_<CL>.yaml`.

In delete-only mode: skips steps 2-4 (no referencer load/save needed), opens redirector .uasset files for delete in the new CL, and **automatically includes the `.umap` sibling of every redirector that has one** — level redirectors come in `.uasset`+`.umap` pairs and both files must land in the CL together.

### Phase 4 tail - lock-failure retry

If the apply script reports lock failures (UE held Windows handles even after `delete_asset` returned), it writes a retry list to `<project>/Saved/PythonOutput/redirectors_lock_retry_<CL>.txt`. After the commandlet exits (UE's process is gone, file handles released), run:

```bash
p4 -x - delete -c <CL> < <project>/Saved/PythonOutput/redirectors_lock_retry_<CL>.txt
```

The locked files are still ON DISK when the commandlet exits (UE held the handle long enough that the in-process disk delete never finished), so the retry must be `p4 delete` -- it marks the depot delete and removes the local file in one step. `p4 reconcile` would see an unchanged local file and do nothing. The script prints the exact command at the end of its run. Fallback: if a future UE version finishes the on-disk delete before exiting, `p4 delete` errors per-file with "file not found" -- only then use `p4 -x - reconcile -c <CL>` against the same list (see Edge Cases).

## Phase 5 - Final report

After phase 4, print:

```
Fixed N redirectors in CL <#>.
Skipped M (blocked by checkouts) - re-run later or ping the users above.
```

If there are blocked redirectors, suggest: **"Tell the blocked users to run `/fix-up-redirectors` themselves to handle their slice."**

## Edge Cases

- **No redirectors found:** print "Clean - no redirectors in <scope>." and stop.
- **All redirectors blocked:** still print the report; nothing to apply. Suggest re-running later.
- **Referenced-broken bucket non-empty:** these are redirectors with no target AND with referencers. The skill never touches them — surface them in the report (sample list comes through as `orphaned_samples` / `broken_samples` in `report.json`) and let the user investigate.
- **Phase-4 validation mismatch:** if `p4 opened -c <CL>` doesn't match the expected set, abort and tell the user. Do NOT call `fixup_referencers` against an unverified CL.
- **fixup_referencers reports failures:** UE returns a failure list; note them in the manifest. The CL still contains the partial fixup. The user can decide whether to submit or revert.
- **Re-running mid-fix:** if there's already a pending CL with description starting `Fix up redirectors:`, refuse phase 4 and ask the user to either submit/revert that one first, or pass `--force-new-cl`.
- **UE file-lock errors during `p4 delete`:** UE has been observed to keep Windows file handles open on referencer packages even after `delete_asset` and a GC pass. The apply script catches the OS-level lock errors -- the Windows P4 client emits the message as "being used by another process" (note: not the older "in use by another process"), and "access is denied" is the generic fallback -- per file, writes the affected paths to `redirectors_lock_retry_<CL>.txt`, and prints the exact `p4 -x - delete -c <CL>` command to run after the commandlet exits. **Empirically the locked files are still on disk when the commandlet exits** (UE held the handle long enough that the disk-delete never finished), so the retry uses `p4 delete` (which marks the depot delete and removes the local file in one step), NOT `p4 reconcile` (which would see an unchanged local file and do nothing). If a future UE version actually finishes the on-disk delete before exiting, `p4 delete` will error per-file with "file not found" -- in that case fall back to `p4 -x - reconcile -c <CL> < <list>` against the same list.
- **Level redirectors (`.umap`):** in **both** fix-up and delete-only modes the script automatically includes the `.umap` sibling of every `.uasset` redirector it deletes (the pairing logic runs before UE work and adds any `.umap` that exists on disk to `redirector_files`). Two shapes need this: normal level redirectors with both `.uasset`+`.umap` on disk (both must land in the apply CL or the depot keeps a dangling half), and `.umap`-native packages where discovery (UE asset registry) reports a `.uasset` path but only the `.umap` exists on disk (UE deletes the `.umap` and auto-opens it for delete in default CL — without pairing, the `.umap` is stranded outside the apply CL).
- **Content Collections (`.collection`):** UE's CollectionManager listens for redirector deletions and rewrites every affected `.collection` file under `Content/Collections/`. By default (`UCollectionSettings::bAutoCommitOnSave = true`) it then **auto-submits each collection as its own one-file CL** with the description "Collection '<Name>' not modified" -- the in-memory members are unchanged but the on-disk paths got rewritten. Phase 4 disables `bAutoCommitOnSave` for the lifetime of the commandlet so the rewrites happen but the auto-submits don't; the script then sweeps `Content/Collections/*.collection` in the default CL and `p4 reopen`s them into the apply CL. End result: collection-file rewrites travel inside the apply CL alongside the redirector deletes, no stray one-file submits. If you see `Collection '...' not modified` CLs after a run, the suppression failed (check the manifest's `collections_reopened` field and the warning log line).

## Common Mistakes

- **Skipping validation in phase 4.** Never call `fixup_referencers` without first verifying the CL's opened set matches what discovery promised. A surprise file in the CL means the world moved between discovery and apply.
- **Treating "checked out by me in another CL" as safe.** It's not. Other-CL checkouts are still blocked - the file would land in the wrong CL otherwise.
- **Skipping the code-references filter for fix-up runs.** Phase 3.5 isn't optional for the fix-up path. A redirector that compiles into a string literal in C++/C#/Python source will silently break that code if you fix the redirector and the target asset later moves or is renamed. Always run the filter; never feed `safe.json` directly into Phase 4. (Orphan runs skip the filter — orphans have no referencers, including no source-code referencers.)
- **Running a multi-thousand-file purge as the first apply.** For broad scopes use the per-directory subset reducer (`pick_one_per_dir.py`) for the test pass — it cuts apply time by 10x+ and surfaces breakage early when revert is still cheap.
- **Passing a relative `SAFE_JSON` path from CI.** The apply script normalizes to absolute via `os.path.abspath`, but normalization happens in the commandlet's cwd (typically `<project>/Binaries/Win64`), not yours. Always pass an absolute path explicitly when scripting.
- **Conflating orphaned redirectors with truly broken ones.** "target_exists: false" means two very different things depending on whether anyone references the redirector. The classifier splits these for you; don't lump them back together in tooling that consumes the report.

## Architecture

The skill follows a facade-over-libs structure:

- `scripts/` are thin facades that orchestrate one phase each
  - `discover_redirectors.py` — Phase 1
  - `classify_safety.py` — Phase 2 (emits fix-up safe set + optional orphaned safe set + report)
  - `filter_safe_by_code_refs.py` / `scan_code_references.py` — Phase 3.5
  - `pick_one_per_dir.py` — per-directory subset reducer (works on either safe-set shape)
  - `apply_fixups.py` — Phase 4 (fix-up mode and delete-only mode; auto-detected from the input shape)
- `lib/p4cli.py` — host-side P4 CLI (find, run, parse opened, where mapping)
- `lib/package_paths.py` — UE-side mount-point map and package -> on-disk path
- `lib/redirector_record.py` — YAML/JSON I/O for the discovery and safe-set files (`load_safe_set` / `save_safe_set` are reused by `pick_one_per_dir.py`)
- `lib/code_refs.py` — host-side source scanner + cache I/O for `./.local-data/code_references.yaml` (24h freshness)

The libs are also useful for one-off redirector-related scripts. Import them directly:

```python
import sys, os
sys.path.insert(0, os.path.join(os.environ['CLAUDE_PLUGIN_ROOT'], 'skills', 'fix-up-redirectors', 'lib'))
from p4cli import get_opened_map, run_p4
```
