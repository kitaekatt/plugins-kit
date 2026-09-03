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

## Anti-patterns

- Leak adapter text into the strong model's prompt. That changes the control and
  taxes a model that did not need the intervention.
- Call a task-wide prompt defect an adapter. Fix the task prompt for every model.
- Adopt an adapter on n=1 or on a result with no twice-run control.
- Apply an adapter to a model that was not measured to need it.

## Status and first build

This record remains DRAFT while the first `md-audit` adapter is built and
measured. The first build must report its model selector, task id, insertion
position, added token and call cost, metric, n, and two control runs before the
adapter is treated as admitted.
