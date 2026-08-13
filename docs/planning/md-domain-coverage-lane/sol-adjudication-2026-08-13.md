# Sol adjudication of 13 generated documents -- 2026-08-13

WHAT THIS IS. An independent keep/revert decision over the 13 CLAUDE.md files
produced by two skills-kit 0.54.0 generation runs against `woodworking-sim`.
The repository owner delegated the call to gpt-5.6-sol ("have sol decide"), so
the verdicts below were ACTED ON, not weighed.

RESULT: 3 KEEP, 10 REVERT, with 10 verified false claims.

WHY IT MATTERS MORE THAN THE RUNS THAT PRODUCED IT. The 0.54.0 fixes were about
ACCOUNTING -- every candidate reaching a terminal disposition, and a null branch
keeping its verified hoists. Both worked (41/41 dispositions, 0 incomplete;
`godot/assets` created from a verified hoist). This document is the separate and
harder question the accounting cannot answer: whether what gets WRITTEN is worth
having. On this batch, mostly not.

THE DOMINANT FAILURE IS NOT OMISSION, IT IS CONFIDENT FALSEHOOD. Seven documents
carry claims that are wrong about the code they constrain -- a class name that
does not exist, a universal that a live caller already violates, a memory model
contradicted by the library's own dispose handler. Three more are longer than the
reviewed baselines they replace without adding anything a bounded reviewer could
act on. Note that several falsehoods were caught only by EXECUTION (a Node
import, a headless Godot import scan, re-running the sampling at other
densities), not by reading -- desk validation would have passed them.

This is direct evidence for the standing item
`generation-carries-unverified-rules` and for edit 3 of the consumer-neutral
criteria amendments (verify a restated rule against the code it now constrains).

Verbatim decision follows.

---

.claude/CLAUDE.md: REVERT
why: It corrects the baseline's misleading nested-`check` example, but replaces it with authoritative implementation details that are themselves false: `_ToolCtx` does not exist, and the claimed exhaustive per-OS field list omits `command`. That makes the rewrite unsafe despite its useful core correction.
checked: Compared both sides of the diff; read `.claude/bootstrap.json`, bootstrap 0.82.0/0.82.1 `engine.py` and `tool_check.py`, and the SessionStart hook.

kernel/tests/CLAUDE.md: REVERT
why: The committed version already carries the dual-runner parity duty, paintMask index-space hazard, perf-gate caveat, and cascading-failure behavior. The rewrite mainly adds source references, numeric restatement, and root-level orientation, so it is longer without materially improving what a reviewer can catch.
checked: Verified the two CMake test registrations and CI jobs, all nine golden files, five `maskCoverage` steps plus one `maskOccluded` step and no `occluded` argument, both runners' control flow, and the 1 ms/5 ms gates.

src/bench/CLAUDE.md: KEEP
why: It adds reviewer-useful facts the baseline lacked: `MetricsPanel` drops `perAxisStatus`, assembly/containment bands are unreachable here, and the indexed-overlay mismatch is latent rather than present for today's fixtures. Those additions catch a stale diagnostic while rejecting a false alarm about the current fixture roster.
checked: Read all four bench modules, `score.js`, `thresholds.js`, both `TEST_PIECES` consumers, and every geometry builder's `finalize()` path; confirmed the eight slider leaves and non-indexed current fixtures.

src/cuts/CLAUDE.md: KEEP
why: The rewrite adds a substantive uncovered boundary: two of six wrappers have no test importer, and it extends the attribute-loss warning to `concatGeometries`. Those are true cross-file review checks not present in the committed document.
checked: Traced imports from every npm-test runner and the fixture exporter, inspected all six modules plus `kernelAdapter.js`, and followed `isCutFace` through `Board.fromGeometry` and `AssemblyTool`.

src/effects/CLAUDE.md: REVERT
why: Although the geometry-level `boundingSphere` correction improves the baseline, the new section falsely makes the bare `-1` a universal shop-table plane. `LatheTool` uses the same `SawdustSystem` while moving its ground to `-maxRadius - 0.3`, so the asserted coupling is already untrue for a live caller.
checked: Read `sawdust.js`, every `SawdustSystem` caller, the three saw table constants, `LatheTool`'s framing code, and Three 0.182 frustum, sphere, raycast, and disposal implementations.

src/physics/CLAUDE.md: REVERT
why: The generated text carries the same three useful facts as the committed version--the dead resolution loop, the GDScript twin, and the precise test boundary--but mostly reorders and expands them. It is not a net improvement over the shorter reviewed baseline.
checked: Verified repo-wide importers, the two eight-iteration/0.15 loops, all four live `boardToHull` consumers, `board_hull`'s missing cache/dedup, and the direct kernel imports in the golden tools.

src/sim/CLAUDE.md: KEEP
why: It adds concrete hidden drift evidence--the stale 1.5/1.4 fixture factors, conflicting 16 cm/20 cm rationale, and the separate `miterSaw-blade-01/02/03` family--without weakening the baseline rules. Those facts materially improve review of otherwise-green cross-client changes.
checked: Verified all six exports, every golden input and replay, the seven non-null upgrade IDs in all four registries, absence of tests owning those IDs, and the roadmap/blade-family references.

test/fixtures/CLAUDE.md: REVERT
why: The strong frozen-artifact guidance from the baseline remains, but the added sections introduce false universals about sampling density, threshold descriptions, and Node-only code. The rewrite is therefore worse than the committed document.
checked: Traced all builder consumers and frozen artifacts; ran the actual THREE cylinder sampling at alternate ring/bin densities; inspected all 12 descriptions, package ESM mode, every npm-test runner, and CI.

tools/CLAUDE.md: REVERT
why: The committed version already states the complete sidecar-shadowing hazard, its loader precedence, and the safe alternative. Growing it from 36 to 53 lines with counts, citations, and a longer bake explanation does not add a new issue a bounded reviewer could catch.
checked: Read the script, both eager loaders, serializer/save path, and both diff versions; parsed all 22 authored plans, confirmed zero `thumbnail` properties and 22 matching sidecars, and searched all script references.

bots/lib/CLAUDE.md: REVERT
why: It contains useful auth and purity couplings, but two categorical foundations are false: the browser API client can load under Node, and losing bot authorization does not make every bot submission return 429. A new ambient document cannot safely carry those claims.
checked: Traced all five modules' importers, successfully imported `src/api/apiClient.js` with Node, and read bot identity/header code plus `isAuthorizedBot`, `submit-plan`, bot defaults, purity scope, and tests.

godot/assets/CLAUDE.md: REVERT
why: This mostly restates a sibling-file pattern visible in the directory and duplicates the manual child's sidecar rule. Its sole consequence is also false: absent committed `.import` metadata loses pinned settings, but Godot can regenerate the sidecar and load the source rather than silently yielding nothing.
checked: Counted 18 PNGs, 4 OGGs, and 22 sidecars; inspected `.gitignore`, both child documents, and resource-loading code; in an isolated project copied only a PNG and observed headless Godot generate its `.png.import` file.

src/debug/CLAUDE.md: REVERT
why: The import-side-effect warning is valuable, but the retention section falsely says a retained geometry keeps its WebGL resources alive past `dispose()`. Three's disposal listener releases attribute buffers and binding state even while JavaScript still references the geometry, so the document teaches the wrong memory model.
checked: Read all of `diagLog.js`, every importer and `diag()` call site, and Three's `WebGLGeometries.onGeometryDispose` implementation.

src/devtools/CLAUDE.md: REVERT
why: The all-key write loop and separate renderer are useful facts, but the document turns "rewrites every file" into the false claim that a one-finish refresh produces a diff of every thumbnail. Git only reports outputs whose bytes changed, so the main review warning overstates the observable blast radius.
checked: Read `finishThumbs.js`, `finish-thumbs.html`, the Vite write endpoint, `finishSample.js`, and the in-app preview path; confirmed 22 preset keys, 22 matching tracked PNGs, and the sole code importer.

OVERALL: Only three of the 13 documents clear the bar, so this batch should not be accepted. Seven documents contain verified false claims, and three more rewrites add length without improving their reviewed baselines. The strongest document is `src/bench/CLAUDE.md`; the weakest is `godot/assets/CLAUDE.md`, a one-paragraph duplicate whose only asserted failure consequence is false.

FALSE CLAIMS FOUND:

- `.claude/CLAUDE.md`: `bootstrap_lib/engine.py::_ToolCtx.__init__` does not exist; the class is `_ToolEntryCtx`. Its "reads only" list for `install.<os>` also omits the supported `command` field.
- `src/effects/CLAUDE.md`: the particle clamp's `-1` is not always the shop-table plane. `LatheTool` is a live `SawdustSystem` consumer and moves its ground plane to `-maxRadius - 0.3` while the particles remain clamped at `-1`.
- `test/fixtures/CLAUDE.md`: raising profile resolution or lowering ring density does not inherently put bins on the `0.1` floor. Actual 30 cm cylinder trials at 2.5 rings/cm with 2 bins/cm, and at 3 rings/cm with 2.5 bins/cm, left zero bins empty.
- `test/fixtures/CLAUDE.md`: not "each" `TEST_PIECES` description restates the four threshold numbers; several descriptions contain none of them.
- `test/fixtures/CLAUDE.md`: a top-level `require` would not leave npm tests green. The package is ESM, both modules are imported by Node runners, and `require` is undefined during ESM evaluation.
- `bots/lib/CLAUDE.md`: `src/api/apiClient.js` does not "cannot load under Node"; a direct Node import succeeds because `playerIdentity.js` touches `localStorage` only inside guarded functions.
- `bots/lib/CLAUDE.md`: losing the bot prefix/secret does not make "every bot submit" return 429. The first unauthorized submission is accepted and stamps the cooldown; only another within 20 seconds is throttled.
- `godot/assets/CLAUDE.md`: adding a source asset without its committed `.import` sidecar does not inherently make the consumer get nothing. Godot's import scan regenerated the missing sidecar from the source in an isolated check.
- `src/debug/CLAUDE.md`: retaining a JavaScript reference to a disposed geometry does not retain its WebGL buffers past `dispose()`; Three's dispose handler removes its index/attribute buffers and releases binding state.
- `src/devtools/CLAUDE.md`: refreshing one finish does not necessarily "arrive as a diff of every thumbnail." The code overwrites all 22 paths, but unchanged bytes do not enter a Git diff.
