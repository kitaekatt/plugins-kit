**Verdict: SERIOUSLY FLAWED.** The central observation—that normal detection cannot discover an undocumented hazard—is correct. But several causal claims are demonstrably wrong, the proposed “mechanical” predicates are semantic judgments, and the test is an overfit development set rather than evidence that a published cross-language change is safe.

**Per-axis findings**

1. **FAILS — diagnosis.** M1 survives: both the standard and operative prompt explicitly forbid discovering new gotchas (`plugins/skills-kit/skills/md-domain/references/standards/claude-md-standards.md:398-400`; `plugins/skills-kit/skills/md-domain/workflow/claude-md-detect.js:202-204`). The rest is less sound:

   - M2 is wrong for the evidence corpus. The current discoverer forces `classic` only for a schema block or skill directory and otherwise recognizes code-directory files mechanically (`plugins/skills-kit/skills/md-domain/scripts/discover_claude_md.py:86-124`); a read-only run classifies flecs-ecs’s root CLAUDE.md as `code-directory`. Even genuinely classic files are not forbidden from reading source: A-3 requires checking flags and class names against tooling/repo state, and taxonomy P explicitly covers numeric claims contradicted by the repo (`claude-md-standards.md:219-227`, `:548`). “Do not load the CD filter” is not “never open source” (`claude-md-detect.js:202-204`). Those misses are criterion-application failures, not a classic/code-directory blind spot.
   - M3 is only partly absent. There is no inverse source→ambient-doc walk, but C-5/R-1 and the placement framework already require a present fact to live with its change driver and reader set (`claude-md-standards.md:108-152`; `cohesion-principles.md:201-231`). The proposal should explain why those rules failed on explicitly misplaced facts. Its ambience check also cannot detect `platform.h`, which the evidence says has zero mentions (`dev/tasks/md-domain-code-review-audit-gap/findings.md:80-86`); that is M1, not M3.
   - M4 is real only for CD claims: counted magnitudes are deliberately fuzzy there (`claude-md-standards.md:366-370`, `:420-422`), while exact classic counts already have P. The stale source comment exposes weak evidence selection, not merely a missing counting rule.
   - M5’s “unrepresentable” claim is false. H/H2 already emit SERIOUS findings whose fix is in code/repo, not the CLAUDE.md (`claude-md-standards.md:551-552`; `claude-md-detect.js:240-243`). What is missing is a generalized invariant-vs-code detection rule and a bounded subject—not representational capacity.
   - M6 correctly identifies a real SILENT carve-out (`claude-md-standards.md:555`), but it does not establish that removing the carve-out globally is safe.

   The broad completeness gap survives; the six-mechanism attribution does not. Several proposal citations are also stale: the DIFF-CLEAN wording is at `audit-lane.md:368-370`, not `:347-349`, and “true in kind” is at `claude-md-standards.md:422`/`:527`, not `:427`.

2. **FAILS — ambience is not mechanical.** Filesystem ancestry is mechanical only after somebody has judged that a statement is a hazard “about” a particular path. Extracting that relation, deciding its intended reader set, and selecting its home are exactly the CCP/CRP/ADP/frequency judgments in the existing placement algorithm (`cohesion-principles.md:270-291`).

   Monorepo build contracts, cross-package schemas, generated sources, symlink access paths, and facts spanning sibling trees all break the simple proxy. Shape-D pointer hubs and external contracts explicitly permit cross-tree anchors (`claude-md-standards.md:343-350`, `:359-374`). A project reference is not ambient—it loads only on demand—and an ambient CLAUDE.md must remain complete without it (`cohesion-principles.md:74-89`, `:131-133`). Thus “relocate or reference” itself requires judgment, and reference-only may fail the stated ambience goal.

   The likely false-positive population is every legitimate cross-tree anchor; the proposal supplies no corpus estimate. At most, ancestry can support an advisory warning for explicit path anchors, with exemptions—not a compliance rule or a completeness check.

3. **FAILS — Part 5’s predicate is not enumerable.** The five labels are semantic outcomes, not mechanically detectable shapes. “Silent” requires reasoning about every error channel and caller; dual-maintenance requires reconstructing a semantic dependency graph; destructive-on-tracked-files requires VCS state. An LLM applying those labels does not become idempotent because the list is finite.

   The list is also post-hoc and narrower than md-domain’s own review-value lattice, which includes blast radius, deliberately-wrong-looking code, security, performance, and ownership (`claude-md-standards.md:376-385`). The evidence itself includes request-thread races, memory corruption, state-parity errors, and incorrect asset guidance—not a clean five-class ontology (`dev/tasks/md-domain-code-review-audit-gap/report-sol.md:19-35`, `:41-43`).

   TypeScript promise/race problems, Rust soundness and feature-gate hazards, Go goroutine/context leaks, and Python transaction/auth/cache hazards do not reduce reliably to these five classes. Fixed prompts are not determinism; the lane itself concedes that related LLM classification is “judgment, not arithmetic” (`audit-lane.md:408-416`).

4. **FAILS — anti-bloat argument.** “Zero words” is overstated. Part 2 cannot repair a zero-mention case by relocation, and the proposal cites such a case itself (`proposal.md:55-61`). Part 3 may expand a count into a missing catalog entry because the existing remediation explicitly prefers correcting the count and adding the missing entry (`claude-md-detect.js:235-237`).

   Port journals and hazards are not fungible. Deleting 300 lines in a deep file does not license adding 10 lines to a root file loaded by every session; net corpus lines ignore ambient-load frequency and reader set. Nor does lifting a global historical-record carve-out guarantee that deletion and hazard authoring occur in the same files or even the same consumer (`claude-md-standards.md:555`).

   Opt-in limits blast radius but does not close the motivating failure: the ordinary “full audit” still omits coverage. The proposal itself leaves Part 5 off by default (`proposal.md:127`, `:149-156`). That is acceptable only if it becomes a separately named coverage result, not support for a stronger default COMPLIANT interpretation.

5. **FAILS — existing-consumer impact and category boundaries.** Part 3 reverses an explicit published policy that counted magnitudes are illustrative (`claude-md-standards.md:366-370`, `:420-422`); without distinguishing exact enumerations from descriptive magnitudes, it will churn compliant corpora.

   Part 4 does not define the finding’s subject, severity, verdict interaction, remediation owner, or review-mode attribution. If the doc is correct and code is wrong, marking the doc NON-COMPLIANT is false; leaving it COMPLIANT beside a code failure is confusing. Existing H2 works because it is narrow and explicitly says the fix is in the repo (`claude-md-standards.md:551-552`).

   A generalized source/invariant sweep exceeds the declared subject of md-domain, which is project Markdown and its placement (`plugins/skills-kit/skills/md-domain/SKILL.md:14-23`, `:282-292`). Keep it within md-domain only as a separate opt-in coverage lane whose subject is `(code subtree, ambient CLAUDE chain)` and whose verdict vocabulary is distinct. Diff-specific code violations belong primarily in code review, not normal `audit claude-md`.

6. **FAILS — regression test.** It is a useful regression fixture, not validation of the proposal. The answer key was used to derive the mechanisms and changes, so recall against it measures reproduction of known flecs findings. The design acknowledges it is non-exhaustive but still overclaims that it can show a mechanism is closed (`regression-test-design.md:75-80`).

   It does not even test M2: the diagnosed M2 examples are guarded ctest suites and literal `python3` (`findings.md:69-70`), but the M1/M2 key omits both and includes `native_registry`, which overlaps M5 (`regression-test-design.md:30-36`). Part 1 has no test. “Unknown but defensible on inspection” makes precision post-hoc rather than pre-registered (`regression-test-design.md:59-62`).

   The missing fourth negative control is a source-level near miss for Part 5: a fixed-cap or dual-maintenance-looking construct that is documented, fails loudly, and is test-enforced must not be reported. Flecs already supplies one in the `MB_HAZARD_CAP`/`MB_MAX_CHARS` contrast (`report-opus.md:52-58`). Beyond that, at least one held-out TypeScript/Rust/Go corpus is mandatory before publishing.

7. **FAILS — critical omissions and cheaper alternative.** Missing entirely are:

   - the coverage lane’s subject and source-directory discovery rule;
   - exclusions for vendored, generated, external, symlinked, and nested-repo trees;
   - runtime/cost bounds and incremental behavior;
   - severity/verdict semantics for code findings;
   - the decision between fixing code, adding enforcement/tests, and documenting an intentional constraint;
   - held-out multilingual evaluation and migration policy for a published plugin.

   The last omission is especially dangerous: many “undocumented hazards” are simply bugs. Converting a fail-open or silent truncation into permanent ambient prose can fossilize behavior that should instead be made loud or removed. The evidence’s `native_registry` case is already a violation of the project’s stated “No silent fallbacks” invariant (`D:/dev/flecs-ecs/CLAUDE.md:120`; `D:/dev/flecs-ecs/engine/src/native_registry.c:27`).

   A substantially simpler path already exists: the authoring direction is explicitly supposed to find high-value kinds that are “present and silent” and write only those (`claude-md-standards.md:352-359`, `:448-450`). Expose that as a non-mutating, bounded `coverage/suggest` lane over a selected directory or current diff, then use the existing placement algorithm (`authoring-lane.md:69-97`). That captures most of M1 without redefining normal document compliance or inventing a universal hazard taxonomy.

**The three strongest objections**

1. **The causal model is wrong on observable facts.** M2 did not apply to the flecs root, and M5 findings are already representable. Before changing standards, record each target’s dimension, criteria applied, source files actually read, and evidence used. Re-run only the missed existing criteria to distinguish prompt noncompliance from missing rules.

2. **Part 5 is an unbounded code-review agent disguised as a mechanical Markdown check.** Replace it with a separately named, bounded coverage lane using the existing value lattice, with code-fix-before-doc remediation and a distinct verdict.

3. **The test is the design set.** Retain flecs as a regression corpus, but require pre-registered near-miss controls plus held-out repositories in at least three language/ecosystem families before shipping.

**What I would do instead**

Ship Part 1 first, phrased narrowly: “COMPLIANT means no FAIL under the listed document criteria; code-review coverage was not assessed.” Reconcile that wording across the standards and lane contracts (`claude-md-standards.md:50-56`; `audit-lane.md:339-349`).

Then make two targeted corrections: classify counts as `exact-enumeration` versus `illustrative-magnitude`, requiring executable enumeration only for the former; and strengthen existing placement checks for explicit out-of-scope path anchors as advisory findings.

Finally, prototype an opt-in `coverage` lane, not a default rule. Its unit is one code directory plus its ambient ancestor chain; it reuses the authoring-direction observation kinds and placement algorithm, reports `GAPS-FOUND/COVERAGE-ASSESSED`, never changes document COMPLIANT, and routes each candidate first to code fix/test enforcement, then to documentation only when the constraint is intentional and durable. Validate it on flecs plus held-out TypeScript, Rust, and Go/Python corpora.

**Where I think this proposal is RIGHT**

M1 is exactly right: normal detection explicitly refuses to discover absent gotchas (`claude-md-standards.md:398-400`; `claude-md-detect.js:203`). It is also right that corpus-wide existence is not enough—the fact must reach the reviewer’s load graph—and that copying facts is the wrong default (`cohesion-principles.md:578-599`).

The proposal is right to demand executable evidence for exact catalogs rather than trusting adjacent comments, and right that a correct document should not be edited to conceal a code violation. Its FLECS_SEED and already-ambient negative controls are good anti-duplication tests (`regression-test-design.md:43-51`). The proposal-first posture is also warranted for a published plugin; the problem is not caution, but that the proposed mechanism has not yet earned publication.