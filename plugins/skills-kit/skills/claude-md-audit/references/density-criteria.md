# Density / Disclosure Lens Criteria

The criteria for the **density lens** of `/claude-md-audit` — an *opt-in* dimension that fires *in addition to* CCP/CRP/ADP/Hygiene when the user asks for it (the `density` argument, or prose intent like "is this CLAUDE.md too verbose / can anything move to a reference"). Loaded by a detect lane only when the run requested the lens. For a normal audit this doc is not loaded and none of these criteria run — default audits are byte-for-byte unchanged.

The classic CCP/CRP/ADP criteria answer **where a fact lives** (which file, which scope). This lens answers the orthogonal question the classic criteria only gesture at: **does a correctly-placed file carry more tokens than its information content needs, and should some of it be disclosed to a reference rather than inlined?** It is the operational form of CRP's "don't make a reader load what they don't need" — applied at the section/block level, not the file level.

## The overriding rule: density ≠ deletion

Every density finding must **route the tokens somewhere** — tighten in place, extract to a reference, or merge a duplicate. A finding whose only effect is *removing* load-bearing nuance is wrong by construction; the lens compresses lossy prose, it does not delete signal. This is why **every criterion here is JUDGMENT severity and every taxonomy row is DISCUSS** — none gate compliance, none auto-apply. Verbosity judgment is noisy and can silently strip nuance an author put there deliberately; a human confirms each call. The lens never emits FAIL and never emits AUTO.

Concretely, for every finding state **where the tokens go**:
- *tighten* → the same information in fewer words, same file, same place;
- *extract* → the block moves to a reference doc and a one-line pointer stays behind;
- *merge* → one of N restatements survives and the others become a cross-reference.

If you cannot name the destination, do not raise the finding.

## Step 1 — identify candidate sections

Read the file top to bottom and mark sections (a `##`/`###` heading and its body) that are *plausibly* over-weight. Cheap signals, none of which is a verdict on its own:

- a section materially longer than its neighbors for no structural reason;
- a worked example, schema dump, or recipe that a reader needs only sometimes;
- the same fact appearing in more than one section;
- preamble/ceremony/hedging ("it is important to note that…", "as always, be careful to…") that carries no testable content;
- a section that restates something a linter, the language, or an ancestor CLAUDE.md already enforces.

Marking is generous; the criteria below are where strictness lives.

## Step 2 — the criteria

### DD-1. density_in_place (JUDGMENT → L_verbose_in_place, DISCUSS)
A section that is **correctly placed and carries real value** but says in N words what materially fewer would carry. Targets: redundant restatement within the section, over-explanation of the obvious, hedging/ceremony preambles, repeated re-establishment of context the reader already has. Output: a *tightened* rewrite (or a token-savings estimate + the specific sentences to cut/compress), **same file, same place**. Never propose moving or deleting the section — that is DD-2 / DD-4.

**Carve-outs (do NOT flag):** a worked example that teaches a genuinely non-obvious procedure; load-bearing nuance that reads as redundant but guards a real failure mode (the author's "even though X, still do Y" is usually load-bearing); deliberate, labeled repetition of a safety rail. When unsure whether prose is ceremony or load-bearing nuance, leave it — false-positive compression is the expensive error.

### DD-2. extract_to_reference (JUDGMENT → M_extract_to_reference, DISCUSS)
A **self-contained block** that (a) serves an on-demand or narrow reading task — not every reader on every load needs it — and (b) is large enough that inlining it taxes every reader who *doesn't*. The fix is **disclosure-level, not scope-level**: the block moves to a `references/*.md` (or a SKILL.md when it is on-task procedure) and a one-line pointer stays in the CLAUDE.md. This is the L1→L3 (or L1→L2) move.

Distinguish it from the classic criteria so findings don't double-count:
- **vs `crp_role_appropriate` (A):** A is *wrong scope* — the content belongs in a different file in the role chain (a subdir CLAUDE.md). DD-2 is *right scope, wrong disclosure level* — the content belongs to this scope but should sit one disclosure layer deeper.
- **vs `crp_size_signal` (F) / `C_crp_split_candidate` (C):** F is the mechanical whole-file size trigger; C is the structural "this whole file decomposes into L2/L3." DD-2 is the finer, block-level call — *this one block* should be disclosed even when the file as a whole is fine. When C and DD-2 both fire, DD-2's per-block proposals are the concrete form of C.

### DD-3. intra_file_redundancy (JUDGMENT → N_intra_file_redundancy, DISCUSS)
The **same fact stated more than once within this one file** (distinct from `ccp_cross_file_duplication` (B), which is duplication across the role chain and is a FAIL/AUTO). Output: keep the single best statement; replace the others with a cross-reference. State once.

### DD-4. value_earns_tokens (JUDGMENT → O_low_value_verbose, DISCUSS)
A section that **does not earn its tokens** under the value filter — and is verbose about it. This is the **classic-file generalization** of the code-directory value filter. Rank kept content by the same lattice; for the canonical ranking and the carve-out list, defer to **`references/code-dir-insight-filter.md` Step 4** (do not restate it here — SSOT). Low-value-and-verbose → propose downgrade (compress to a line) or, for a genuinely contentless section, deletion, **with the user's confirmation**.

**Do not double-count with the code-directory dimension.** If the file is `dimension: code-directory`, the value filter already runs as CD-5 (taxonomy J) — let it own value findings there. DD-4 is for `classic` files (and the non-code sections of a mixed file), where no value filter otherwise runs.

## Step 3 — what this lens does NOT do

- It does not move content between files in the role chain (that is `crp_role_appropriate` / A) — only between *disclosure levels* within a scope.
- It does not invent new content or "improve" an author's voice; it reduces tokens against a fixed information content.
- It does not run by default. A run without the `density` request never loads this doc.
- It never produces a FAIL or an AUTO. A density-only audit is always COMPLIANT; the lens surfaces opportunities, never gates.

## Reporting

Emit density findings under group **Density**, severity **JUDGMENT**, taxonomy one of `L_verbose_in_place` / `M_extract_to_reference` / `N_intra_file_redundancy` / `O_low_value_verbose`, bucket **DISCUSS**. Each finding's `remediation` MUST name the destination (tighten / extract→`<ref path>` / merge→`<surviving location>`) per the overriding rule. Include an approximate token-savings figure when proposing a cut or extract, so the user can weigh the trade.
