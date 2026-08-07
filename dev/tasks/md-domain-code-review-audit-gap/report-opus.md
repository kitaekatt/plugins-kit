# Assessment A -- Claude Opus (Agent tool, high effort)

Independent assessment of whether flecs-ecs has sufficient CLAUDE.md coverage to inform
CODE REVIEW. Ran against flecs-ecs HEAD `d2073ee` (branch `md-audit/skills-compliance`),
2026-08-07, immediately after the 7-commit md-domain audit landed.

Brief given: assess MISSING / EXCESSIVE / INCORRECT for every CLAUDE.md with code files
under it, plus whether ancestors genuinely cover the code dirs that have none. Told to use
`skills-kit:md-domain`'s standards as a lens but NOT to run the audit lane pipeline -- a
reviewer's judgment was wanted, not a second lane run. Told to make no edits.

---

All key claims verified. Here is my assessment.

---

# CLAUDE.md coverage for code review -- flecs-ecs

Verified against HEAD `d2073ee`. I opened source for every finding below; claims I could not confirm are excluded.

The audit that just landed did its job on *anchors* -- I resolved ~40 cited paths/symbols/constants across the engine, REST, client and tools docs and found essentially no rot. `PREFIX_MATCH` (router.c:20), `main.c:103`, `simulation.c:137-145`, `components.c:659-660`, `TARGET_TURN_MS=375`, the wasm-rt `TIMELINE`, `wasm_3x` = 22/83, `faction-matrix-sashimi.yaml`, `test_lean.c` -- all correct. **The remaining problem is not staleness. It is allocation: the highest-risk surfaces in this repo have no ambient coverage at all, while a lot of ambient budget goes to port journals.**

## 1. engine/tests -- MISSING, no CLAUDE.md. Running the test suite deletes tracked files.

`engine/tests/test_rest.sh:47-48` does `rm -rf ./sandboxes/clean` from the project root. `git ls-files sandboxes/clean` returns two tracked files. **This session's own `git status` shows exactly `D sandboxes/clean/entities/mage.yaml` and `D sandboxes/clean/systems/lua/move_right.yaml`** -- the dirty tree a reviewer is looking at right now *is* a test side effect, and nothing anywhere says so. A reviewer will read it as an intentional deletion and approve it.

Also homeless, all verified:
- `test.h:12-21` prints `PASS` **unconditionally** after the test body; `ASSERT` (`:23-30`) prints `FAIL` and `return`s, then the wrapper prints `PASS` for the same test. Counts and exit code are correct (`_test_pass--` compensates), the console is not.
- No fixture mechanism: `ASSERT` returns from the body, skipping any manual `teardown()`, leaking the engine and leaving the file-scope `static Engine *engine` live for the next test.
- `test_app.c` / `test_templates.c` write `flecs_test_app_templates/` and `flecs_test_templates/` **relative** under `_WIN32` with ctest's `WORKING_DIRECTORY ${CMAKE_SOURCE_DIR}`; `git check-ignore` confirms neither is ignored. Both are untracked in the tree today.

**This directory warrants its own CLAUDE.md. It is the strongest single finding.**

## 2. Root CLAUDE.md -- INCORRECT: ctest registrations are conditional and vanish silently

Root claims ctest "additionally registers Node and Python suites (`test_rt_client_lib`, `test_serve_client`)". Every one of those is inside a `find_program` guard (`CMakeLists.txt:387, 447, 474`). On **this configured build**: `PYTHON3_FOR_TOOL_TESTS:FILEPATH=PYTHON3_FOR_TOOL_TESTS-NOTFOUND` -- `test_serve_client` is not registered. `python3` does not resolve on this box; the guard demands literally `python3`, not `python` (which the mb guard accepts and finds). `ctest` passes green having never run it. The `ctest -N is authoritative` line mitigates but does not say **green != ran**.

## 3. sashimi -- MISSING: the two most likely silent defects are undocumented

985 lines across the pair, and `grep -c "S_MAX"` returns **0** in both.

- **`systems.c:1153`: `static CVec2 gems[S_MAX_ENTS], hearts[64];`** with a silent clamp at `:1170` (`else if (pk->effect == 2 && n_hearts < 64)`). Past 64 hearts the hero AI stops seeing them -- deterministically, no error. An undocumented magic 64 inside a 4096-cap function.
- **`bridge_named_config` (`systems.c:836-880`) is a fixed-indentation text scanner over a *generated* file.** `snprintf(needle, "  %s:", ...)` and `sscanf(line, "    damage_taken_ratio: %f", &v)` -- exactly two and four spaces. `app.yaml` is emitted by `tools/generate_content.py`. A cosmetic indentation change in the generator silently reverts the difficulty ratio to default. `fopen` failure is a bare `return` (`:844`).

Both docs describe this bridge benignly as "re-parses `app.yaml`'s `configs:` block."

**INCORRECT:** `sashimi/CLAUDE.md:7` says "26 systems"; `systems.c:4264-4288` registers 25. `clients/CLAUDE.md:190` says 71 m4a clips; there are 72.

## 4. avk -- MISSING: decision #17 is anchored nowhere near the code, and understates the hazard

Root decision #17 exists; `grep 65536` in `sandboxes/avk/CLAUDE.md`, `sandboxes/CLAUDE.md`, `HIERARCHY.md` returns nothing. Meanwhile `sandboxes/avk/systems/native/systems.c` has **39 bare `65536` literals with no `#define`**. Largest config is 9583+38360 = **47,943 units against 65,536 (73% consumed)**; overflow is `&& count < 65536` truncation -- silently dropped units.

Two couplings #17 does not describe:
- It says "**stack** arrays"; 38 of 39 are `static`. The one true stack array is `ecs_entity_t to_clear[65536]` (`systems.c:305`) -- **512 KB on the 1 MB stack** pinned at `CMakeLists.txt:21` (`CIVETWEB_THREAD_STACK_SIZE 1048576`) and the WASM `STACK_SIZE=1048576`. Half the stack in one declaration.
- A **separate grid-dimension cap**, `GRID_MAX_X 1152` / `GRID_MAX_Y 768` (`:272-273`), unrelated to unit count. `occ_set` (`:282`) silently skips out-of-range writes. Largest configured map is 1088x612 -- 94% of the cap. A reviewer adding a bigger map config gets a silently broken occupancy grid.

Contrast: **monkey-baiting does this correctly** (CLAUDE.md:196-202 -- `MB_MAX_MOVES`/`MB_HAZARD_CAP`/`MB_MAX_CHARS` "fails loud", re-checked by `test_mb_sim`). That is the model; avk and sashimi should follow it.

## 5. engine/src/engine -- MISSING: the two largest files have zero review notes

The 124-line file covers simulation, reset, `on_state_change`, determinism, spatial/faction. It says nothing about `templates.c` (**1398 lines**, the largest) or `systems.c` (944, the Lua bridge). Verified hazards there:

- **`systems.c:760-772`: a Lua system's query silently drops unregistered component names.** `if (comp && ecs_is_alive(...))` -- else the token is discarded with no error. A typo'd query (`"Position, Velcoity"`) runs over `Position`-only entities and reports success. Only if *all* names fail does it error.
- `systems.c:796`: `ecs_entity_t matched[1024]` -- Lua systems silently process at most 1024 entities/turn. This is a hard cap that decision #17 (framed as "native systems") does not reach.
- `query_comps[32]` exactly equals `FLECS_TERM_COUNT_MAX` (32) -- zero headroom; raising one without the other overflows `qdesc.terms[]`.
- `templates.c:530-538`: `TMPL_VALUES_MAX 1024` truncates via `snprintf` with **no error**, unlike the component/child count caps at `:630`/`:646` which do print.

The "Component Access Patterns" code fence (lines 106-124) is the weakest content in the file -- a restatement of the Flecs API. Only its closing sentence (`ecs_modified_id` skips change notification) earns its place.

## 6. tools/mb_loop -- MISSING: 11.8k lines, 15 lines of glossary

The mb_loop prose that exists is genuinely excellent (byte-identity export contract, the fail-closed `model_sha AND solver_version AND solver_backend` gate, the `fq` refusal -- I verified `model_export.py:1188-1192` raises rather than warns). But the determinism couplings that a diff will actually break are absent. Verified:

- **`archive.py:491-494` fails OPEN**: `if self.key is None or entry.get("comparability") is None: return True` -- an older archive is silently ranked across `sim_version`s. CLAUDE.md:1190 asserts "Comparability is never assumed."
- **`stage.py:45`: `SANDBOX_FILES = ("app.yaml",)`** directly under a docstring claiming it carries "everything mb_sim's rules loader reads, plus the loop's own authored inputs." If anything else is ever read, staging silently scores committed data -- the exact failure the staging seam exists to prevent.
- `emit.py:555`: the RNG stream key is `f"emit:{target}:{n:04d}"` keyed on candidates *accepted so far*, so any verifier change re-keys every downstream stream -- while CLAUDE.md presents emission as seed-determined.
- `model_export.py:196` `_js_json` uses `JSON.stringify(obj, null, 1)` with **unsorted keys**, so dict insertion order is hash input; reordering a key for readability invalidates every archived gate decision.

## 7. web/ tree -- MISSING: the documented symlink topology is broken in this checkout

`web/matrix/CLAUDE.md:117-121` documents `web/lib`, `web/matrix/lib`, `web/matrix/config` as symlinks. `git ls-files -s` confirms mode `120000`. On disk here they are **21-, 34- and 29-byte regular files containing the path text** -- Git Bash on Windows degraded them. `tools/CLAUDE.md:21` documents exactly this hazard for `localhost.sh`, but nothing carries it for the `web/` tree, where it silently breaks the REST client's engine-runtime import.

## 8. EXCESSIVE -- where the ambient budget goes

- **`sandboxes/monkey-baiting/CLAUDE.md:17-65` "Port status" -- 49 lines**, eight `**Stage N (done)**` paragraphs. Every one says "(done)". Zero decision-relevant information for a reviewer; pure project history.
- **`sashimi/CLAUDE.md:232-392` "Port decisions" -- 161 lines (27% of the file)**, much of it framing what each rule *superseded* ("not a decision anymore", "retiring the..."). Eagle FeatherRecall alone is 39 lines.
- **`sashimi/CLAUDE.md:503-590` "Tests" -- 88 lines** re-enumerating the `RUN()` cases in `test_systems.c` in prose; and `:592-605` carries a difficulty number the text itself declares invalid ("needs re-measuring").
- **`sashimi/clients/CLAUDE.md:31-145` -- 115 of 380 lines (30%) on the leaderboard + auth**, a feature the same file says ships "off/empty... zero network calls," already pinned by two Node suites -- while `sashimi-client.js` (**1193 lines**, the largest module, home to both narrated regressions) gets 5 lines.
- `engine/clients/lib/CLAUDE.md:97-100`: the removed-`gpuHint` parenthetical is changelog.

The monkey-baiting concentration **does work** where it matters -- `systems/native` (the four non-negotiable rules, the `mb_step.c`-included-verbatim-under-nvcc coupling, the explicit "avk caches ids in file statics; that would be raced here" contrast) is the best code-review content in the repo. The 1414 lines are not the problem; the 49-line status preamble sitting above it is.

## Bottom line

**No -- coverage is not sufficient for code review.** It is accurate and well-anchored, but it is written as *project history* where it should be written as a *defect-catching brief*. A reviewer armed with all of it would still approve a change that deletes tracked fixtures, silently truncates at 64 hearts, breaks a difficulty parser by re-indenting a generator, or overflows an occupancy grid.

The five changes with the highest return:

1. **Add `engine/tests/CLAUDE.md`** -- the tracked-file deletion in `test_rest.sh`, the FAIL-then-PASS output, `ASSERT`-skips-teardown, the unignored Windows temp dirs, and the `find_program` gating that makes green != ran.
2. **Anchor buffer caps next to the code they govern** -- the `65536`/`GRID_MAX_X/Y`/`to_clear`-vs-1MB-stack trio in `sandboxes/avk/CLAUDE.md`, and `S_MAX_*` + `hearts[64]` in sashimi. Copy monkey-baiting's fail-loud framing.
3. **Add ~15 lines to `engine/src/engine/CLAUDE.md`** on the Lua bridge's silent query-token drop and the `matched[1024]` / `TMPL_VALUES_MAX` truncations -- and delete the Flecs API code fence to pay for it.
4. **Document `bridge_named_config`'s indentation coupling to a generated file** in `sashimi/CLAUDE.md`; add the ~40 lines of determinism couplings (`_comparable` fail-open, `SANDBOX_FILES`, unsorted-key hashing) to the mb_loop section.
5. **Cut the port journals** -- monkey-baiting's 49-line "Port status", sashimi's 161-line "Port decisions" framing, its 88-line test enumeration and dead difficulty number, and the 115 lines on the disabled leaderboard. That is ~250 lines out against ~70 in: a net reduction with a materially higher catch rate.

Two smaller items worth folding into existing files rather than new ones: `native_registry_register` (`engine/src/native_registry.c:29-34`) **silently no-ops on an unknown sandbox name** -- a decision-#12 violation in the engine's own code, and the failure mode for adding a sandbox and forgetting the registry entry; and `tools/CLAUDE.md` should note that `lib/state.py`'s `fetch_units` is hardcoded to avk's `Archer_`/`Knight_` names and returns an empty list (not an error) for any other sandbox. I did **not** find `engine/src/` itself to warrant a CLAUDE.md -- `main.c:103`, `document_root`, and the two-place native-system registration are all already correctly anchored elsewhere.
