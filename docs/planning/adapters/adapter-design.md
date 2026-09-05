# Adapter concept -- design record

Status: ADMITTED and IMPLEMENTED for one model-task pair. The `md-audit`
EVIDENCE PACK, in its compact form at a single call, is the standard
configuration for the local qwen3.8-27b endpoint auditing markdown. The owner
made that call on 2026-09-04 against the measurement in "Outcome" below, and
the seam is settled in "Seam" below. No adapter is admitted for any other
model or task.

## Definition

An adapter is task-specific context attached to one model (or model class) doing
one task so that model produces better results. It is attached only for that
model. Selection therefore has two axes: the model or capability tier, and the
task.

The strong model's prompt is unchanged. An adapter is a targeted context-shaping
intervention for the model whose performance it is intended to improve, not a
general rewrite of the task.

## What an adapter is not

- A general prompt improvement. If every model needs it, put it in the task's
  own prompt.
- A model-agnostic tool.
- Sampling parameters.
- A fine-tune.

## Evidence and the empirical bar

The motivating evidence is a 2026-09-04 measurement: on a markdown audit (one
artifact plus its md-domain standards document, returning JSON findings with
rule ids), qwen3.8-27b served locally by NInfer used model-default sampling,
reasoning_effort xhigh, and max_tokens 60000. It returned one completion per
request. Delivery is therefore not the target gap; quality is.

Adapters are hypotheses, never assumptions. A candidate is admitted only after
measurement records its metric, sample size, cost, and two control runs without
the adapter. A single run, including n=1, never admits an adapter. The measured
model-task pair is the only pair to which that adapter may be applied.

## Candidate attachment seams

The candidates considered were (the choice is recorded under "Seam" below):

1. `awesome-kit` `orchestrate`, at brief-authoring time, keyed by the routed
   model. This can shape the brief before dispatch, but its ownership and timing
   must be tested with the first adapter.
2. The `llm-scripting-kit` model-endpoint registry entry, represented by
   [`plugins/llm-scripting-kit/lib/llm_scripting_kit/model_endpoints.py`](../../../plugins/llm-scripting-kit/lib/llm_scripting_kit/model_endpoints.py). A
   per-endpoint context is task-agnostic, so it cannot select the required task
   axis by itself; endpoint context alone is the wrong axis.
3. The task skill itself, such as `md-domain`, shipping a per-tier variant.
   This keeps task knowledge near the task, but may make model-specific shaping
   less reusable.

The seam these were decided in favour of is recorded under "Seam" below.

## Adapter artifact contract

```yaml
adapter:
  identity:
    model_selector: model id or capability tier measured for this adapter
    task_id: stable identifier for the task
  content:
    - compact generic or standards-derived rule-id checklist
    - generic or standards-derived examples of correctly cited findings
    - required scan-plan step
    - pre-computed structural facts supplied as data
    - standards excerpt trimmed to applicable rules
    - decomposition into per-rule-family calls
  prompt_position: explicit insertion point relative to task instructions and artifact
  cost:
    added_tokens: recorded count
    extra_calls: recorded count
  admission_measurement:
    metric: named quality metric
    sample_size: n
    control_runs_without_adapter: 2
    comparison: same task and conditions with and without the adapter
  application_rule: apply only to the measured model-task pair
```

The content and prompt position are part of the artifact, not an implementation
detail. Cost includes added tokens and extra calls. The admission record must
show the result against both control runs and preserve the cost of the
intervention.

### Adapter ground rule

An adapter is corpus-independent and may not encode information from the corpus.
Allowed context is computed at request time from the artifact and its repository,
generic instructions, a sampling strategy, and material from the standards
document itself. Disallowed context includes ground-truth, reviewer verdicts, or
per-file results: mined exemplars, frequency-ranked checklists, negative rules
distilled from dismissals, and calibration fitted on the corpus. A learnings pack
built from reviewed findings is disqualified, not measured as a loss.

### First concrete content: the md-audit EVIDENCE PACK

The first adapter is the `md-audit` EVIDENCE PACK. It is deterministic,
produced by a prepass, and inserted after the audited file and before the
response schema for the local model only. It adds zero model calls. Each row
carries the exact criterion ids it serves: `placement_not_in_skill_dir`,
`placement_maturation`, `adp_discoverability`, `crp_unitary_reading_task`,
`adp_one_hop_deep`, `adp_no_claude_md_back_reference`,
`ccp_no_skill_content_duplication`, `readme_role`,
`machine_emitted_artifact_provenance`, `hygiene_thresholds`,
`mechanical_convention_hygiene`, or `ancestor_convention_conformance`.

The pack contains identity (artifact routed by PATH, role, direct code files,
and whether the code-directory dimension applies); measurements (lines, bytes,
heading tree, and largest-section share); references (every link, backticked
path, and path:line citation resolved as exists, missing, or directory, with
the actual target line quoted); ancestors (the CLAUDE.md chain, duplicate
candidates at >= 0.85 similarity quoted with both line numbers, and
ancestor-declared conventions quoted verbatim); claim evidence (identifiers
named next to verbs such as "lives", "defines", "exports", or "reads"
grepped in the named file, with NOT FOUND stated plainly); and mechanical
checks (non-ASCII characters, machine-specific absolute paths, temporal
deixis, dead line citations, and the mechanical contract-check verdicts).
It closes with: "Evidence rows are facts, not findings. A row with no rule
violation is not a finding."

Each audit row records the pack's sha256 and size so a result is reproducible.

### Known defect carried from the prototype

`_resolve_path` in `evidence_pack.py` parses a reference with `urlsplit` BEFORE
stripping a trailing `:<line>` citation. A scheme may legally contain dots, so a
bare filename with a line number -- `helper.py:1`, `notes.txt:9` -- parses as
the scheme `helper.py` and is reported `external` instead of being resolved
against the repository. A citation carrying any directory component
(`scripts/helper.py:42`) is unaffected, because the slash rules out a scheme.

It is NOT fixed, and that is now a MEASURED decision rather than a deferral.

The fix is small. Stripping the trailing line number before parsing is
sufficient on its own: the token then carries no colon, so the existing scheme
test behaves correctly, and `mailto:` and host-with-port forms keep working. The
deleted `v2` profile also required `://` for a scheme, which is unnecessary and
would have broken those forms.

It is also correct. On the 21-file screen, five files carry a misparsed
citation and four produce a different pack once fixed. The changed rows move
references such as `shop.gd:47` and `list-plans.js:58` from `external` to
`exists` against the real file, so the unfixed pack does not merely omit those
references -- it states something false about them.

It does not pay. Two screen arms with the fix (2026-09-04, same corpus, same
ground truth of 106 pairs, same engine and request configuration) scored recall
0.330 and 0.321 at precision 0.921 and 0.944, for F1 0.486 and 0.479. The
adopted configuration's two arms scored recall 0.377 and 0.311 at precision
0.976 and 0.971, for F1 0.544 and 0.471. The fixed mean is F1 0.483 against
0.508, and the difference between the means is smaller than the spread between
the adopted configuration's own two arms, so the honest reading is no
detectable effect rather than a regression. ADP, the family those references
feed, did not move: 2 and 1 exact before, 1 and 2 after.

So the pack keeps a known-false row rather than taking an unmeasurable change
to the measured stimulus. Revisit only alongside a change that has its own
reason to re-measure; do not fix it on correctness grounds alone, because that
argument has now been tested and did not hold.

## Anti-patterns

- Leak adapter text into the strong model's prompt. That changes the control and
  taxes a model that did not need the intervention.
- Call a task-wide prompt defect an adapter. Fix the task prompt for every model.
- Adopt an adapter on n=1 or on a result with no twice-run control.
- Apply an adapter to a model that was not measured to need it.

## Measurement results

Why an evidence pack: the diagnosis of the motivating 59-file audit showed the
local model's misses concentrated in facts absent from a single-shot prompt.
It found 1 of 25 ancestor-duplication cases, confirmed 0 of 23 code claims,
missed 30 of 38 mechanical-hygiene cases, and cited paths and line anchors it
could not resolve. The cloud auditors ran with a read-only filesystem, and
their outputs cited repository facts absent from their brief. The pack
pre-computes those facts deterministically and supplies them to the local
model only, at zero extra model calls.

The instrument matches pairs exactly by `(file, canonical rule)` against
reviewer-confirmed proxy ground truth, and reports recall and precision. Corpus
A has 53 files and 198 ground-truth pairs. Each condition has two independent
arms:

- Control: recall 0.237 / 0.217, precision 0.839 / 0.843 (means 0.227 / 0.841).
- Compact evidence pack: recall 0.328 / 0.298, precision 0.844 / 0.881
  (+8.6 recall points, +2.2 precision points over control means).
- Compact pack plus cross-file duplication rows (v4c): recall 0.338 / 0.354,
  precision 0.848 / 0.814 (+11.9 recall points, -1.0 precision point over
  control means). This is the best single-call adapter. Its cross-file
  duplication (CCP) family was 8/8, versus 5/5 for the pack and 1 for control.

Corpus B is held out: 22 files (8 CLAUDE.md, 6 SKILL.md, and 8 project docs)
with 69 folded ground-truth pairs. Its ground truth used two cloud workers,
cloud review lanes, and review of the local model's findings, folding in true
local findings the cloud missed. Cloud workers reached recall 0.68 and 0.76,
with precision 0.86-0.87. On B, control recall was 0.261 / 0.246 and precision
0.750 / 0.654; the compact pack reached recall 0.319 / 0.304 and precision
0.710 / 0.955 (+5.8 recall points, +13.1 precision points over control means).
The pack gain replicates on an untuned repository. v4c has not run on B.

Closed single-call levers, all corpus-independent unless noted: pack position
(system tail versus user head) made no difference; v2 (path-semantics fix,
deterministic claim checks, and per-section budgets) lost because budgets
dropped rows that fed wins; a bigger 40k pack left recall flat and reduced
precision by 10 points; the standards' complete rule list as an unranked
checklist gave no gain; a system demand for every-family coverage scored below
the pack; medium reasoning effort reduced recall by 7-13 points; negative rules
collapsed recall and are also disqualified by the ground rule.

Completeness is the adapter's responsibility. A miss caused by a fact the
pack did not carry is a pack defect, not a model limit: the pack must remove
the need for tool calls. The check is a harness run with read-only tools over
the same brief -- anything the tool loop looks up that the pack did not carry
gets compiled into the next pack version.

The dominant effect is sample variance, not prompt content. Two arms of the
same condition agree on only about half their true pairs. With the pack, the
union of two independent samples gives recall 0.404-0.439 on A and 0.406 on B,
at precision 0.78-0.82; four samples give recall 0.50 on A and 0.58 on B, at
precision 0.67-0.80. Requiring two votes of four gives recall 0.37 at precision
0.91. The server produces one completion per request (n=1), so k samples cost
k requests. This has zero implementation complexity and is an adoption cost
decision for the owner.

A further seam candidate, considered and not chosen: a job specification
(job-kit) that declares the
files and generators a unit needs, materialized into the query by the runner
per endpoint kind -- inlined for a transport endpoint, listed as readable for a
harness endpoint. The completeness guarantee then lives in the job file.

The next measurements, if the loop continues, are v4c on corpus B; a harness
check of the pack's no-tool-calls guarantee; and a per-family decomposition
(multi-call) compared with sampling at equal request count. Then the generation
lane (analyze plus generate claude-md) may begin, gated on accepted audit
quality. Admission covers only the
measured qwen3.8-27b / md-audit pair, and the generation lane is not admitted
by it.

## Outcome

The owner adopted the compact evidence pack at a SINGLE call as the standard
configuration for this model-task pair. Recorded here so the reasoning survives
the experiment.

The deciding view was a single-number one. F1 over exact pairs, on a 21-file
screen with 106 ground-truth pairs, alongside the token cost of producing it.
Cost is stated in tokens rather than wall clock because two of the arms ran
concurrently against one server, which makes their elapsed times meaningless.

| Arm | Recall | Precision | F1 | Tokens | vs control |
|---|---|---|---|---|---|
| Bare control | 0.227 | 0.841 | 0.36 | 759k | 1.0x |
| Compact pack, one call | 0.344 | 0.974 | 0.51 | 815k | 1.1x |
| Compact pack, union of two samples | 0.425 | 0.978 | 0.59 | 1.63M | 2.1x |
| Tool-using harness, no pack | 0.387 | 0.759 | 0.51 | 1.79M | 2.4x |
| Tool-using harness plus pack | 0.382 | 0.827 | 0.52 | 1.67M | 2.2x |
| Tool-using harness plus pack, two samples | 0.491 | 0.825 | 0.62 | 3.34M | 4.4x |

The adopted row is the second. It buys 15 points of F1 over the bare control
for 7 percent more tokens, and it is the only row whose precision stays near
0.97. Every higher-scoring row costs at least twice the control and gives up
15 precision points or more.

Three findings behind the table are worth keeping.

**Sampling beats prompt content, and it is not free.** Two arms of one condition
agree on roughly half their true pairs, so the union of two samples is the
largest single gain available. It was NOT built into the driver. Re-auditing is
already possible whenever a caller wants that recall, so a k-loop would spend
double on every audit to serve the minority of cases that want it. The default
is one call; a second audit is the caller's decision, taken per run.

**Tools are where the strong models get their lead, and the pack recovers only
part of it.** Cloud auditors given a read-only filesystem reach recall 0.68 to
0.81. The local model given the same tool loop reaches 0.387, and its precision
falls to 0.759 because a tool loop invents findings the pack does not. Adding
the pack to the tool loop raises precision to 0.825 and CUTS its tool calls
from about 300 to 215 over 21 files, because the model stops hunting for context
it was already given. The pack makes the tool path cheaper; it does not make it
worth 2.2x.

**One family resists the pack.** Code-directory findings fall from 11 exact in
the tool loop to 5 with the pack present, the only family where a tool loop
beats the pack outright. That is the standing candidate for the next pack
version under the completeness rule above.

A separate hypothesis was closed against this table: mining the strong models'
actual tool calls and precomputing those lookups into the pack. Two variants
were built, one carrying path contexts and topic owners and one carrying paths
alone. The first lost on both screen arms and the second was neutral. The bound
was visible in the mining itself, which found only 11 of 18 residual misses
addressable by any additional lookup, and 3 already carried by the pack and unused.
Precomputing more context does not raise single-call recall.

## Seam

The seam question is settled, and none of the three candidates above won it.

The adapter is owned END TO END by the task skill, `md-domain`, and enforced in
`plugins/skills-kit/skills/md-domain/scripts/emit_audit_jobs.py` -- the emitter that materialises
an audit prompt into a job file. That script already receives the endpoint
names and already writes an `endpoint_preference` list into every job, so the
model axis was an input it held all along. The record's premise that the task
skill cannot know the model was false in the shipped code.

The pack builder lives beside it at `plugins/skills-kit/skills/md-domain/scripts/evidence_pack.py`
with `build_pack(repo_root, rel_path, compact=True, max_chars=24000)`. It is the
prototype's compact profile, ported byte-for-byte; the eight experiment profiles
that lost or were disqualified are not shipped.

Enforcement is by construction rather than by convention. `emit_audit_jobs.py`
attaches the pack when EVERY preferred endpoint is admitted, attaches nothing
when none is, and FAILS the emit when the list mixes the two -- job-kit resolves
that list at run time, so a pack chosen at emit time against a mixed list is
wrong for whichever endpoint it did not choose. Admission is per measured pair,
which is stricter than a model-tier test: an endpoint id in that set is a claim
that this adapter was measured for that endpoint on this task.

The admitted set is CONFIGURATION, not a shipped list, and its default is empty.
Endpoint ids differ per user and per fleet, so a fixed list would make the
adapter dead for anyone whose endpoints are named differently and would put a
machine-identifying name in a public repository. The set is read through
skills-kit's existing layered config as
`adapters: {md-audit-evidence-pack: {admitted_endpoints: [...]}}`
(`skills_kit_lib/standards_resolve.py`); an unconfigured run admits nothing and
therefore behaves exactly as if the adapter did not exist.

The rejected alternative was a split: the task skill DECLARES the condition and
the caller enforces it. A cross-check rejected it as a two-sided contract with
no test that sees both sides -- nothing obliges a caller to read the
declaration, and a caller that forgets produces a plausible report at the lower
score with no error for anyone to notice.

The wrapper line introducing the pack in the prompt is part of the measured
stimulus. The 2026-09-04 figures were produced with that exact framing and no
code fence, so rewording it changes the prompt the measurement was taken
against.

One dependency, worth naming because it is invisible from the emitter.
Enforcement holds because the `workflow/*-detect.js` lanes build their
prompts in process and hard-pin a frontier model, so no admitted endpoint ever
reaches them. That pinning is documented in `plugins/skills-kit/skills/md-domain/references/lanes/audit-lane.md`
under "Model pinning". The day a lane gains a configurable model, that lane
becomes a second path to an audit prompt and this enforcement no longer covers
every caller.
