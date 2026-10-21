# CLAUDE.md

## Project Overview

Spirit Crossing is an Unreal Engine 5 game with a custom C++ backend server. The codebase is split into two main areas:

- **SpiritCrossing/**: Unreal Engine 5 game client and editor code
- **SpryFox/**: Custom C++ server, backend systems, and shared code

The project uses Unreal Build Tool (UBT) for building the editor, client, server, validation, mapbuilder, lens, bot, tests, and customer support projects.
FASTBuild (FBuild) is used to generate the SpryFox solution but most of the projects in that solution are built with UBT using build.py.

## Bash and the project's tools

Bash is a subprocess launcher; for project work you invoke native tools (`p4`, `python.bat`, `powershell`, `build.sh`) through bash. The capabilities below are the canonical operations. Most exist because the alternative trips the Bash safety checker, which scans every command string independently of `permissions.allow`; an allow-list entry does NOT bypass the safety checker, so rewriting the command is the fix, never adding more allow entries. The two search bullets are the exception -- they exist for context cost, not for the safety checker, and `Bash(grep*)` is allowlisted and does not prompt.

- **Modify a file** -- use the Write or Edit tool, not `cat >` / `echo >` / `>>` / heredoc. Shell redirection requires safety approval; Write and Edit do not.
- **Bulk find-and-replace across .csv / .yaml / .md files** -- if `/designer:bulk-rename` is available it is mandatory: invoke it, not `(Get-Content X) -replace ... | Set-Content X` or `sed -i`. PowerShell's `Get-Content`/`Set-Content` defaults to the system encoding (UTF-16LE on Windows) and silently corrupts non-ASCII bytes -- smart quotes become diamonds, emoji and em dashes become `??` -- on lines that are NOT the target of the replacement. `/designer:bulk-rename` reads/writes bytes (no encoding round-trip), refuses pre-broken CSV/YAML, and verifies post-rewrite that the file still parses. Use this for NPC ID renames, config-key renames, or any identifier-replace across many text files. If `/designer:bulk-rename` is not installed the encoding hazard is unchanged, so the PowerShell round-trip stays off the table -- use the Edit tool per file instead.
- **Extract a field from text** -- use `cut -d' ' -f3`, not `awk '{print $3}'`. `$N` triggers a safety prompt no allow entry bypasses.
- **Run-then-stop a command** -- use `timeout 3 cmd 2>&1 || true`, not `cmd & ... kill %1`. The `&` operator triggers a prompt regardless of surrounding allow entries.
- **Argument containing `#`** -- single-quote it: `p4 diff -du '135568#1'`. Unquoted or double-quoted `#` triggers a mid-word safety prompt.
- **Search file contents** -- bound every search with a path and a result limit. Search a directory you have a reason to suspect, not the repository root, and cap the output (`grep -m`, a `head`, the Grep tool's `head_limit`). Either the Grep tool or bash `grep` is fine; what costs context is an unbounded sweep, not which tool ran it.
- **Never run an unbounded search over the session transcripts** -- the `.jsonl` files under `~/.claude/projects` are Claude Code's own transcripts, and a recursive search rooted at or above them is the most expensive command measured in this project: two such calls spent about 1.37M tokens. Search a narrower path under there instead; the transcripts themselves are what to stay out of.
- **Launch a Windows-native binary** -- `powershell -NoProfile -Command "..."`, not `cmd /c <tool>`. `cmd /c` swallows stdout silently. Scope: this governs the command strings YOU compose for the Bash tool, like every rule in this section. It does NOT govern a `subprocess` call inside project Python, where an argv list goes straight to `CreateProcess` with no shell and no stdout interposed. A `.bat` is the case that legitimately needs a shell there -- `CreateProcess` cannot execute a batch file at all, so Python invoking `python.bat` must prepend `cmd.exe /c` and make the interpreter path absolute (worked example: `cmd_import_mt` in [loc.py](.claude/skills/localization-domain/scripts/loc/loc.py)). PowerShell is not a substitute in that position, and reading this bullet as a ban on `cmd.exe` inside Python is a misread that has already cost a review cycle.
- **Run a Python script** -- canonical wrapper: `powershell -NoProfile -Command "\$ErrorActionPreference='SilentlyContinue'; cd $PWD; & SpiritCrossing\Scripts\python.bat <script> <args>; exit \$LASTEXITCODE"`. Never bare `python` / `python3` / `py` -- it picks up the system Python (often 3.13) and fails with `ModuleNotFoundError`. The wrapper activates the project venv, suppresses C++ DLL stderr, preserves cwd, and propagates the exit code.
- **PowerShell with a Windows path** -- outer single quotes: `powershell -Command '& "C:\Program Files\Tool\x.exe"'`. Bash mangles `\P` etc. inside double quotes.
- **PowerShell with a `$variable`** -- pick exactly one form: outer `"..."` with `\$var`, OR outer `'...'` with bare `$var`. Outer `'...'` with `\$var` does NOT work; bash does not process `\$` inside single quotes.
- **PowerShell running a C++ tool** -- prepend `\$ErrorActionPreference='SilentlyContinue'` (DLLs write benign stderr that otherwise fails the build) and append `; exit \$LASTEXITCODE` (so the underlying tool's exit code propagates).
- **Read a `.uasset` blueprint** -- invoke `/ue-blueprint-details`, not `./build.sh get_blueprint_details`.

For deeper cases: `/core:powershell` (multi-line commands, `.ps1` scripts, multi-`$variable` patterns); `/python-coding` (nested-quoting or broken-venv recovery).

## Working Approach

**Unavailable Capabilities**
When a project-expected skill, plugin, tool, MCP integration, bootstrap component, or provisioned runtime is unavailable, report the capability and its concrete consequence once. Use its documented safe fallback and continue unrelated work. If no safe fallback exists, stop only the dependent subtask. Treat recovery as separate work: do not ask the user to install, enable, retry, restart, or switch workspaces. State a cause only when evidence establishes it.

**Skills from Uninstalled Plugins**
A skill reference in `<plugin>:<skill>` form (`/prototyping:tdd`, `/engineer:investigate-crash`) names the plugin that provides the skill. Not every developer installs every plugin, so a skill or document can tell you to run a skill whose plugin is not installed in this session. Skip that instruction, tell the user you skipped it because the `<plugin>` plugin is not installed, and continue with the rest of the work. Do not offer to install the plugin, and do not report the reference as broken or remove it -- the reference is correct.

**Multi-Item Work**
- When multiple items need to be implemented, select a sensible implementation order and implement all of them. Only ask the user to make a choice when there are genuinely divergent options (different outcomes, not different orderings).
- Anti-pattern: implementing A, B, C and asking for permission to continue after each item.
- Anti-pattern: asking the user to pick an order. This delays completing the user's actual goal, which is that everything is implemented.

**Prefer the Simplest Complete Fix**
Prefer the simplest fix that is complete, correct, and offers full coverage. Don't suggest partial fixes that are quicker to implement but don't fully address the underlying concern.
- **Complete** anti-pattern: fixes the problem for now, but it will return in the future.
- **Correct** anti-pattern: the fix replaces one problem with a different one that is less severe.
- **Coverage** anti-pattern: the fix addresses 80% of the cases but doesn't address the other 20%.
- **Cost-savings** anti-pattern: the fix is inadequate but quick --a misplaced desire to achieve the goal with less effort delivers an inadequate solution.

Concrete anti-patterns:
- When fixing a bug, suggesting a simpler fix that doesn't address 20% of the problem.
- Writing a script to fix broken data without fixing the pipeline that allowed the broken data to enter the system.

If you suspect the user doesn't want a complete, correct, full-coverage fix, ask them. For example: "It sounds like you want to resolve the issue ASAP, versus identifying the root cause and preventing it in the future. Should I focus on a quick fix?"

**Complete Always-Required Work Before Handing Back**
When a task has a step that is *always* required to finish it -- a code review, a build verification, a test run, a config validation -- run that step yourself before asking the user to review, approve, or submit. Asking permission for always-required work adds a round-trip without changing the outcome, and the user's review is faster and better-informed when the always-required work has already produced its signal.

Test: would the answer to "should I run X?" ever be "no" for this task? If the answer is always "yes," run X without asking. The work itself is the gate; the user's role is to act on the signal it produces, not to authorize its production.

- When `/p4-kit:p4-code-review` is available it is the mandatory gate on a CL the user is ready to SUBMIT; when it is not installed the gate does not apply, and the rest of this bullet, the shelf-cleanup bullet below it, and the documentation-CL bullet are moot. The test-suite anti-pattern still applies regardless of whether `/p4-kit:p4-code-review` is installed. **The trigger is submit intent, not the work looking finished.** The user has expressed it when they say they are ready to submit, ask you to shelve for Swarm, ask for the review, or ask whether the CL is ready -- and a running `/spiritcrossing-gates:preflight` or `/spiritcrossing-gates:submit` carries it too. Once they have, run the review without asking: asking permission for always-required work adds a round-trip without changing the outcome. Anti-pattern: asking the user to review, submit, or look at a submit-ready CL without first running `/p4-kit:p4-code-review` on it -- or asking whether to run it. Just run it, fix what it finds, then hand back the cleaned-up CL. Invoking `/spiritcrossing-gates:deliver` is submit intent for this gate: the loop re-runs review whenever the open files change, so the CL it hands back is the CL it reviewed. Submission itself stays manual.
- The inverse anti-pattern, and the more expensive one: running `/p4-kit:p4-code-review` on a CL the user is still iterating on. Finishing an edit is not submit intent, and neither is handing a CL back -- a common workflow is to hand back a CL to test it in PIE, fix what the test shows, and only then submit, so a review fired at handback reviews a diff that is about to change and spends minutes the user did not ask for. Observed: config-only CLs 165528, 166546 and 166663, where reviews ran unasked and two of the three reviewed a diff that is not what shipped. When you have finished the edits and hold no submit signal, say the CL is ready for them to test and stop; they will tell you when it is ready to go.
- Anti-pattern: shelving a CL to feed `/p4-kit:p4-code-review` (which reads the diff via `p4 describe -S`) and leaving the shelf attached when handing back for submit. On this server, `p4 submit` will not proceed while a shelf is attached to the same CL -- the user has to delete the shelf themselves before submit goes through. The shelf was a means to running review, not the end-state. After review completes, run `p4 shelve -d -c <CL>` on every CL whose shelf only existed to feed the reviewer. Same principle as the test suite -- once the signal is captured, the scaffolding goes.
- Anti-pattern: declaring a code change "ready" without running the test suite. The user always wants to know whether tests pass; run them and surface the result.
- The `/p4-kit:p4-code-review` gate covers documentation CLs too, including CLs that touch only `.md` files. It audits .md changes against project standards and verifies references. One review covers everything; it replaces the separate `/md-audit` handback step.
- This principle is the generic shape behind several existing domain rules -- they stay in their domain sections for findability, but read them as instances of "complete the always-required work before handing back":
  - Build invariants -- "Never present a code change as complete without a verified build."
  - GameConfigs submit gate -- "`./build.sh configbinaries` must pass before submit." (See `GameConfigs/CLAUDE.md`.)

Companion to *Multi-Item Work* (don't ask order on independent items) and *Prefer the Simplest Complete Fix* (don't ship a partial fix). Together: complete the work, verify it, then hand back.

## Auto Mode

Auto Mode lets Claude execute work continuously without per-action confirmation. The runtime keeps one safety gate: actions failing any of three axioms still require explicit confirmation. Use this framework to recognize which actions trip the gate and which are safe to proceed.

### The three axioms

1. **Reversibility** -- can I undo this with a local command, no coordination, no apology?
2. **Blast radius** -- does the effect cross my workspace boundary into shared systems (depot, remote, prod, teammates)?
3. **Exposure** -- does this publish data to a destination that doesn't already have it (third-party services, public web tools, vendor APIs other than Anthropic for this Claude Code session)?

Pass all three -> pre-authorized. Fail any one -> confirm.

### Worked example: `p4 reconcile` of an orphaned local config file (OK)

Situation: a plugin migration left `legacy-config.yaml` orphaned under the `.claude/` directory -- the file is gone from disk and P4 needs to be told to mark it for delete in my pending CL. (The filename is illustrative; it is deliberately not a path that resolves, which is why it is not written as a link.)

- **Reversibility**: PASS. `p4 revert` on that file undoes the open-for-delete in seconds; `p4 sync` re-downloads the content from the depot if needed. No coordination required.
- **Blast radius**: PASS. The action mutates only my pending CL. The depot is unchanged until `p4 submit`; teammates are unaffected. The CL is visible via `p4 opened` but does not impose on anyone yet.
- **Exposure**: PASS. The action talks to our own Perforce server, which already holds this file's history. No third-party destination.

All three axioms pass -> pre-authorized. Proceed without confirmation. This generalizes: every P4 verb that builds up a pending CL (`p4 edit`, `p4 add`, `p4 delete`, `p4 reopen`, `p4 unshelve`, `p4 resolve -am`) passes all three for the same reasons.

### Contrast: `p4 submit` of a pending CL (NOT OK without explicit approval)

Same evaluation, opposite outcome:

- **Reversibility**: FAIL. Once submitted, the change is in depot history. Backing it out requires another CL, and the original submission record stays. Anyone who has already `p4 sync`'d the change has it on disk.
- **Blast radius**: FAIL. The depot is the team's source of truth; a submit changes the state every teammate's next `p4 sync` pulls.
- **Exposure**: pass (the Perforce server already had the pending CL's metadata) -- but irrelevant; the action already failed two axioms.

Failing even one axiom triggers the gate. Two failures here means this stays gated even after a generic "ship it" -- explicit per-action authorization is required. This is exactly why "Submit safety" below treats `p4 submit` as the irreversible publication step.

### When the gate is over-conservative

The runtime gate sometimes fires on actions that pass all three axioms because it pattern-matches on a coarse cue (e.g. "any P4 mutation" rather than "P4 publication"). The fix is to write the axiom analysis into the relevant tool-specific section so future sessions can extrapolate -- see "Auto Mode pre-authorization" inside "Working with source control" for the P4 case. Pre-authorization must include the *because* (which axioms it passes and why), not just the verb list, so neighboring cases extend cleanly.

## Development Guidelines

**Files & Output**
- **Never write non-ASCII characters** into source files, configs, scripts, or documents. Use only ASCII (U+0000-U+007F). Fullwidth characters, smart quotes, em dashes, and other Unicode look-alikes silently break parsers, diffs, and builds.
- Non-temporary Python scripts go in `./Tools/Scripts/{P4USER}` --P4USER is in `.p4config.txt`
- Headers live alongside `.cpp` files in `Source/`; config schemas live in `.fbs` files
- **Never hardcode absolute paths** in shared files (CLAUDE.md, skills, scripts). Use `$PWD` for the project root --actual paths vary per machine.
- Never edit `Generated/` directories --these are overwritten by codegen
- **Hand files back as clickable links.** When you give the user a file they might open (a brief, a CSV, a config, a source location), emit it as a project-root-relative clickable markdown link so it opens in the IDE -- e.g. `[selector.py:1002](Tools/Scripts/libs/firstpass-ops/firstpass_ops/assignment/selector.py#L1002)`, rooted at the project root with NO leading slash (`GameConfigs/...`, `SpiritCrossing/...`, `.claude/...`), with a line number when pointing at a specific spot. A leading slash (`/GameConfigs/...`) does NOT resolve -- the IDE reads it as an absolute filesystem path and the link is dead. Both workspace files are multi-root (`SpiritCrossingContent.code-workspace` starts at `.`, `SpiritCrossingClassic.code-workspace` starts at `GameConfigs/Real`), and the no-slash project-root form is what resolves. Use the same form for links authored inside `.claude/` docs and skills: `.claude/scripts/check_claude_references.py` resolves a markdown link whose path starts with `.claude/` or a top-level project directory (its `PROJECT_DIRS` list) against the project root and existence-checks it at submit time. Outside `Docs/ClaudeDocumentation/` a loose backticked path is skipped when it starts with a top-level project directory (`GameConfigs/...`, `SpryFox/...`) -- backticked `.claude/...`, `references/...`, `scripts/...`, `hooks/...` paths are checked everywhere -- so a project-directory reference should be a link, not a backtick. Not clickable, so avoid: leading-slash paths, bare backticked paths, absolute OS paths (`d:\dev\...`), `file://` / `vscode://` URLs, dotted `../../` relative paths, and code-fenced paths.

### Using `tmp/`

`tmp/` is the canonical location for ad-hoc scripts, notes, reports, and analysis output. The directory is p4ignored -- files placed here do not enter source control and are disposable by convention. Project conventions for working in `tmp/`:

- **Python scripts that import stdlib modules go in a `tmp/<subdir>/`** (e.g. `tmp/locqaproposal/`), not bare `tmp/`. The project's `tmp/` accumulates helper files with stdlib-shadowing names (`tmp/inspect.py` is a real example). When Python launches a script via `python tmp/script.py`, the script's directory lands on `sys.path[0]`, and a bare-`tmp/` script silently imports `tmp/inspect.py` instead of the stdlib `inspect`, producing confusing `AttributeError`s deep in third-party packages. A subdirectory keeps `sys.path[0]` clean of the bare-tmp clutter.
- **Notes, reports, analysis output**: any extension, any layout, no naming convention.
- **Cleanup**: anything written to `tmp/` may be deleted between sessions. Do not place anything in `tmp/` that you intend to keep.

**Player-facing text**

Claude does not write player-facing copy. The SourceString of any row you add or change under `SpiritCrossing/Content/LocKeys/` is either text a developer supplied, copied verbatim, or exactly `[Developer Text Required]`. This holds for every skill and for ad-hoc requests, and a skill that says otherwise is wrong. What the line needs goes in the Comment column, not the text field. Whenever you write that marker, end your reply by listing each field that needs the developer's text and ask them for it. Never submit a changelist that adds the marker. Full rule, scope and the Comment convention: [LocKeys CLAUDE.md](SpiritCrossing/Content/LocKeys/CLAUDE.md).

**C++ in this codebase**

This project substitutes several standard C++ idioms because of shipping-build / engine-API constraints. Use the substitution, not the standard form.

- **Error reporting** -- error codes / `std::optional` / `istringstream::fail()`, not `try` / `catch` / `throw`. Exceptions are forbidden at runtime; the only approved exception boundaries are SQLite and httplib -- see SpryFox/CLAUDE.md.
- **Runtime guards** -- `SFAssert` / `SFExpect` (plus the `Msg` and `Fail` variants) from `Common/Source/SFAssert.h`, not `checkf` / `ensure`. The standard guards silently no-op in shipping server builds; a guard that fired in editor disappears in cohort (the live/test server environment players connect to) and the server crashes on the next null deref.
- **Integer width in UE code** -- `int64` / `int32` / `uint64` etc., not `int64_t` / `long long`. UE types match engine APIs; mixing widths triggers narrowing-conversion warnings. `int64_t` is fine in pure-`SpryFox/` server code that does not include UE headers.
- **Type aliasing** -- explicit type, not `auto`. Reserve `auto` for unspeakable templated types (iterator types, lambda captures returning a private struct).
- **ID values** -- public API (`GameDB.GetItemIDFromName()`, `SEntityId::Decode()`, `InventoryV2::Read::VisitAllFull()`, `StringTable` factory methods), not `static_cast<WrapperType>(int)` or raw-value extraction. Wrapper types (`ConfigItemID`, `StringTableIdCommon`, `SEntityId`, `CInventoryID`, Ozy ID types) exist to enforce type safety; `static_cast` compiles but destroys it. Sole exception: low-level serialization that explicitly documents the conversion. Worked examples: [id-wrapper-types.md](.claude/docs/id-wrapper-types.md).
- **Local throwaway containers** -- `SpryFox::StackVector<T, N>` ([Collections.h](SpryFox/Common/Source/Collections/Collections.h)), not `std::vector`, for a vector that lives only inside one function: collect-then-iterate id lists, scratch buffers, per-call candidate lists. It keeps up to `N` elements on the stack and only spills to the heap on overflow, so the common case allocates nothing; size `N` to the usual count. Container calls go through `->` (`Ids->push_back(Id)`); range-for works directly. Example: `SCritterSpawner::SpawnPendingCritters`. `std::vector` stays right for members, return values, and lists stored past the call.
- **Network replication** -- the custom Replication API (`ReplicateMarkDirty*`, save-before-replicate), not Unreal replication. `UPROPERTY(Replicated)` / `UFUNCTION(Server)` / `HasAuthority()` look right but are banned -- silent misbehavior in cohort. API: [SpryFox/CLAUDE.md](SpryFox/CLAUDE.md) and [replication-quick-ref.md](.claude/docs/replication-quick-ref.md).

**Conventions**
- UE prefixes: `U` (UObject), `A` (AActor), `F` (struct / non-UObject), `I` (interface), `T` (template), `E` (enum), `S` (Slate); variables PascalCase. UPROPERTY/UFUNCTION specifier tables: [SpiritCrossing/Source/CLAUDE.md](SpiritCrossing/Source/CLAUDE.md).
- Triple-slash javadoc docstrings (`/// brief; @param; @return`).
- `SF_DEBUG` for debug-only code; unused params commented as `void foo(int /*unused*/)`.
- Engine / third-party edits wrap with `// SF Begin - @username: Description` ... `// SF End`.

**Build invariants**
- `persistence::SaveComponent*()` runs BEFORE `ReplicateMarkDirty*()` -- save-first prevents crash data loss.
- Components are static for an entity's lifetime; cannot be added or removed after creation. Design entities upfront. Use `SScopedEntityInit` for creation; its `CreateComponent` runs the component's `Initialize` immediately (never call it again by hand -- a second call duplicates whatever `Initialize` registers or schedules), and scope exit only announces the entity to replication.
- After any `.fbs` schema change, run `./build.sh codegen` before building.
- Never present a code change as complete without a verified build. If you modify a compiled file (`.cpp`, `.h`, `.fbs`, config schemas), build and confirm zero errors yourself, or ask the user to confirm they compiled, before declaring done.

**Design Philosophy**
- Prefer C++ for new logic, but use existing config/YAML infrastructure when it already solves the problem
- Config is "in charge" --C++ implements functionality, config files configure it for designer variants
- No helper libraries --use `SComponentManager`, `SWorldContext`, `SComponentTrait<T>` directly
- State machines orchestrate (spawn/despawn, timers) but don't execute activity logic directly

**Testing**
- Write tests at the lowest tier possible: Catch2 (~1 min) > Unreal Automation (~5 min) > Gauntlet (~10 min)
- Measuring a server cost rather than checking behaviour is a separate tool, not a tier: [bot-driven-server-measurement.md](.claude/docs/bot-driven-server-measurement.md)

**Documentation**
- Search `Docs/` and header files before creating new docs --ask "am I duplicating or complementing?"

## Critical Skills

Before performing these tasks, invoke the corresponding skill for full guidance:
- Invoke `/core:powershell` when (a) authoring a PowerShell command longer than a single tool-call line, (b) writing a `.ps1` script file, or (c) a PowerShell-wrapped command silently produces wrong output despite the rules in `Bash and the project's tools` --the skill has worked examples and the multi-`$variable` `.ps1` pattern.
- Invoke `/python-coding` when (a) the canonical one-liner in `Bash and the project's tools` is not enough --multi-stage scripts, scripts that pass arguments through nested quoting, or scripts that hit `ModuleNotFoundError` despite using `python.bat`-- or (b) the venv is broken or missing and `ResetPythonVenv.bat` is needed.


## Gate kernel (`./gate.sh`)

Workflow gates are scripts, not prose. `./gate.sh` is the single entry point; every subcommand prints one JSON object on stdout and exits **0** pass, **1** fail, **2** could-not-run.

```bash
./gate.sh build --target server --config Development   # build, then assert the binary actually moved
./gate.sh build --target editor --config Development   # editor (Debug builds as DebugGame); then every project module DLL (any Build.cs under SpiritCrossing/Source or Plugins) must be newer than the sources UBT read for it
./gate.sh binary-fresh --target tests                  # fail if the binary predates the newest source
./gate.sh test --filter "*SpiritShrineReveal*"         # refuses a stale binary; pads a wildcard-less filter
./gate.sh cl audit                                     # default-changelist files, missing descriptions, C++/Config mixing
./gate.sh cl submit-ready <CL>                         # description, files, attached shelf
./gate.sh refs --change <CL>                           # reference integrity under .claude/
./gate.sh configbinaries                               # the GameConfigs submit gate
./gate.sh ledger list | show <id> | set <id> ... | remove <id>
./gate.sh ledger list --stale-days 30                  # also warn about summaries older than N days
./gate.sh ledger list --with-cl-state                  # also ask p4 whether each item's CLs shipped
./gate.sh ledger check-cls                             # every referenced changelist still exists
./gate.sh ledger holds                                 # changelists blocked from submission, and why
./gate.sh ledger render                                # rewrite working-context.md from the ledger
./gate.sh ledger migrate                               # one-time seed from the old markdown render
./gate.sh handoff write --item <id>                    # capture plan, next task, held CLs, open files
./gate.sh handoff show                                 # read back the last handoff
./gate.sh routing resolve --agent <role-or-agent>       # which model, and why that one
./gate.sh routing lint                                 # unrouted dispatches, model versions outside the table
./gate.sh routing validate                             # the table's own floors and aliases
./gate.sh routing render                               # WRITES each agent's model from the table; --check only reports drift
./gate.sh plans scan --status active --max-age-days 90
./gate.sh plans set <plan-path> <active|paused|complete|superseded>
./gate.sh plans normalize --apply                      # bring status markers to the current format
./gate.sh plans check-size <plan>                      # target 7 tasks; 12 is the hard cap
```

`ledger set` takes `--cl` repeatably, and it **adds** rather than replaces: holds
are derived from that list, so a replacing flag would lift the hold on every
changelist left out of the call. Use `--clear-cls` to drop them deliberately.

**Prefer `./gate.sh build` and `./gate.sh test` over bare `./build.sh`.** The gate always passes an explicit `--config`, defaulting to Development rather than `build.py`'s Debug, and asserts afterwards that the binary for that config is newer than the newest source file. That closes the trap described under *Build configs* below by construction rather than by warning.

Source: [Tools/Scripts/libs/gate-kernel/](Tools/Scripts/libs/gate-kernel/). Stdlib-only, so `gate.sh` deliberately skips `python.sh` and its per-invocation dependency install.

**The work ledger** is `ledger.json` in the Claude project directory, written only through `./gate.sh ledger`. `working-context.md` beside it is a render -- editing it accomplishes nothing.

## Build System

All build commands go through Python wrappers in `SpiritCrossing/Scripts/` --never invoke FBuild, UBT, or other tools directly.
- `build.py` - Main build orchestration
- `backend.py` - Backend deployment and server management
- `automated.py` - Called by UGS (Unreal Game Sync), Unreal, and build systems

Common commands:
```bash
./build.sh                                     # Full build (server + configs)
./build.sh build server                        # Server only
./build.sh build server --config=Development   # Specific config
./build.sh codegen                             # Code generation (after .fbs changes)
./build.sh configbinaries                      # Build config binaries (does not rebuild config tool!)
./build.sh configs                             # Rebuilds config tool if needed, then config binaries. Prefer for ConfigTool edits.
./build.sh run tests                           # Server tests (Catch2)
./build.sh run tests -- "TestName"             # Specific test
./build.sh run tests --tier=fast               # Only the no-fixture tier; tiers are fast, config, cdn (comma-separated) or all
./build.sh run unreal_tests                    # Unreal automation tests
./build.sh run unreal_tests --test-filter=Name # Specific Unreal test
./build.sh run gauntlet_test --test-name=Name  # Integration test
./build.sh sln combined                        # Generate VS solution
./backend.sh run_cohort                        # Run local server
./build.sh client --config=Development --platform=Win64  # Build client
./build.sh cook --config=Development --platform=Win64 --editor-config=Development    # Cook client
./build.sh deploy --platform=Android           # Deploy to device
./build.sh deploy --platform=SteamDeck         # Push the staged Win64 build to a paired Steam Deck and launch it
```

FASTBuild config: `SpryFox/fbuild.bff`, `SpryFox/Build/Prologue.bff`, `SpryFox/Build/BuildSettings.bff`.

Build configs: Debug (full symbols), Development, Shipping (optimized). **`build.sh` defaults to Debug/DebugGame, NOT Development** (`build.py` docopt: `[default: Debug]`) -- always pass `--config=Development` explicitly to `build server` / `build tests`. Omitting it builds the suffixed `*-Win64-DebugGame.*` binaries and leaves the unsuffixed Development ones (`Server.exe`, `SpiritCrossingTest.exe`) untouched, while still reporting `Result: Succeeded` -- and `run tests --config=Development` then runs the stale Development exe and passes. A fresh suffixed binary next to a stale unsuffixed one is the tell.
Platforms --Client: Win64, Android, iOS. Server: Win64, Linux, Mac (dev only). Use `--platform=<platform>`.

## Spirit Crossing-Specific Architecture

### Backend ("Ozy")

Ozy is Spirit Crossing's custom backend/persistence system. Source: https://github.com/SpryFox/Ozy (private). Local development uses "Fake Ozy" (SQLite, `custom-server.db3`); production uses K8s in AWS. The C++ client code in `SpryFox/{Common,Server}/Source/Ozy/` consumes Ozy's REST API -- no shared source, no automated sync between repos.

```bash
./backend.sh run_cohort          # Run local server
./backend.sh deploy --build_id=X # Deploy to remote
./backend.sh download_cohort     # Download player data
./backend.sh wipe_cohort         # Clear local backend
```

### Custom Networking (NOT Unreal Replication)

Spirit Crossing uses Yojimbo (reliable UDP) + FlatBuffer serialization instead of Unreal replication. Before implementing any network replication, read [SpryFox/CLAUDE.md](SpryFox/CLAUDE.md) (Replication API), [SpiritCrossing/Source/CLAUDE.md](SpiritCrossing/Source/CLAUDE.md) (banned Unreal replication surface) and [replication-quick-ref.md](.claude/docs/replication-quick-ref.md) (advanced features).

### Configuration System

YAML/JSON configs compiled to flatbuffer binaries. Source: `SpiritCrossing/Config/`, tool: `SpryFox/ConfigTool/`. Build: `./build.sh configs` or `./build.sh configbinaries`. Uses variant expansion for environment-specific configs.

### Code Generation

**CodeGeneration2** (SpryFox/CodeGeneration2/, C#). Always run `./build.sh codegen` before building after schema changes.

### Terrain and Map Generation

Procedural terrain: generate artifacts -> bake in Unreal -> cook for clients. Commands: `generate_map_pool`, `bake_all_maps`, `cook_all_maps`, `publish_terrain`.

Server-specific architecture (server components, Ozy persistence, SWorldContext, state machines, quest system) lives in `SpryFox/Server/CLAUDE.md`.

## Testing

Three test tiers: **Catch2** (server, ~1-2 min) > **Unreal Automation** (client, headless, ~5 min) > **Gauntlet** (E2E, ~10 min). Write tests at the lowest tier possible.

The three tiers answer **"does this behave correctly?"**. When the question is instead **"how much does this cost?"** -- event fan-out, listeners woken per publish, tick time, anything counted by `SPerformanceMetrics`, measured on a live world driven by a bot -- that is a different tool and it is not on the ladder above. See [bot-driven-server-measurement.md](.claude/docs/bot-driven-server-measurement.md), which also covers the before/after A/B method and how to tell a valid measurement from a void one. Prove behaviour with Catch2; measure cost with that. Do not try to make one artefact do both.

- **Run tests**: `/prototyping:test server`, `/prototyping:test unreal [filter]`, `/prototyping:test auto`
- **TDD workflow**: `/prototyping:tdd` --red-green-refactor with git checkpoints
- **Debug workflow**: `/prototyping:debug` --4-phase systematic investigation with repro tests
- **C++ coverage (design/how-to)**: adding Clang source-based coverage instrumentation for server tests -- `.claude/docs/test-coverage-tooling.md` (design guide, not yet wired into the build)

## Development Environment

### Python
**Use `SpiritCrossing/Scripts/python.bat`** to run Python scripts --it activates the Engine's bundled Python3 venv. See the canonical PowerShell wrapper in `Bash and the project's tools`; invoke `/python-coding` only for the cases listed in `Critical Skills`.

### Project Generation
```bash
./GenerateProjectFiles.bat          # Windows
./build.sh sln server               # Server-only solution
./build.sh comp_db                  # compile_commands.json for LSP
```

### Debugging and Profiling
Tracy: `./build.sh profile tracy`. Heap: `./build.sh profile heap`. Insights: `./build.sh profile insights`.

Those profile a running process and answer "which function is burning CPU". To instead **quantify a server cost that only exists when a player does something** -- event fan-out, listeners woken per publish, cost before vs after a change -- drive it with a bot and read `SPerformanceMetrics`: [bot-driven-server-measurement.md](.claude/docs/bot-driven-server-measurement.md).

### Claude Code Environment Tips

**Editor binary**: `UnrealEditor-Cmd.exe` (non-monolithic, no platform suffix). Synced via UGS.

## Unreal Tools -- Use the Right Tool

Three tools exist for Unreal work. Choosing the wrong one wastes time or fails silently.

- **Reading asset data from disk** (properties, references, DataTable rows, emote set TMap keys on data assets) -> invoke `/unreal-kit:ue-python-api`. No Editor required. Works via commandlet.
- **Reading Blueprint logic** (execution flow, event graphs, node connections, C++ dependencies, delegate bindings) -> invoke `/ue-blueprint-details`. No Editor required. This is the ONLY way to read `.uasset` blueprint graph structure.
- **Inspecting live/runtime state** (Blueprint variables populated at initialization, spawned actor properties, TMap values on instantiated actors, anything that requires an actor to exist in a level) -> use the MCP server tools (`inspect`, `control_actor`). Editor must be open. Pattern: spawn actor -> inspect property -> delete actor.
- **Modifying the Editor** (creating content, modifying scenes, authoring graphs, PIE, screenshots) -> invoke `/unreal-kit:ue-mcp-server`. Editor must be open. For widget blueprints also read [mcp-widget-authoring.md](.claude/docs/mcp-widget-authoring.md): save and compile status, error codes, and how this bridge narrows that skill's save and event-override rules.
- **Running cheats during PIE** (teleport, add items, time travel, quest management) -> use MCP `control_editor` with `action: console_command`. Routes through the PIE PlayerController. Invoke `/unreal-kit:ue-mcp-server` for usage details and available commands.
- **Driving PIE UI or movement in automation** (pressing menu buttons, moving a list selection, holding a button or stick in one chosen client of a multi-client PIE, asking what each pad button does right now) -> use the per-client synthetic gamepad tools in [pie-gamepad-automation.md](.claude/docs/pie-gamepad-automation.md), not synthetic mouse clicks.

**Availability gates the capability.** `/unreal-kit:ue-python-api` and `/unreal-kit:ue-mcp-server` come from the `unreal-kit` plugin (see `.claude/bootstrap.json`); `/ue-blueprint-details` is repo-local and always present. If `unreal-kit` is not installed, the capability does not exist rather than falling back: `.uasset` data on disk cannot be read another way (raw `Read`/Bash returns binary), and Editor automation is unavailable. Say the tool is unavailable -- do not read `.uasset` files directly or improvise a substitute.

Per-workspace MCP port discovery (each session's editor has its own dynamic port plus a workspace-local endpoint file; there is no static port-to-workspace mapping): `.claude/docs/mcp-port-discovery.md`.

**Key distinction -- static vs runtime data:**
- `/unreal-kit:ue-python-api` reads what's **on disk** -- asset properties, data asset TMap keys (works for `UNPCDialogEmoteSet`, `USpiritDialogEmoteSet`), reference graphs. Fails for Blueprint variables that are populated at runtime.
- MCP `inspect` reads what's **in memory after initialization** -- Blueprint variables, runtime-populated TMaps (e.g., bear `EmoteNameToAnimsMap_0` which is populated by Blueprint logic from animation dependencies). Requires spawning an actor instance.
- `/ue-blueprint-details` reads **Blueprint graph structure** -- what the logic does, not what values it produces.

## Critical Files

- `SpiritCrossing/SpiritCrossing.uproject` --Unreal project definition
- `SpryFox/fbuild.bff` --Backend build configuration
- `SpiritCrossing/Scripts/build.py` --Main build orchestrator
- `SpiritCrossing/Scripts/backend.py` --Backend management
- `SpryFox/Build/BranchConfig.yaml` --Branch-specific configuration

## Documentation

Spirit Crossing has extensive docs. **Check before creating new documentation**.

**Primary** (`Docs/` directory, Markdeep HTML):
- `Docs/Server/ServerComponents.md.html` --**START HERE** for server components
- `Docs/Server/ServerDesign.md.html` --Server architecture
- `Docs/EntiesAndComponents.md.html` --Entity lifecycle
- `Docs/ConnectionFlowBetweenClientAndServer.md.html` --Connection sequence
- `Docs/ConfigFormat/Index.md.html` --Config type reference (produced by doc-gen; materializes after a doc-gen build, like `Generated/`, and is not committed)
- `Docs/ControllerReference.md.html` --Input/controller mappings per `UMenuBase` subclass

**Claude Code quick refs** (`.claude/docs/`): replication, server components, flatbuffer attributes, quest system, UI system, lens (offline loot/spawn evaluator: `.claude/docs/lens-quick-ref.md`), useful SC cheats (`.claude/docs/useful-sc-cheats.md`), music authority (who decides world-item music state, server vs client: `.claude/docs/music-authority-quick-ref.md`), CDN (boot fetches, prewarmed snapshot, zero-CDN QA check: `.claude/docs/cdn-quick-ref.md`), Nintendo devkits (waking a kit before diagnosing a wedge, forcing docked/handheld, baking a command line into the NSP, on-device screenshots, and why a cook needs the editor rebuilt first: `.claude/docs/nintendo-devkit-quick-ref.md`), Steam Deck (pairing, deploying the Win64 build, why Steam or touch misbehave on a hand-copied build: `.claude/docs/steam-deck-quick-ref.md`), PIE gamepad automation (drive a PIE client's UI/movement with per-client synthetic gamepad input and query live bindings: `.claude/docs/pie-gamepad-automation.md`), server logs (query live/dev cohort server logs in Grafana/Loki, get a token: the `engineer:server-logs` skill).

**Before documenting**: Search `Docs/` for existing coverage, check header files, ask "Am I duplicating or complementing?"

## Claude Code Configuration

The `.claude/` directory is checked into Perforce (shared team config):
- `settings.json` --shared hooks
- `commands/`, `skills/` --team slash commands and skills

Per-user files (p4ignored, NOT checked in):
- `CLAUDE.local.md` --personal instructions
- `.claude/settings.local.json` --personal permissions

**Never put absolute paths in shared files.** Use per-user files for machine-specific values.

**Project and personal settings.json files merge, not override.** Both `.claude/settings.json` and `.claude/settings.local.json` (plus the personal `~/.claude/settings.json`) contribute to the allow list. Removing an entry from one file does not deny the command if another file still allows it --check all files when an unexpected allow shows up.

**Adding or modifying a Claude Code plugin**: The project consumes plugins from two marketplaces (`plugins-kit` upstream and `spryfox-plugins` SpryFox-private), wired up in `.claude/bootstrap.json`. To author or change a plugin, see `.claude/docs/spryfox-plugins-reference.md` -- covers repo layout, version-bump rules, local testing via `--plugin-dir`, and the publish flow. If you don't have access to the SpryFox repo, the `git-kit` plugin's bootstrap will tell you how to get it.

### Available Skills

Skills are listed automatically in the system context. Key dependencies:
- `/spiritcrossing-gates:designer-plan-domain`, `/prototyping:tdd`, `/prototyping:debug`: Git features require `/designer:setup` first
- `/claude-sandbox:backup-claude-to-git` (requires the claude-sandbox plugin, manual install), `/git-*`: Require `/designer:setup`

**First time?** Run `/designer:setup` to configure optional features. Core skills work without setup.

**For Unreal Engine Python scripting** (asset inspection, reference graphs): invoke `/unreal-kit:ue-python-api`.

### Using Claude Code Effectively

**Delegate, and name a model every time you do.** This project asks you to use
the Agent tool. Do not wait to be asked per task -- treat gather-shaped work as
something to hand off by default. Inheritance is the expensive default: a
dispatch that names no model runs on the orchestrating model. That was measured
on 2026-09-04 against a control -- two identical dispatches naming no model, one
of which a routing hook rewrote; the untouched one ran at the orchestration tier
and the rewritten one at the gather tier.

| Tier | Model | What goes here |
|------|-------|----------------|
| Orchestration | the main session | Design decisions, coupled edit-verify loops, anything where the next step depends on reading the last result |
| Oracle | `fable` | Plan review, adversarial passes over a diff or a design, critique |
| Analysis | `sonnet` | Precedent research, code-adjacent reasoning, reading an unfamiliar system |
| Gather | `haiku` | File sweeps, greps across many paths, P4 queries, build babysitting, log parsing, changelist triage |

A dispatch that decides no model is a defect, not a default. Name the tier at the
call site -- `Agent(model: "haiku", ...)` -- and pick it from the table above.
Do not delegate a coupled edit-verify loop; the handoff costs more than it saves
when each step depends on the last.

Nothing in this repository enforces any of that, and there is no routing table
here to consult. Both ship with `spiritcrossing-gates`, an opt-in plugin: a
routing table that maps a named role to a tier and holds a floor under judging
work, four role agents, and a hook that refuses a dispatch naming neither a role
nor a model. `./gate.sh routing` is the kernel half and is in the depot, but it
reports could-not-run until a table is supplied, so the subcommands listed under
*Gate kernel* above do nothing useful without the plugin. The `spiritcrossing-gates`
plugin also carries `/spiritcrossing-gates:deliver`, which runs an approved plan
through execution, round-trip verification, review and preflight to a changelist
ready for you to submit.

Installing the plugin is enough. The hooks find the table through the plugin
directory the harness hands them, and a routing subcommand you type yourself
finds it through the harness's own install record, so neither needs
configuring. `GATE_ROUTING_TABLE` still works and still wins, which is how you
point the kernel at a table you are editing rather than the installed one.

Install it with `claude plugin install spiritcrossing-gates@spryfox-plugins
--scope user`. It is deliberately absent from `.claude/bootstrap.json` and
stays that way until it has been reviewed and tested: its dispatch guard
denies a subagent call naming neither a role nor a model, which would break a
session that had not read this section, so it reaches only the people who ask
for it.

Do not write that variable down as a permanent setting. An installed plugin
lives in a version-suffixed directory, so a path written by hand names a
version and stops naming the installed one at the next upgrade. That failure is
quiet: the hooks keep working, and only the typed subcommand breaks, months
after the upgrade that broke it.

**Data and routine analysis**: Delegate analysis of large amounts of data or routine analysis to the `analysis` subagent via the Task tool (`subagent_type: "analysis"`). This agent is fast and inexpensive, and has access to all tools. Use it for tasks like parsing logs, summarizing build output, comparing config files, aggregating test results, or any repetitive analytical work that doesn't require deep architectural reasoning.

**Plan mode investigation**: When you need to use Bash to investigate during plan mode, exit plan mode first, complete the investigation, then re-enter plan mode. Staying in plan mode while running Bash commands spams the user with permissions prompts.

## Working with source control

Perforce is authoritative; `p4` CLI is on PATH. The unit of work is the changelist (CL), not the file. **Every modification to a P4-committable file implies a CL for that work exists.** Files left in the default changelist are an anti-pattern -- they are ungrouped, unreviewable, and harder to revert.

**Why the CL is the unit:** changes inside a well-described CL are cheap to revert (`p4 revert -c <CL>`), shelve, or restructure. Only `p4 submit` is irreversible. The right posture is to act freely inside a CL, not to be cautious about making changes.

**Group by logical unit, not by file count.** When work spans multiple concerns (different buckets of a plan, different domains, different mechanical vs. design-call risk profiles), use separate CLs so each can be reviewed and acted on independently. The C++/Config split below is one specific application; the general rule is one logical unit per CL.

### Operations

- **Modify a tracked file** -- `p4 edit <file>` first. Multi-user opens are fine; "also opened by ..." is informational, not blocking.
- **Add a new file** -- `p4 add <file>`.
- **Move opened files into a CL** -- `p4 reopen -c <cl> <file>`, explicit file paths ONLY. **Never wildcard-reopen** (`p4 reopen -c <cl> <path>/...`): it moves every opened file under the path, including files organized into other pending CLs, and pending-CL membership is recorded nowhere -- a wildcard reopen silently destroys CL organization that cannot be reconstructed. Before any reopen, run `p4 opened` and verify each file's current CL. Enforced by the `.claude/hooks/p4-cl-guard.sh` PreToolUse hook, which also snapshots `p4 opened` to `.local-data/p4-snapshots/opened.log` before every CL-mutating p4 command so membership IS recoverable after the next mistake.
- **Discard a CL's changes** -- `p4 revert -c <cl>`. Cheap and atomic; this is what makes acting freely inside a CL safe.
- **Park a CL server-side** -- `p4 shelve -c <cl>`.
- **Reconcile untracked changes** -- `p4 reconcile <path/...>`.
- **Visually diff opened files** -- `"/c/Program Files/Perforce/p4v.exe" -p4vc diffhave "<local-path>"`. Never `p4vc.bat` (swallows args inconsistently from Git Bash). Depot-to-local: `p4 where //depot/path/file`; third whitespace field is the local path (`cut -d' ' -f3`). Warn before launching >5 windows. `diffhave` for opened files; `p4vc diffprev` with a depot path for submitted CLs. Text-only: `p4 diff -du <file>` for a single file, `p4 describe -S -du <CL>` for a whole changelist. **`p4 diff` has no `-c` option** -- `p4 diff -du -c <CL>` writes "Invalid option: -c." to stderr and **zero bytes to stdout**, so a pipeline counting stdout lines reports zero changed lines for every CL and reads as "the work is gone" rather than as an error (p4 exits 1, but a pipe discards it). One caveat on the working forms: `describe -S -du` prints 0 diff lines for `+l` binary types by design, so size-check those instead of reading a diff.

### Submit safety

Submit is the irreversible step.
- Never `p4 submit` without explicit user approval.
- Never `p4 submit` a changelist that adds `[Developer Text Required]` to a CSV under `SpiritCrossing/Content/LocKeys/`. The marker means a player-facing field is waiting for a developer's text. List the fields for the developer instead; they can supply the text, or submit the changelist themselves if the placeholder is meant to ship. The `.claude/hooks/lockeys-submit-guard.sh` PreToolUse hook denies the command.
- Never `p4 obliterate` -- destroys history.
- Never `p4 revert //...` or wildcard-revert across an unrelated CL -- discards work indiscriminately. Revert specific files by name, or by CL via `p4 revert -c <cl>` when you mean it.
- `p4 revert -a` is safe and pre-authorized -- it reverts only files whose content matches the depot (no-op `p4 edit`s left behind by tools like `first_pass.py refresh`). It cannot lose work because matching files have no local content to lose.
- If asked to submit and you have not personally verified a successful build this session, ask the user to confirm they have compiled.
- Reference integrity: every reference in a file under `.claude/` must resolve to a file on disk. The validator is `.claude/scripts/check_claude_references.py` and the standard is `.claude/docs/reference-integrity.md`; run it yourself before submitting. It BLOCKS `p4 submit` only for people who have installed the spiritcrossing-gates plugin, which is the only thing that registers the hook -- no settings file in this repository does, so for everyone else the rule is a convention, not a gate.

### Auto Mode pre-authorization

Validation and publication happen at submit; CL setup is reversible via `p4 revert -c <cl>`. So in Auto Mode, CL-building operations are pre-authorized -- no per-action confirmation needed. The publication boundary is `p4 submit`; the Auto Mode "destructive / shared-state" gate fires there, not before.

**Pre-authorized in Auto Mode (pending workspace state only)**:
- `p4 edit`, `p4 add`, `p4 delete`, `p4 reconcile`, `p4 reopen`, `p4 revert <file>`, `p4 revert -a` (unchanged-only), `p4 shelve`, `p4 shelve -d`, `p4 unshelve`, `p4 resolve -am`.
- Creating a new CL (`p4 change -i`); editing an existing CL's description via the dump-edit-restore pattern in "Edit a pending CL's description safely".
- Reorganizing files across pending CLs with `p4 reopen -c`.

`p4 shelve` belongs here, not in the confirm list. Three-axiom check: reversible (`p4 shelve -d -c <CL>` removes it cleanly); blast radius is the same teammate-visibility surface that `p4 opened` already exposes for `p4 edit` (the shelf is *visible* via `p4 changes -s shelved`, but does not impose anything on teammates and does not change depot history); exposure is to the project's own Perforce server, which already holds the pending CL's metadata. The symmetry with the already-pre-authorized `p4 unshelve` is the simplest argument: both create teammate-visible-but-non-imposing pending state. The publication boundary is still `p4 submit`.

**Always requires explicit per-action confirmation, even in Auto Mode**:
- `p4 submit` -- the irreversible publication step.
- `p4 sync` -- overwrites local workspace files with depot content; it is not confined to pending CL state.
- The "Submit safety" never-list above (`p4 obliterate`, `p4 revert //...` or wildcard-revert, `p4 sync -f` over an active CL).

There are no exceptions to this anywhere in the project. A directory-scoped `CLAUDE.md` does NOT get to pre-authorize `p4 submit` for its own file scope; the earlier domain carve-out mechanism was retired because its binding half (a `Bash(p4 submit:*)` permission allow entry) never existed, leaving the documentation more permissive than the harness actually was. If per-scope auto-submit is wanted in future, it must be re-introduced together with the settings.json allow entry that actually binds it.

**Parallel rule for other shared systems.** Anything that *publishes* requires explicit confirmation; anything that only mutates local-workspace state is pre-authorized.
- Publishes (confirm): `git push` to a remote -- applies to plugin/skill development (e.g. SpryFox plugins on GitHub); this project's source is in Perforce, not git. `gh pr create` / `gh issue create` / `gh pr merge` / `gh pr review` and posting comments -- same scope. `./backend.sh deploy` to any non-local environment. Sending messages to Slack / email / external services. Uploading content to pastebins / gists / diagram renderers.
- Local-only (pre-authorized): the local git mirror (commit / branch / checkout / stash / rebase of unpublished commits / cherry-pick / local tags), `./backend.sh run_cohort`, edits to `tmp/` and `.local-data/`, anything inside the working tree that doesn't cross out to a remote.

### CL naming and content

Title format: `<Plan Title>: <description> [<content>, <purpose>]`

- **Content tags:** `C++`, `Config`, `Schema`, `C#`, `Mix`, `Skill`, `Docs` (only `Docs/ClaudeDocumentation/**`). The pattern -> tag lookup, including which paths count as `Tool`, is [cl-content-heuristics.md](.claude/docs/cl-content-heuristics.md).
- **Purpose tags:** `Release` (default, omit), `Test`, `Tool`, `Claude`.
- **C++ isolation:** C++ and Config in separate CLs for review. Schema (`.fbs`) and C# codegen / ConfigTool ride with C++. Test code rides with the code it tests. A feature should never need more than 2 CLs (C++ and Config); favor fewer when the split isn't justified by review burden.
- Tags required for CLs with 3+ files or a plan prefix; single-file CLs exempt.

### Description formatting

The title is the first line. The body explains *why* and lists any cross-CL dependencies.

- **Keep it short.** One line per change, or one short paragraph per area of the CL. A reviewer reads a description to decide what to look at, not to have the change explained to them -- the diff does that, and the code comments carry the reasoning. Cut: the argument for why the change was needed, the failure it prevents, what was measured, how the rule is enforced, per-file inventories, and counts of things changed. Those belong in the code, the doc, or a `.notes.md`, all of which outlive the CL. A description that runs past roughly ten lines is describing rather than naming.
- **No manual line wrapping.** Body paragraphs are single long lines (P4 and Swarm soft-wrap). Hard-wrapping at 65/72 chars is WRONG for this project -- it forces readers to mentally un-wrap.
- Blank lines separate paragraphs; bullets use `- ` and each bullet is one long line.
- In a `p4 change` spec file, every description line -- including blank lines between paragraphs -- starts with a leading tab.

### Edit a pending CL's description safely

Footgun: `p4 change -i` with a hand-written spec containing `Description:` but no `Files:` section silently moves all files in the CL back to default. No error.

Safe pattern -- always dump first, edit in place, write back:

```bash
p4 change -o <cl>                  # full spec including Files: section, straight to tool output
# Write the spec to tmp/cl-form.txt with the Description block edited (leave Files: alone)
powershell -NoProfile -Command "Get-Content tmp\cl-form.txt | p4 change -i"   # Files: preserved
```

**Why no `>` or `<`.** The obvious form (`p4 change -o <cl> > /tmp/form` ... `p4 change -i < /tmp/form`) works interactively but is unusable in any unattended context. Shell redirection is flagged by the Bash safety checker, which runs *before* and independently of `permissions.allow` -- so the command raises an approval dialog no allow entry can suppress, and a scheduled or background run blocks on it forever with no visible error. Capturing `p4 change -o` from tool output and piping via `Get-Content` inside the quoted PowerShell string avoids both operators: bash never parses the `|`, and the whole command matches the existing `Bash(powershell*)` allow entry. Same reasoning applies to `2>&1 >`, heredocs, and `/tmp` (use `tmp/`, per "Using `tmp/`").

For a fresh CL on creation: put the full description in the initial `p4 change -i` input and either include `Files:` or omit `Files:` (nothing to lose) and use `p4 reopen -c <new_cl> <paths>` after.

Inverse footgun, same severity: **never one-line `p4 change -o | ... | p4 change -i` to create a "new" CL.** The dumped spec's `Files:` section lists every file open in the default changelist, so piping it back creates the CL WITH all of those files swept into it ("Change N created with 10 open file(s)" is the tell -- a clean empty CL prints just "Change N created."). To create an empty CL, Write a minimal spec file (Change/Client/User/Status/Description, NO `Files:` section) and run `powershell -NoProfile -Command "Get-Content <specfile> | p4 change -i"` as its own command. Both this shape and wildcard `p4 reopen` are denied by the `.claude/hooks/p4-cl-guard.sh` PreToolUse hook.

### Unshelve and resolve

Workflow to unshelve a CL and merge with head:
1. `p4 unshelve -s <CL>`
2. `p4 sync <file>` per affected file (the "must resolve" message is expected when the shelf is behind head).
3. `p4 resolve -am <file>` -- `0 conflicting` is a clean merge; any conflicting chunks: stop, report, do NOT `-at` / `-ay` without explicit approval (they discard one side).

If the shelf was from current head, step 2 prints "is opened and not being changed" and step 3 prints "no file(s) to resolve" -- files are already current.

