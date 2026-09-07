# Adapter experiments that did not pay

This catalog records adapter candidates and operating profiles that should not
be rediscovered as fresh ideas. Read it with the
[model-task adapter reference](adapters.md), which defines the ground rule and
the empirical admission bar. "Did not pay" has four distinct meanings here:
a measured quality loss or null result, a cost rejection despite a quality
gain, disqualification before measurement, or an integration assumption that
failed end to end. Each entry names which one applies.

The durable source for the md-audit comparison tables is the
[adapter design record](../planning/adapters/adapter-design.md). Screen-only
details below cite the dated headings or variant rows in the experiment task
record's `ledger.md`; the generation and integration evidence cites its
`plan.md`, `log.md`, or `CLAUDE.md`. Those task files are named without their
machine-local location because this repository is public.

## Generation-lane evidence-pack transfer

**Tried.** The audit pack's shape was transferred to md-domain generation on
one code directory. A deterministic prepass supplied files, the internal import
graph, external dependencies, and entry points. Two unchanged controls and two
pack arms used the same request configuration.

**Observed.** The control arms used 6,926 and 24,531 reasoning tokens; the pack
arms used 15,711 and 22,147. Their means were 15,728 and 18,929, a 3,200-token
between-condition gap against the control condition's own 17,605-token spread.
There was no detectable cost effect. The earlier single-pair statement that the
pack "roughly doubled" cost was RETRACTED when the second control ran. A weak
eight-fact presence check likewise scored 3.5/8 for control and 4.0/8 for the
pack, inside the observed noise; every arm missed the same four facts.

Those arms are supporting evidence, not the verdict. The generation standard
[`absent-fact-earns-ambient-cost`](../../plugins/skills-kit/skills/md-domain/references/standards/coverage-standards.md)
admits only a fact that a straightforward reading of the local mistake site
does not reveal, and its example rejects a file inventory by name. The pack's
files, import graph, external dependencies, and entry points are all
recoverable from that reading. They were inadmissible output by construction,
so the model was right to omit them.

**Takeaway.** The audit and generation tasks have different information
economies. Audit rewards mechanical lookups that the model otherwise skips;
generation charges ambient-document space only for facts local reading does
not reveal. More arms or different framing cannot make the transferred
sections admissible. Close this pack shape for generation without generalizing
the result to other adapter shapes.

Source: experiment task record, `plan.md` Accomplished entry dated 2026-09-05,
`log.md` repeated-arm entry dated 2026-09-05, and `CLAUDE.md` "Where we are
today".

## Pack position: system tail versus user head

**Tried.** The verbose v1 pack was moved from the user-head condition to the
tail of the system prompt, leaving its content unchanged.

**Observed.** Both screen conditions reached recall 0.321. The system-tail arm
had precision 0.895; there was no recall gain to promote. This was a position
screen, not a comparison between different pack contents.

**Takeaway.** Do not reopen prompt position by merely swapping these two
locations. Position is part of a stimulus and a genuinely different framing
may still be tested, but this exact system-tail move supplied no detectable
benefit.

Source: experiment `ledger.md`, initial variant table, `v1p system-tail`; also
summarized under [Measurement results](../planning/adapters/adapter-design.md#measurement-results).

## Verbose v1 payload

**Tried.** The original verbose evidence pack retained resolved-OK reference
rows and longer section text. The compact successor collapsed those routine
rows and attached criterion ids directly to sections.

**Observed.** The verbose full arms reached recall 0.323 and 0.318 at precision
0.877 and 0.875. The compact pack was 36 percent smaller and remained within
the same noisy quality range. The owner selected the compact pack for the
admitted single-call profile.

**Takeaway.** More explanatory payload is not a free improvement. When two
forms are indistinguishable on quality, prefer the smaller measured stimulus
and do not restore verbose rows without a new reason to measure them.

Source: experiment `ledger.md`, initial variant table and 2026-09-03 20:40
verdict; adoption is recorded under
[Outcome](../planning/adapters/adapter-design.md#outcome).

## The v2 profile

**Tried.** V2 combined corrected path semantics, deterministic claim checks,
one primary criterion id per row, and per-section budgets within a
12,000-character total. It also removed routine `exists` rows.

**Observed.** Its screen scored recall 0.255 and precision 0.900, against the
compact arm's 0.377 and 0.976. Budget cuts removed measurement and ancestor
rows that had fed correct CRP findings: that family fell from 15 exact pairs to
10, while deterministic CD checks did not recover the loss.

**Takeaway.** The bundle lost because evidence was budgeted by section before
the model saw it. Correcter labels and deterministic checks did not compensate
for dropping useful rows. Do not resurrect v2 as a bundle; isolate any future
correctness change and treat it as a new stimulus.

Source: experiment `ledger.md`, initial `v2` row, and `log.md` heading
"2026-09-03 19:08 -- v2 lost"; summary in
[Measurement results](../planning/adapters/adapter-design.md#measurement-results).

## Expanded evidence pack at 24k

**Tried.** V4 added path labels, cross-file duplication rows, and deterministic
CD checks to the compact base, removed section budgets, and kept only a global
24,000-character cap.

**Observed.** The screen scored recall 0.321 and precision 0.919, within the
compact arms' recall range of 0.311 to 0.377 and below their roughly 0.97
precision. Its target CCP family improved to 6 exact pairs from 2 and 3, but
CRP, Hygiene, and Placement gave back the gain as the median pack grew from
about 7.5k to 13k characters.

**Takeaway.** A section can improve its target family while making the full
artifact no better. Judge the whole output, not the local row count; added
evidence can dilute attention paid to the admitted base.

Source: experiment `ledger.md`, `v4 (24k cap)` row.

## Expanded evidence pack at 40k

**Tried.** The same v4 content was allowed a 40,000-character global cap so
truncated cross-file and CD rows could enter the prompt.

**Observed.** Recall was 0.330 versus 0.321 at 24k, while precision fell from
0.919 to 0.814. More evidence rows produced more findings, not more true
findings; the approximately one-point recall difference was inside the much
larger arm-to-arm variance already observed.

**Takeaway.** Raising the cap did not repair the expanded pack. A larger pack
needs a targeted omission hypothesis, not the assumption that untruncated
evidence will be used.

Source: experiment `ledger.md`, `v4 (40k cap)` row; summary in
[Measurement results](../planning/adapters/adapter-design.md#measurement-results).

## Cross-file-only expansion (v4c)

**Tried.** V4c kept only the v4 section that worked locally: cross-file
duplication rows, about 1.5k characters on top of the compact pack.

**Observed.** Against control, v4c gained 11.9 recall points and lost 1.0
precision point on corpus A, then gained 5.1 recall and 9.2 precision points on
held-out corpus B. Against the compact pack itself, however, it was +3.3 recall
and -3.2 precision on A, then -1.4 recall and -5.8 precision on B. The CCP
family gain replicated; the net advantage did not.

**Takeaway.** V4c is a measured family trade, not a better default. Do not
promote a targeted-family win into an overall adapter claim, especially when
the held-out corpus reverses the net result.

Source: experiment `ledger.md`, headings "v4c FULL CONFIRM" and "corpus B FINAL
instrument"; the cross-corpus result is summarized under
[Measurement results](../planning/adapters/adapter-design.md#measurement-results).

## Complete unranked standards checklist

**Tried.** V3s appended every applicable canonical rule id and its standards
gist, in standards order and without corpus-derived ranking: 76 ids for
CLAUDE.md, 41 for skills, and 45 for project docs.

**Observed.** The screen scored recall 0.311 and precision 0.868. That supplied
no recall gain over the compact arms, reduced precision by about 10 points, and
left CD at one exact pair.

**Takeaway.** Input-side enumeration does not make this model traverse every
rule. A complete checklist is eligible under the ground rule, but completeness
of instructions is not evidence of completeness of attention.

Source: experiment `ledger.md`, 2026-09-03 23:50 entry and `v3s (legal
checklist)` row; summary in
[Measurement results](../planning/adapters/adapter-design.md#measurement-results).

## System demand for every-family coverage

**Tried.** The compact pack gained a qwen-only system instruction demanding an
every-family review and spelling out CD distinctions.

**Observed.** Its screen scored recall 0.283 and precision 0.882, below both
compact recall arms, and returned zero exact ADP pairs.

**Takeaway.** A stronger demand for breadth does not create the missing scan
behavior. Do not substitute imperative wording for evidence that the requested
coverage increased.

Source: experiment `ledger.md`, `compact + qwen SYSTEM instruction` row;
summary in
[Measurement results](../planning/adapters/adapter-design.md#measurement-results).

## Medium reasoning effort

**Tried.** The compact pack ran at medium rather than xhigh reasoning effort,
with the rest of the request configuration unchanged.

**Observed.** Recall fell to 0.245, 7 to 13 points below the two compact arms,
at precision 0.963. Median completion tokens fell from 12,535 to 6,973, and one
of 21 responses was empty.

**Takeaway.** The lower request cost came from doing less of the coverage task.
Medium effort is not an equivalent cheaper profile for this admitted
model-task pair.

Source: experiment `ledger.md`, `compact @ MEDIUM effort` row; summary in
[Measurement results](../planning/adapters/adapter-design.md#measurement-results).

## Corpus-derived learnings pack (v3)

**Tried.** V3 assembled dismissed-finding rules, confirmed exemplars, and a
frequency-ranked checklist from reviewed corpus results. A follow-on v3e would
have retained exemplars and ranking without the negative rules; a calibrated
postfilter was also proposed.

**Observed.** These artifacts use ground truth, reviewer verdicts, or per-file
outcomes as adapter input. The corpus-independence ground rule therefore
reclassified v3 as DISQUALIFIED, discarded v3e before a run, and disqualified
the calibrated postfilter. V3 had already produced a screen score, but that
score is not an eligible adapter comparison and is not its verdict.

**Takeaway.** V3 was disqualified, not lost on measurement. Do not re-run it on
the same or a different corpus in an attempt to reverse a score: its provenance
makes the candidate ineligible before scoring.

Source: experiment `plan.md`, "Ground rules", and `ledger.md` ground-rule entry
dated 2026-09-03 23:45; repository statement in the
[adapter ground rule](../planning/adapters/adapter-design.md#adapter-ground-rule).

## Negative rules distilled from dismissals

**Tried.** Five prohibition-style rules mined from reviewer dismissals were
included inside v3 to suppress known false-positive habits.

**Observed.** They suppressed reporting across families: the screen returned
27 findings versus 53 for compact, left 7 files with no findings, and dropped
CRP exact pairs from 15 to 3. Alias citation behavior improved, but recall
collapsed. The rules are also corpus-derived and thus independently
disqualified by the ground rule.

**Takeaway.** This lever is doubly closed. Its measured behavior is wrong for a
recall-limited model, and its provenance makes it ineligible regardless of
that behavior. Neither a prompt rewrite nor another corpus repairs the second
failure.

Source: experiment `log.md` heading "2026-09-03 20:03 -- v3 lost" and
`ledger.md` initial `v3` row; eligibility in the
[adapter ground rule](../planning/adapters/adapter-design.md#adapter-ground-rule).

## Transcript-mined lookup packs

The transcript study found 18 residual exact pairs that tool-using agents found
and every compact arm missed. Eleven were addressable by another request-time
lookup, while three were already carried by the compact pack and simply went
unused. Two corpus-independent packs tested whether precomputing the additional
lookups would close that gap.

Source for both variants: experiment `ledger.md` heading "transcript-mined-pack"
and `log.md` entries from 2026-09-04 12:10 through 13:56; durable conclusion
under [Outcome](../planning/adapters/adapter-design.md#outcome).

### V5: path contexts and topic owners

**Tried.** V5 added dual-context path-resolution rows and topic-owner sentence
pairs to the compact pack.

**Observed.** Its two screens scored recall 0.255 and 0.302 at precision 0.871
and 0.914. Both arms were below the compact arms on recall and precision.

**Takeaway.** The sentence-pair expansion repeated v4's dilution pattern.
Precomputing tool lookups did not make the model use them in a single call.

### V5p: path contexts alone

**Tried.** V5p removed topic owners and retained only the path contexts.

**Observed.** Its two arms scored recall 0.321 and 0.302 at precision 1.000 and
0.941, for mean recall 0.311 versus 0.344 for compact. Its target ADP family
scored 0 and 2 exact pairs versus compact's 2 and 1.

**Takeaway.** Paths alone were neutral, including on their target family. Once
the compact pack is present, evidence use rather than evidence presence is the
binding limitation for this lookup family.

## Reference-resolution fix

**Tried.** The pack's path resolver was corrected to strip a trailing line
number before URL parsing. The old order falsely classified bare references
such as `helper.py:1` as external.

**Observed.** Five of 21 screen files contained an affected citation and four
packs changed; the repaired rows resolved against real files. Two corrected
arms scored F1 0.486 and 0.479, mean 0.483, against the admitted arms' mean
0.508. The 0.025 difference between means was smaller than the admitted
condition's own 0.073 arm spread, and the target ADP family did not move.

**Takeaway.** The fix is correct and measured, but it was not shipped because
it had no detectable task effect and would change the admitted stimulus. Batch
it only with a change that has an independent reason to remeasure; do not call
the result a regression or reopen it on correctness alone.

Source:
[Known defect carried from the prototype](../planning/adapters/adapter-design.md#known-defect-carried-from-the-prototype).

## Repeated sampling as the default

**Tried.** Independent compact-pack completions were unioned instead of making
one request per file.

**Observed.** On the adoption screen, the union of two samples reached F1 0.59
at 2.1x control tokens, compared with F1 0.51 at 1.1x for the single-call pack.
Sampling improved quality; it was not a null result.

**Takeaway.** Built-in repeated sampling was rejected on operating cost, not
quality. Re-auditing remains a caller-selected way to buy recall for a specific
run, but it is not the adapter's default and should not be smuggled in as prompt
machinery.

Source: [Outcome](../planning/adapters/adapter-design.md#outcome).

## Tool-using harness profiles

**Tried.** The local qwen3.8-27b endpoint ran the same audit through a read-only
tool loop, both without the pack and with it; the pack-plus-tools condition was
also unioned across two samples.

**Observed.** The no-pack tool row reached F1 0.51 at 2.4x control tokens, the
single pack-plus-tools row reached 0.52 at 2.2x, and its two-sample union reached
0.62 at 4.4x, the highest measured qwen score. For context, the non-tool
two-sample pack also scored higher than the default, F1 0.59 at 2.1x. These
profiles improved quality; they did not fail the quality comparison.

**Takeaway.** This is a cost rejection, not a quality rejection. The admitted
single-call pack bought most of the useful gain for 1.1x control tokens and
kept precision near 0.97; every higher-scoring row cost 2.1x to 4.4x. Use a
tool-capable path when the run warrants that cost, but do not describe it as a
free completion of the evidence pack.

Source: [Outcome](../planning/adapters/adapter-design.md#outcome).

## Published-source inspection as completion evidence

**Tried.** After the adapter was implemented and published, its source,
builder, unit coverage, and emitted job were inspected as evidence that the
feature was complete. A real audit was then run through the published emitter,
routing layer, transport, and response check.

**Observed.** The end-to-end run exposed three independent blockers. A hidden
routing floor required one harness-specific control and excluded transport
endpoints; the emitted prompt told a toolless transport to open three files
that the measured prompt had inlined; and the job omitted the measured request
configuration, so a 4,096-token default was consumed by reasoning before an
answer. Local admission configuration was also absent, so the pack had not
attached merely because its source existed.

**Takeaway.** Published source is not evidence that an adapter runs. Verify the
published path through selection, emission, routing, transport, response
validation, and local admission; each layer can preserve plausible-looking
source while defeating the measured configuration.

Source: experiment task record, `CLAUDE.md` "Where we are today" and `plan.md`
Accomplished entry dated 2026-09-05.

## What was not ruled out

This section is deliberately separate from the closed catalog. Each item below
was deferred or proposed without a measurement. None is evidence of a win, but
none may be called a measured loss.

### Per-rule-family decomposition

Separate calls using the correct md-audit families were deferred without an
arm. The required partition is CCP, CRP, ADP, Hygiene, and CD; the earlier
pilot's partition was not the proposed experiment. This remains a multi-call
candidate to compare with ordinary sampling at equal request count.

Source: experiment `plan.md`, `decomposition-adapter` item, and
[Measurement results](../planning/adapters/adapter-design.md#measurement-results).

### Standards trimming

A smaller standards payload was proposed, but the density estimates were
unverified and no control/candidate arms ran. Trimming remains an unmeasured
hypothesis; the complete-checklist loss does not answer it because adding an
index and removing standards content are different stimuli.

Source: experiment `plan.md`, `standards-trim-adapter` item.

### Inverted suppression-signals pack

After the generation transfer failed, the task record proposed inverting the
prepass: compute what is already ambient or already stated near the relevant
site, and supply suppression signals serving
`already-ambient-suppressed`. No pack was built and no arm ran.

This is not the rejected content inventory in reverse wording. It targets
duplicate-candidate suppression rather than trying to insert cheaply
recoverable facts. Its eligibility and usefulness still need to be established
before admission.

Source: experiment `CLAUDE.md` "Where we want to get to" and `plan.md`
`generation-adapter-databench` item; criterion in
[coverage standards](../../plugins/skills-kit/skills/md-domain/references/standards/coverage-standards.md).
