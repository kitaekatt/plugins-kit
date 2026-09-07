# Model-task adapters

This is the provisional reference for building adapters in plugins-kit. It is
based on one admitted adapter, the md-audit evidence pack for the local
qwen3.8-27b endpoint, and one negative transfer of that idea to md-domain code
directory generation. Those two results establish a working method, not a
general theory. Amend this reference when another model-task pair supplies
contrary evidence.

The detailed md-audit measurements and design history remain in the
[adapter design record](../planning/adapters/adapter-design.md). Before spending
a run on a candidate, also check the
[negative-results catalog](adapters-negative-results.md).

## Definition

An adapter is task-specific context attached to one model, endpoint, or measured
capability tier doing one task so that it produces better results. Selection has
two independent axes:

1. **Model axis:** the model, endpoint, or capability tier for which the
   intervention was measured.
2. **Task axis:** the exact task whose output and admission criteria the
   intervention serves.

The unadapted model's task prompt remains the control. In particular, an adapter
for a weaker or local model does not leak into the prompt of a stronger model
that did not need it.

An adapter is not:

- a task-wide prompt correction; if every model needs the change, fix the task
  prompt;
- a model-agnostic tool;
- a sampling parameter or repeated-sampling policy; or
- a fine-tune.

A deterministic prepass can produce adapter content, as the md-audit evidence
pack does. The prepass is not the adapter by itself: the measured artifact also
includes the resulting content, its framing, and its location in the prompt.

## Ground rule: corpus independence

Corpus independence is an eligibility rule, not another score. An adapter may
use context computed at request time from the subject artifact and its
repository, generic instructions, and material in the task's own standards.
It may not encode information learned from the evaluation corpus.

Disallowed inputs include ground truth, reviewer verdicts, per-file results,
confirmed-finding exemplars mined from the corpus, frequency-ranked checklists,
negative rules distilled from dismissals, file-specific hints, and calibration
fitted on the corpus. A candidate using any of them is **disqualified**. Do not
run it and describe the outcome as a measured loss; it was never an eligible
adapter. Proxy ground truth is an instrument for scoring, not an input to the
artifact.

Corpus-independent construction does not prove corpus robustness. It merely
makes that claim possible to test on a repository that did not shape the
candidate. The md-audit pack's held-out result is recorded in the
[design record](../planning/adapters/adapter-design.md#measurement-results).

## Empirical admission bar

An adapter begins as a hypothesis. Admit it only after a comparison under the
same task, corpus, request configuration, scoring instrument, and model
selection:

- run the unadapted control at least twice;
- run the candidate at least twice;
- name the metric, scoring instrument, corpus, and sample size;
- preserve the exact prompt framing, position, request configuration, and
  artifact digest for each arm; and
- record added tokens and extra model calls beside the quality result.

Never admit from `n=1`. Use the controls' own inter-arm spread as the first
noise estimate. A between-condition delta smaller than that spread is not a
detectable effect on this instrument. The experiment applied this rule in both
directions: it retracted an apparent generation cost increase and declined a
correct reference-resolution fix whose score did not separate from the
unchanged pack.

Admission is limited to the measured model-task pair and request configuration.
A result for md-audit does not admit the same artifact for generation, another
md-domain lane, a differently configured endpoint, or a model presumed to sit
in the same tier. A tier selector is justified only by measurements that support
the tier as the unit of admission.

## Adapter artifact contract

Every candidate and admitted adapter should leave a record with this shape:

```yaml
adapter:
  identity:
    model_selector: model id, endpoint id, or measured capability tier
    task_id: stable task identifier
  content:
    provenance: request-time, generic, or standards-derived sources
    payload: exact generated or static context
  prompt_position:
    insertion_point: position relative to task instructions, subject, and schema
    wrapper: exact text framing the payload
  cost:
    added_tokens: measured input or total token delta
    extra_calls: model calls added per task unit
  admission_measurement:
    metric: named quality metric and scoring instrument
    sample_size: number of task units and independent arms
    control_runs_without_adapter: 2 or more
    candidate_runs_with_adapter: 2 or more
    request_configuration: exact measured configuration
    comparison: same task and conditions with and without the adapter
  application_rule:
    admitted_model_task_pairs: only pairs supported by the measurement
    behavior_when_unconfigured: adapter absent
```

Eligible payload shapes include generic or standards-derived guidance and
examples, a required scan plan, deterministic structural facts computed from
the current request, an excerpt of the applicable standards, or a decomposition
plan. This is an open list. Provenance and admission, not resemblance to the
first evidence pack, decide whether a future shape is an adapter.

Content, wrapper, prompt position, and request configuration are part of the
measured stimulus. They are not incidental implementation details. Record token
and call cost because the configuration with the highest quality score need not
be the right default; that distinction decided the shipped md-audit profile.

## Ownership and enforcement seam

In plugins-kit, the **task skill owns an adapter end to end**. It owns the
builder or static content, model-task admission lookup, prompt insertion,
measured request configuration, and tests. It enforces the decision at the
emitter that materializes the final prompt or job, where both selection axes
are present.

The shipped example is md-domain's
[`emit_audit_jobs.py`](../../plugins/skills-kit/skills/md-domain/scripts/emit_audit_jobs.py).
It attaches the evidence pack when every preferred endpoint is admitted,
attaches nothing when none is admitted, and rejects a mixed admitted and
non-admitted preference list. Choosing one prompt before a downstream runner
resolves a mixed list would necessarily be wrong for one possible endpoint.

The rejected seam split responsibility in two: the task skill would declare a
condition and each caller would be expected to consume it. No test saw both
sides of that contract. A caller could forget the declaration and still return
a plausible report at the lower control score, with no failure to expose the
omission. Ownership at the emitter turns selection into construction rather
than convention.

Endpoint admission is local configuration with an empty default. Endpoint ids
vary by user and fleet; committing them would both misroute other installations
and publish machine-identifying data. An endpoint's presence in the admitted set
is a claim that the adapter was measured for that endpoint and task. An
unconfigured run must behave exactly as though the adapter does not exist. The
key and mixed-list behavior are documented in
[Configuring md-domain standards](../../plugins/skills-kit/skills/md-domain/references/configuring-standards.md#adapters).

This seam is complete only while it covers every route that can emit the task.
If another lane gains configurable model routing, it becomes another emission
path and must either pass through the same enforcement point or enforce the same
rule itself with an end-to-end test.

## Best practices (as of 2026-09-07)

These are priors from one successful application and one negative
transfer. Keep the headings stable enough for later experiments to confirm,
narrow, or replace them.

### Read the task's output admission criteria first

Ask whether the task would accept the information the adapter plans to supply.
The generation transfer failed this test structurally. Audit criteria required
mechanical lookups that the local model skipped, so supplying those facts paid.
Generation's
[`coverage-standards.md`](../../plugins/skills-kit/skills/md-domain/references/standards/coverage-standards.md)
criterion `absent-fact-earns-ambient-cost` admits only facts a straightforward
local reading does not reveal and explicitly rejects a file inventory. Files,
import graph, dependencies, and entry points computed by the generation pack
were therefore inadmissible by
construction. Prompt tuning could not make those sections become good output.

Read the criteria before building the prepass, not after measuring its prose.
The full negative result is in
[Generation-lane evidence-pack transfer](adapters-negative-results.md#generation-lane-evidence-pack-transfer).

### Diagnose an adapter-shaped gap

Start from repeated misses and identify what information or presentation the
affected model lacks. The md-audit diagnosis found that misses clustered around
repository facts absent from the single-shot prompt: ancestor duplication,
code-claim verification, and mechanical checks. That supported a deterministic
evidence pack. It did not support a generic instruction to try harder.

If the defect is shared by every model, repair the task prompt. If the gain comes
from repeated sampling, expose a sampling choice. If tools are essential, use a
tool-capable execution path. Call the intervention an adapter only when the gap
is specific to one model-task selection and context can address it. See the
[diagnosis behind the first pack](../planning/adapters/adapter-design.md#measurement-results).

### Establish within-condition noise before comparing conditions

Run identical controls before interpreting a candidate delta. Two md-audit arms
of the same condition agreed on only about half of their true pairs, and the two
generation controls differed by 17,605 reasoning tokens. A single comparison
can therefore suggest a large benefit or cost that disappears on repetition.

Treat the inter-arm spread as a noise floor, report both arms rather than only
their mean, and use more repetitions or a stronger instrument when the proposed
effect does not clear it. This is why two controls and two candidate arms are
the minimum admission evidence.

### Freeze and fingerprint the measured stimulus

Correctness is necessary but does not by itself authorize changing an admitted
artifact. A real path-resolution defect in the md-audit pack was fixed correctly
and measured twice, yet its mean F1 differed from the admitted pack by less than
the admitted pack's own arm-to-arm spread. The fix was not shipped. The evidence
and decision are preserved in the
[design record](../planning/adapters/adapter-design.md#known-defect-carried-from-the-prototype).

Hash generated content, preserve the exact wrapper, and treat changes to
content, framing, position, or request configuration as new stimuli. Batch a
correctness repair only with a change that has its own reason to remeasure when
the repair itself has no detectable benefit.

### Record quality and total cost together

Choose the operational profile from quality and cost, not quality alone. On the
md-audit screen, the admitted single-call pack moved F1 from 0.36 to 0.51 for
1.1x the control tokens. Higher-scoring repeated-sample and tool-using rows cost
2.1x to 4.4x. The single-call profile became the default even though it did not
have the highest raw F1. The compact comparison belongs in the
[design record's outcome](../planning/adapters/adapter-design.md#outcome).

Measure cost in the unit the execution actually consumes. Concurrent wall time
against one server was not comparable in this experiment, so the adoption
decision used tokens and calls.

### Test sampling before adding prompt machinery

Check whether independent completions vary more than prompt variants before
building more context machinery. The largest recall gain in md-audit came from
the union of independent samples, not from another prompt section. That is a
useful diagnosis even when repeated sampling is not the default.

Do not silently convert this finding into a doubled run. Re-auditing was already
available to callers, while a built-in two-sample default would charge every run
2.1x. Keep one call as the measured default and let the caller buy more recall
when a particular run warrants it.

### Treat completeness as a testable adapter obligation

For an evidence adapter intended to replace repository lookups, a miss caused by
an absent fact is an adapter defect, not automatically a model limit. Run the
same brief through a read-only tool-using harness. Compile facts the tool loop
needed but the adapter omitted into the next candidate, then remeasure it.

Completeness does not mean including every fact the harness touched. The
transcript-mined variants showed that some evidence was already present but
unused, and more rows could dilute attention. The harness identifies candidate
gaps; admission criteria and repeated measurements still decide what enters the
artifact. See
[Transcript-mined lookup packs](adapters-negative-results.md#transcript-mined-lookup-packs).

### Validate on an untuned corpus before claiming corpus robustness

The ground rule prevents direct corpus leakage, but design choices can still fit
the corpus that suggested them. Repeat the control and candidate on an untuned
repository before claiming the effect is corpus-robust. The compact md-audit
pack replicated on a held-out repository; an expanded cross-file variant did
not produce the same net advantage there. Neither result broadens admission to
another task or model.

### Enforce selection at one prompt-emission seam

Put model-task selection where the final prompt or job is materialized and give
that component end-to-end ownership. Test admitted, unadmitted, and mixed model
selections there. Inventory every other route capable of emitting the same task;
a bypass invalidates the enforcement claim.

Avoid declaration-only APIs that depend on every caller remembering to inspect
and honor a flag. Their failure mode is silent quality regression rather than a
useful error.

### Verify the published path end to end

An adapter is not done when its builder works or its source is published. Run a
real task through the published artifacts, routing layer, final emitter,
transport, response validator, and admission configuration. Assert that the
adapter actually attached and that the accepted result came from the intended
endpoint.

The first such md-audit run exposed three independent integration failures that
unit and prototype measurements had not:

1. A hidden routing floor required one harness's control id, so transport and
   alternate harness endpoints were unroutable. The repair expressed the safety
   requirement as an effect each backend could guarantee.
2. The emitted job told a toolless transport to open three files even though the
   measured prompt had inlined them. The repair made the production payload
   match the measured execution shape.
3. The job omitted the measured request configuration, so a 4,096-token default
   was consumed by reasoning before any answer. The repair carried the admitted
   configuration through the job.

Also verify that local admission configuration exists. The first shipped
version had an empty default as intended, but no local configuration admitted
the target endpoint, so merely inspecting published source did not prove the
adapter had ever attached.

### Keep endpoint admission local and empty by default

Do not commit hostnames, fleet endpoint ids, or local routing names. Ship the
configuration schema and an empty admitted set. A user or fleet configuration
can then bind the measured model-task claim to its local endpoint name, while an
unconfigured installation retains the exact control behavior.

Fail loudly when one emitted job mixes admitted and unadmitted endpoints. An
adapter selected before runtime routing cannot be correct for both.

### Ship only the admitted profile

Keep losing, disqualified, and diagnostic variants in the experiment rather
than in production switches. Shipping them enlarges the support surface and
invites an unmeasured combination to become a de facto configuration. The
md-audit implementation retained the compact winning builder and left the
experimental profiles behind; the closed variants and their lessons are
cataloged in [Adapter experiments that did not pay](adapters-negative-results.md).

When a future variant wins, replace or version the admitted artifact together
with its measurement record. Do not accumulate a menu of profiles whose names
outlive the evidence that distinguished them.
