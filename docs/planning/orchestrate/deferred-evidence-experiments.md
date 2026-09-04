# Deferred evidence experiments for orchestrate

Status: retained ledger, recorded 2026-09-04.

This document preserves the seven deferred evidence gaps from task
`orchestrate-2.0`. The source ledger was section 7, "Known evidence gaps", of
the deleted `tier-principles.md` source, which commit `4cb4d96c` deleted on
2026-08-26. That commit replaced the generated routing policy and its generator
with hand-written schema-3 configuration. The change of policy authorship does
not answer the evidence questions: the corresponding routing surfaces remain in
`plugins/awesome-kit/skills/orchestrate/defaults/orchestration.yaml`.

On 2026-09-04, all seven gaps are classified as **kept**. None is obsolete
because of the retraction. Each experiment below states the evidence that
would close it.

## 1. Codex capability benchmarks [kept]

The former principles selected `gpt-5.6-luna` and `gpt-5.6-sol` mainly from
dispatch shape and the absence of a counter-case, not measured capability.
Run a fixed, blinded corpus across the relevant Codex models and a comparison
seat, with identical briefs, task-specific quality checks, failure rates,
latency, and token cost; repeated results that establish where each model
wins would close the gap.

## 2. Pool-consumption measurements [kept]

The fable guard treats its usage pool as a separate exhaustible budget, but the
former evidence was structural rather than quantitative. Collect dispatch,
capacity, fallback, and exhaustion data across multiple reset windows and
workload mixes, then show whether the guard predicts pool protection and where
the policy should change; that measured relationship would close the gap.

## 3. Narrow `multi_agent_v2` measurement [kept]

The recorded test used N=8 trivial items on codex-cli 0.146.0, measured 65
seconds versus 48 seconds, and did not resolve token accounting, so it says
nothing about independent long-running items. Compare enabled and disabled
parallel execution across several N values, item durations, output sizes, and
failure modes, with wall time, tokens, throughput, and recovery recorded; a
repeatable result for the intended workload would close the gap.

## 4. One-off usage telemetry [kept]

The 2026-08-21 tally came from one machine's session transcripts, excluded
policy examples, and counted fable 23, opus 117, sonnet 153, and Codex 56
announcements; it did not capture inline work or provide a continuing feed.
Persist unit id, matched row, target, fallback, outcome, and enough workload
denominator data across machines and dates, including inline units, so routing
frequency and quality can be compared over time; that feed would close the gap.

## 5. Agent-type effectiveness [kept]

The policy assigns Claude-side roles such as `Explore`, `Plan`, and
general-purpose, but no evidence shows whether `Explore` outperforms a
general-purpose agent at the same model rung. Randomize matched read-only
tasks between the roles and blind-score correctness, completeness, useful
compression, latency, and cost; a significant, repeatable difference or a
defensible no-difference result would close the gap.

## 6. P0.3 volume threshold [kept]

The hand-written policy retains P0.3: for known, insufficient work, author one
specification for many units and execute across them, but "many" has no
threshold. Run matched workloads at increasing unit counts and task families,
comparing one specification against per-unit specification for authoring time,
execution time, errors, rework, and total cost; the measured crossover by task
family would define the threshold and close the gap.

## 7. Claude-side answer for fan-out [kept]

The Codex-absent policy still discloses that genuine fan-out has no dedicated
Codex-side pull and tells the reader to sequence units or handle them inline.
Evaluate a Claude-side parallel mechanism against sequencing and inline work,
including isolation, coordination, failure recovery, quality, latency, and
capacity effects; either a safe routing rule or evidence that sequencing or
inline handling is the correct explicit answer would close the gap.
