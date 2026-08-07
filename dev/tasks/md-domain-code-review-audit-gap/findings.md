# Findings: why a clean md-domain audit coexisted with undocumented hazards

Status: triage COMPLETE, causal analysis COMPLETE, proposal PENDING USER REVIEW.
Evidence: four verification units run 2026-08-07 against flecs-ecs @
`md-audit/skills-compliance` (read-only) and against
`plugins/skills-kit/skills/md-domain/`.

## 1. Triage result

| Report | VALID | PARTIAL | REJECTED |
|---|---|---|---|
| `report-opus.md` | 20 | 4 (#4, #6b, #8c) | 1 (#6e) |
| `report-sol.md` | 9 | 3 (#4, #6, #9) | 0 |

Rejected: report-opus #6e ("15 lines of glossary") is contradicted by its own
body; the mb_loop balance-loop section runs ~180 lines and the same report calls
it excellent. The determinism findings 6a-6d in that section stand.

Trimmed: report-sol #9's "two formats" half is a stretch (REST errors are
conventionally format-exempt); the `tools/lib/api.py:28-33` YAML-subset coupling
underneath it is solid.

Both reports reached the same verdict from disjoint samples. Report A never
opened `engine/src/rest/`, `avk/clients/wasm/`, `avk/clients/console/` or
`matrix/`; report B never examined `test_rest.sh`. Neither omission was a
methodology failure -- both briefs covered the missed ground. Two thorough
reviewers sampling independently found largely different hazards, which is
itself evidence about coverage guarantees.

## 2. Six named mechanisms

Each valid deficiency traces to one of six mechanisms in md-domain. All six are
cited to source; none is "the audit should look harder".

### M1 -- Absent content is invisible by construction

`references/standards/claude-md-standards.md:400` states the CD dimension is
"a **validator over existing claims, not a gotcha crawler** -- it does not scan
the directory for *new* gotchas to add (that is the authoring direction; doing it
here would be non-idempotent and expensive)". The operative instruction reaches
the detect agent at `workflow/claude-md-detect.js:203`: "Validate existing claims
only -- do NOT crawl the directory for new gotchas (non-idempotent)."

Of ~40 rules that can fire on a CLAUDE.md, four can fire on silence: H-1/H-2/H-3
(root file missing identity / commands / directory map) and A-4 (known error
gated behind a skill trigger). All four concern the doc's self-description or a
fact's location. None asks whether a directory contains a hazard the doc omits.
CD-6 is the near miss: it flags a file eroded to structural description, i.e.
regression from a prior state. A file that was always thin passes it silently
(`claude-md-standards.md:426`).

Accounts for the majority of valid findings: `test_rest.sh` fixture deletion,
`test.h` unconditional PASS, ASSERT skipping teardown, `hearts[64]` clamp, the
Lua silent component drop, `matched[1024]` truncation, the 39 bare `65536`
literals, the 127-coordinate WebSocket ceiling, the WASM 65,536 truncation,
`took_damage` REST parity, `api.py` coupling, `state.py` hardcoded prefixes,
`emit.py` RNG re-keying, `model_export.py` unsorted-key hashing, and the
degraded Windows symlinks.

### M2 -- The classic/code-directory split withholds code from most of the corpus

Source is read only for `dimension: code-directory` files. Any file carrying a
`claude_md:` block -- including every root project CLAUDE.md -- is `classic`, and
`workflow/claude-md-detect.js:204` instructs: "run the classic
CCP/CRP/ADP/Hygiene/Schema criteria only; do NOT load or apply the code-directory
insight filter." So on classic files the audit never opens source even to
fact-check.

Accounts for: root CLAUDE.md's `find_program`-guarded ctest suites that silently
vanish, and its literal `python3` that misses `python`.

### M3 -- Nothing audits ambient coverage

No rule, script or prompt walks outward from a source file to ask whether an
ancestor CLAUDE.md covers it. The load-graph machinery in
`references/cohesion-principles.md:47-60` governs md-to-md placement only. H-11's
ancestor walk (`audit-lane.md:88-94`) checks the subject doc against ancestors'
declared conventions -- doc-vs-doc.

Consequence: a fact can be accurate, resolvable, and useless. Verified cases --
`engine/src/{main.c,native_registry.c,platform.h}` have only the root file as an
ancestor, while the facts about them sit in `engine/src/engine/CLAUDE.md:69`,
`engine/src/rest/CLAUDE.md:44` and `sandboxes/AvKrt/CLAUDE.md:18`, none ambient;
`platform.h` has zero mentions corpus-wide. The native dual-maintenance step is at
`sandboxes/HIERARCHY.md:133`, a sibling tree. The Windows symlink-degradation
hazard is documented at `tools/CLAUDE.md:21` but not ambient for `web/matrix/`.

This is the axis the two reports split on (disagreement 1). A asked "does this
fact exist somewhere"; B asked "is it ambient here". B was right on the question
that decides reviewer outcomes, and md-domain has no rule for it.

The converse guard matters too: `FLECS_SEED` at `sandboxes/avk/CLAUDE.md:40` is
NOT ambient for `engine/tests/` and does not appear there -- so a naive "copy the
fact closer" fix would have imported an inapplicable contract. Ambience is
directional, not an excuse to duplicate.

### M4 -- CD-4's "true in kind" excludes magnitude, and stale comments defeat it

`claude-md-standards.md:427` asks "Read the anchored code; is the claim still true
**in kind**?" A count that is wrong is still true in kind. Count-shaped claims are
therefore structurally under-checked -- and they are exactly what a reviewer
leans on.

Verified drift that SURVIVED the audit in a file it read and edited:
`sandboxes/monkey-baiting/CLAUDE.md:149` says "the eleven native systems" against
19 actual registrations (12 match-phase at `mb_engine.c:432-443`, 7 campaign via
line 314); `:1399` says `test_mb_wasm` has 13 tests against 16 `TEST(...)`
definitions all driven by `RUN(...)`. Also `sashimi/CLAUDE.md` "26 systems" vs 25,
and `clients/CLAUDE.md` "71 clips" vs 72.

The sharpest detail in the whole investigation: the stale figure "eleven" is
echoed in the source's own comment at `mb_engine.c:429`. A fact-check performed
against the nearest human-readable text CONFIRMS the doc. Nothing requires the
check to be performed against executable content.

### M5 -- An ambient doc can state an invariant that the code violates, and both pass

H-11 checks a subject doc against an ancestor's declared convention (doc-vs-doc).
No rule checks CODE against an invariant the corpus states.

Verified: root `CLAUDE.md:120` decision #12 "No silent fallbacks", violated by
`native_registry_register` silently no-opping on an unknown sandbox name
(`engine/src/native_registry.c:27-33`). And
`sandboxes/monkey-baiting/CLAUDE.md:1190` "Comparability is never assumed",
contradicted by `archive.py:491-494` failing OPEN on missing comparability.

In both, the documentation is right and the code is wrong -- a class the audit
cannot represent at all, because its subject is the doc.

### M6 -- The value filter is silenced on exactly the content that bloats

CD-5 exists and should have flagged the port journals. It did not, because
`claude-md-standards.md:389-394` carves out accepted structural patterns
including historical records, and `claude-md-standards.md:555` assigns SILENT to
"a validator detection artifact or an accepted structural pattern (historical
record ...)". Port journals ARE historical records.

Separately, the density lens that would flag verbose classic files is opt-in and
never ran: `claude-md-standards.md:458` ("Never runs by default") and `:462`
("The lens never emits FAIL ... A density-only audit is always COMPLIANT").

Accounts for both reports' independent condemnation of the port journals
(49 lines of "(done)" stages; 161/605 lines = 27%; 115/380 = 30% on a disabled
leaderboard).

## 3. The conceptual gap, in one paragraph

md-domain audits the documentation as a self-contained artifact -- its schema,
its internal cohesion, and the truth of the assertions it happens to make -- but
never audits the RELATION between the corpus and the code it exists to serve.
Every criterion takes the doc's own content as the universe of discourse:
present claims can be falsified, absent claims cannot be represented at all,
placement is judged by md-to-md cohesion rather than by whether a fact reaches
the reader of the file it describes, fact-checks are satisfied by the nearest
prose rather than by behaviour, and an invariant the corpus asserts is never
tested against the code that must honour it. Under that framing COMPLIANT means
"internally coherent and locally accurate", which is orthogonal to -- not weaker
than -- fit for review. The audit was not wrong; it answered a different question
from the one its verdict is read as answering.

## 4. Note on verdict semantics

`claude-md-standards.md:52` defines COMPLIANT as the absence of FAIL findings.
Nothing anywhere asks an outcome-level question. Notably the CD-5 value filter
already contains the right sentence -- "would a code-review agent catch something
a senior teammate catches and a generic reviewer misses" -- but it is a
content-selection heuristic for judging sections already present, never a gate.
The question exists in the skill and points backwards.

Precedent worth reusing: review mode's DIFF-CLEAN is explicitly documented at
`audit-lane.md:347-349` as "a weaker and more honest claim: *this change
introduced no failure*, not *this file is clean*". The same honesty is available
to COMPLIANT.
