# Adapter concept -- design record

Status: DRAFT. The first adapter is under construction for the `md-audit` task.
This record defines the concept; it does not choose an attachment seam or claim
that an adapter has improved a result.

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

The motivating evidence is the user's measurement from 2026-09-03: in a 59-file
Markdown audit against a standards document of about 100 KB, a local 27B
reasoning model at its highest reasoning effort returned 59/59 answers but
reached about 14-23% recall. Cloud models under the same task reached about
64-70% recall. Delivery is therefore not the target gap; quality is.

Adapters are hypotheses, never assumptions. A candidate is admitted only after
measurement records its metric, sample size, cost, and two control runs without
the adapter. A single run, including n=1, never admits an adapter. The measured
model-task pair is the only pair to which that adapter may be applied.

## Candidate attachment seams

The candidate seams are intentionally unresolved:

1. `awesome-kit` `orchestrate`, at brief-authoring time, keyed by the routed
   model. This can shape the brief before dispatch, but its ownership and timing
   must be tested with the first adapter.
2. The `llm-scripting-kit` model-endpoint registry entry, represented by
   `plugins/llm-scripting-kit/lib/llm_scripting_kit/model_endpoints.py`. A
   per-endpoint context is task-agnostic, so it cannot select the required task
   axis by itself; endpoint context alone is the wrong axis.
3. The task skill itself, such as `md-domain`, shipping a per-tier variant.
   This keeps task knowledge near the task, but may make model-specific shaping
   less reusable.

The first built adapter and its measurement decide among these seams. Until
then, none is the design's selected owner.

## Adapter artifact contract

```yaml
adapter:
  identity:
    model_selector: model id or capability tier measured for this adapter
    task_id: stable identifier for the task
  content:
    - compact rule-id checklist placed near the relevant file
    - few-shot examples of correctly cited findings
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

The pack is roughly 0.8-6k tokens per file. Across the 59-file motivating
corpus, its minimum, median, and maximum sizes were 3.3k, 11.6k, and 24k
characters, and it was built in under 5 s. Each audit row records the pack's
sha256 and size so a result is reproducible.

## Anti-patterns

- Leak adapter text into the strong model's prompt. That changes the control and
  taxes a model that did not need the intervention.
- Call a task-wide prompt defect an adapter. Fix the task prompt for every model.
- Adopt an adapter on n=1 or on a result with no twice-run control.
- Apply an adapter to a model that was not measured to need it.

## Status and first build

The first adapter is the `md-audit` EVIDENCE PACK. It was motivated by the
diagnosis of a 59-file audit: local-model misses concentrated in facts absent
from a single-shot prompt. Only 1 of 25 ancestor-duplication cases was found,
0 of 23 code claims were confirmed, cited paths and line anchors needed
resolution, and 30 of 38 mechanical-hygiene cases were missed. Cloud auditors
ran in a read-only filesystem, while their outputs cited repository facts
absent from their brief. The pack pre-computes, deterministically, the facts a
filesystem would have yielded and supplies them to the local model only.

The pack is routed by PATH and records the artifact identity, role, direct code
files, and whether the code-directory dimension applies. Its sections are
identity, measurements, references, ancestors, claim evidence, and mechanical
checks, as specified in the contract above. It costs zero extra model calls
and roughly 0.8-6k tokens per file. In the 59-file corpus it was built in under
5 s; measured pack sizes ranged from 3.3k to 24k characters, with a median of
11.6k.

The measurement instrument canonicalizes rule ids, credits aliases, and
rejects section-number citations. Ground truth is reviewer-confirmed with
locations. The frozen paired corpus has 53 files. The control run was repeated
twice without the adapter: recall was 0.237 and 0.217, a 2-point noise band.
The adapter run was also repeated twice. The ship rule was recall +15 points
with confirmed precision no more than 3 points lower. Result: four adapter arms
(two verbose, two compact) scored recall 0.323, 0.318, 0.328 and 0.298 against
the controls' 0.237 and 0.217, with precision 0.84-0.88 against 0.84 -- a
replicated gain of about 9 recall points with precision kept, below the +15
bar. The compact form costs 36 percent fewer characters for the same gain.
Two variants lost: a stricter pack whose budgets dropped the size measurements
that fed the largest family of wins, and a pack carrying "do not report"
rules, which suppressed reporting across every family on a model that was
already too conservative. Prohibitions do not belong in an adapter for a
recall-limited model; positive evidence does.

Completeness is the adapter's responsibility. A miss caused by a fact the
pack did not carry is a pack defect, not a model limit: the pack must remove
the need for tool calls. The check is a harness run with read-only tools over
the same brief -- anything the tool loop looks up that the pack did not carry
gets compiled into the next pack version.

A further seam candidate: a job specification (job-kit) that declares the
files and generators a unit needs, materialized into the query by the runner
per endpoint kind -- inlined for a transport endpoint, listed as readable for a
harness endpoint. The completeness guarantee then lives in the job file.

The seam decision remains deferred. Because the pack is produced by a
deterministic prepass, the current evidence favours attaching it at the
task skill (`md-domain`), keyed by model tier. That is not the decision: it
waits on the measurement. Until then, this record remains DRAFT and the
adapter is not admitted. The pack's sha256 and size are recorded on every
audit row to make each result reproducible.
