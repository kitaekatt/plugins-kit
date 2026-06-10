# Code-Directory Insight-Validation Criteria

The criteria for the **code-directory dimension** of the claude-md-audit member (`/md-audit claude-md`) — the validation that fires *in addition to* CCP/CRP/ADP/Hygiene when a CLAUDE.md sits inside (or describes) a directory of code / YAML / CSV. Loaded by a detect lane only when `discover.py` flagged the file `dimension: code-directory` (the Level-1 trigger; see `scripts/discover.py::classify_dimension`). For a `classic` file this doc is not loaded and none of these criteria run.

These files are not librarian artifacts — they are **distilled review intelligence about one directory**. Their failure mode is not misplacement; it is **the claims rotted** (the god-object got decomposed, the sibling config was renamed, the line anchor drifted) or **the insight stopped earning its place**. This dimension validates fidelity-to-code and value. It is a **validator over existing claims, not a gotcha crawler** — it does not scan the directory for *new* gotchas to add (that is the authoring pipeline; doing it here would be non-idempotent and expensive).

## The two-level recognition model

- **Level 1 (already done by `discover.py`)** decided *whether* this dimension runs (the file is flagged `code-directory`).
- **Level 2 (this doc)** decides *how hard* to scrutinize each claim — via the **anchor-modality classifier** below. Only one anchor modality is ever eligible for FAIL. This is the safety valve: because Level 1 triggers generously, Level 2 must be strict about what can FAIL, so accurate negative-existence / external / templated / generated claims are never punished.

The one judgment call is identifying the file's **shape** (A/B/C/D); it gates nothing — it only tells you which observation kinds to expect.

## Step 1 — identify the shape(s)

A file may legitimately **mix** shapes (an architecture preamble + a gotcha list + a cross-child rule block in one file). Identify the dominant shape and note any mixed-in ones; do not flag a file for mixing.

- **Shape A — gotcha-per-section** (source code: C++, C#, Python). A `##` heading is usually a claim; the body gives an anchor + a why + a do-instead.
- **Shape B — purpose + Schema/Files + Review Checks** (data/config: YAML, CSV). The payload is the `## Review Checks` section. An *annotated* `## Files`/`## Schema` block (each entry carries a constraint) is payload, not inventory.
- **Shape C — boundary / ownership** (directory-level; common in infra). Headings are boundary statements; body says what lives here vs. not, plus safety rails and ordering invariants.
- **Shape D — architecture / pointer-hub exposition**. Headings are **labels, not claims**; anchors are **named patterns** or **pointers to an SSOT**, not symbol/line pointers. Do NOT try to resolve a symbol/line anchor for a Shape-D heading — validate that its pointer target (the doc it points to) resolves.

## Step 2 — classify every anchor's modality BEFORE any existence check

For each concrete anchor a claim makes (a symbol, file, sibling path, field, name, command), tag exactly one modality. **Only `requires-present` is eligible for a FAIL.** This classification is the gate; run it first.

| Modality | How to recognize it | Scoring |
|---|---|---|
| **requires-present** | a symbol/file/sibling the claim says *should exist* (`TryAction`, `../Cohorts/`, a field name) | the ONLY modality eligible for FAIL when named-and-absent |
| **requires-absent** (negative-existence) | the claim asserts/requires absence: "gitignored", "does not exist in a clean checkout", "a tracked X here is a leak", a FORBIDDEN list | **inverted**: absence = PASS; *presence* of the asserted-absent thing = FAIL (taxonomy H2) |
| **external-unverifiable** | lives outside this repo/VCS: cross-repo Perforce `//depot/...`, cluster-side context/namespace/AWS-profile names, 1Password / Secrets-Manager refs, another repo's HTTP contract | INFO / UNVERIFIABLE, **never FAIL** |
| **template-or-env** | `{{ .Values.* }}`, `$DB_PASSWORD`, `secretKeyRef` targets, helm/k8s runtime names | resolve against the template/values graph if cheap, else UNVERIFIABLE; **never grep as a literal, never FAIL** |
| **vendored-don't-read** | a vendored binary/subtree (`aws-iam-authenticator`) | confirm presence only; **never open the bytes** |
| **generated-or-unsynced** | matches `*Generated.*`, lives under `Intermediate/`/`Saved/`/`node_modules/`; a codegen template name (`CN<Name>...`); a Perforce path that may not be synced locally | INFO "anchor unresolved — may be generated or unsynced; verify on a full sync"; **never FAIL** |
| **non-anchor** | a macro / keyword / concept-word, not a resolvable identifier: `UPROPERTY`, `UCLASS`, `UFUNCTION`, `GENERATED_BODY`, `checkNoEntry`, `SFAssert`, `__cpp_exceptions`, `DOREPLIFETIME` | skip entirely (no finding) |

Resolve symbol anchors **repo-wide**, not directory-locally — a YAML dir legitimately cites a `.cs` symbol in another module (e.g. `MigratedIds.cs`). A leading-slash path (`/docs/...`, `/kubernetes/CLAUDE.md`) resolves against **repo root**, not filesystem root.

## Step 3 — the criteria

### CD-1. anchor_modality_classify (precondition, no severity)
Tag every anchor per Step 2. Emit nothing on its own; it gates CD-2/CD-3.

### CD-2. fidelity_anchor_resolves
**requires-present** anchor that is named-and-absent (after a repo-wide check) → **FAIL** (taxonomy H). For a **requires-absent** anchor, run inverted: the asserted-absent thing is now *present* (a tracked file under a gitignored SSOT path; a FORBIDDEN name that now resolves) → **FAIL** (taxonomy H2 — the invariant is violated, surface it loudly). All other modalities → PASS/INFO per the table, never FAIL.

### CD-3. fidelity_line_anchor (JUDGMENT, coupled to symbol resolution)
When a claim cites a line number (`lines ~2204-2215`, `line 120`): find the enclosing symbol the claim names. If the symbol resolves but is **>~30 lines** from the cited number → **I2_line_drift**, remediation "drop the line number; keep the symbol anchor" (AUTO). **Stay silent** if the author already supplied a recovery hint (e.g. "run `grep -n '# NOTE:'` before reviewing"). If no line number is cited, skip.

### CD-4. fidelity_claim_holds (JUDGMENT, never auto-FAIL)
Read the anchored code; is the claim still true **in kind**? A god-object now decomposed, a TODO the claim depends on now resolved, a "bypasses X" that no longer bypasses → **I_claim_drift** (DISCUSS). **Counted magnitudes are intentionally fuzzy** — "7200-line", "resets 10 fields", "12 C# files" — **never FAIL on the number**; flag only if the *kind inverts* (god-object → small/decomposed).

### CD-5. value_insight_earns_place (JUDGMENT)
Run each section through the value filter (Step 4). Low-value → **J_low_value_insight** (DISCUSS; a genuinely *bare* un-annotated inventory may be AUTO delete).

### CD-6. silent_failure_preserved (INFO, positive check)
If the file has been reduced to only structural description with **no** tier-1/tier-2 silent-failure or blast-radius claim, emit an INFO erosion signal — the highest-value content may have been edited out.

## Step 4 — the value filter ("what we care about")

An insight earns its place only if a code-review agent that read it would catch something a senior teammate catches and a generic reviewer misses. Rank kept content by:

1. **Silent failure** — no compiler/linter/type-checker/test/CI catches it. *Highest value.*
2. **Blast radius / coupling** — a change here breaks something *there*, across a file/dir/repo boundary.
3. **Deliberately-wrong-looking** — looks like it should be "fixed/simplified" but must not be.
4. **Safety / security rails** — forbidden targets, secrets hygiene, auth boundaries.
5. **Diff-invisible performance** — per-tick spam, O(N) loops that read as O(1) in the hunk.
6. **Ownership / boundary** — what belongs here; what to review vs. ignore (vendored).

**Flag as low-value (J):** linter/compiler/CI-enforced rules; language/framework defaults; generic programming advice; a rule already in an ancestor CLAUDE.md (CCP duplication — defer to the classic CCP criteria); a **bare** directory inventory; an empty heading with no claim.

**Carve-outs — do NOT flag these as low-value (both maintainer-agents required them):**
- An **annotated** `## Files`/`## Schema` block whose entries each carry a constraint (`credentials.json` must be a template; `cohortConfigId` must match `Cohorts/`) — it is payload; the Review Checks depend on it. Never AUTO-delete.
- A denormalized **constraint catalog that points to its SSOT** (e.g. an EKS C1–F3 list mirroring a runbook) — a navigational cheatsheet by design.
- **Operational cheatsheets that scope a safety rail** (the allowed kubectl/deploy commands next to a FORBIDDEN list) — part of the rail.
- **Topology / ownership tables** (cluster→namespace→deployments, `KNOWN_ACCOUNTS`, account-id duality) — blast-radius coupling maps, not inventories.

## Step 5 — observation kinds (what high-value content looks like, per shape)

Use this only to recognize value; do not go hunting for missing kinds.

- **Shape A:** god-object/don't-add-here · deliberate hack/workaround (don't simplify) · diff-invisible perf trap (O(N), per-tick) · lifetime/ownership hazard (raw `this`, must-outlive) · type-safety bypass (don't copy) · dead/misleading code · build-flag-dependent behavior · lifecycle-method contract · "use the helper, not inline".
- **Shape B:** cross-config referential integrity (*"silent at build time"* — highest value) · rename/removal blast radius (*"search for usages first"*) · "not just config review" escalation · secrets hygiene · asset/external-path validity · **append-only data-ledger** (order-immutable, sentinel value, "removing an entry corrupts save data").
- **Shape C:** allowed/FORBIDDEN safety rails · gitignored-by-design/tracked-file-is-a-leak · vendored subtree ("review provenance not bytes") · deploy/migration ordering invariant · children-index blast radius · pointer to a universal rule (don't restate).
- **Shape D:** architecture exposition (named-pattern invariant: "mirrors the binary schema — do not diverge") · pointer-hub ("rule is universal; payload in <SSOT>; here is the one local delta") · **external-contract** (local constant + "the other side lives in `<repo>`; verify by hand" — a *complete* claim, not a defective bare prohibition).

## Taxonomy (extends the classic A–K)

| ID | Name | Detection | Default remediation | Bucket |
|---|---|---|---|---|
| `H_stale_anchor` | requires-present anchor no longer resolves (repo-wide) | symbol/sibling/path absent and not external/generated/template/vendored | re-anchor to current symbol/path, or delete if the code is gone | DISCUSS |
| `H2_inverted_absence` | requires-absent thing is now present | tracked file under a gitignored SSOT path; a FORBIDDEN name now resolves | escalate as a finding — the invariant is violated | DISCUSS |
| `I_claim_drift` | claim no longer matches the code *in kind* | code read contradicts the claim (not a counted magnitude) | re-validate with user; update mechanism or retire the claim | DISCUSS |
| `I2_line_drift` | symbol found far from cited line, no recovery hint | enclosing symbol resolves >~30 lines from the number | drop the line number, keep the symbol | AUTO |
| `J_low_value_insight` | section fails the value filter (after carve-outs) | linter-caught / default / *bare* inventory / pure restatement | delete (bare inventory) or downgrade | DISCUSS (bare inventory delete → AUTO) |
| `K_unclassified` | escape hatch | nothing above fits after a deliberate attempt | user proposes strategy | SPECIAL |

## Severity & verdict interaction

- `CD-2` FAIL (H or H2) gates the file NON-COMPLIANT, same as a classic FAIL.
- `CD-3`/`CD-4`/`CD-5` are JUDGMENT/DISCUSS and `CD-6` is INFO — they surface for review without gating, and do not escalate to FAIL on re-run.
- Idempotency: the modality classification and anchor resolution are mechanical; the JUDGMENT prompts are fixed. Same file + same tree → same findings.

## Output

Emit findings under a `CodeDir` group, alongside the classic CCP/CRP/ADP/Hygiene/Schema groups:

```
### CodeDir (insight validation — code/yaml/csv directory)
[FAIL]     CD-2 H_stale_anchor: `OldSymbol` (line 14) no longer resolves anywhere in the repo
[JUDGMENT] CD-4 I_claim_drift: "7200-line god object" — SCCharacter.cpp is now decomposed (~3000 lines)
[AUTO]     CD-3 I2_line_drift: "lines ~2204-2215" — TryInterrupt hack is at ~2398; drop the number
[INFO]     CD-2 generated-or-unsynced: `OzyComponents.h` unresolved — likely codegen output; verify on full sync
[PASS]     CD-2 requires-absent: kubernetes/secrets/ correctly absent from the checkout
```

A file with no `requires-present` anchors and no value-filter failures is **COMPLIANT** on this dimension even though it carries many claims — absence of FAIL is the bar, exactly as for the classic dimensions.
