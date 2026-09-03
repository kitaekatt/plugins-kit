# reviewer_b bakeoff -- on-disk contract

Measures ONE thing: how well a given model performs the
`reviewer_b_diff_only_bugs` lane, precision and recall reported separately,
with validator accuracy deliberately excluded (no validator runs).

The canonical lane prompt is
`plugins/bootstrap/bootstrap_lib/code_review/lane_prompts.py`
(`LANE_PROMPTS["reviewer_b_diff_only_bugs"]` + `build_user_message`). Every arm
sends that same prompt. Nothing here restates or paraphrases it.

## Corpus

`corpus/<case-id>/chunk.diff` -- one unified diff, the exact bytes a lane
receives as its chunk. Self-contained: a reviewer sees this and nothing else.

`corpus/<case-id>/case.yaml`:

```yaml
id: off-by-one-loop-bound      # == directory name
kind: positive                 # positive | decoy
files: [src/total.py]          # repo-relative paths appearing in the diff
planted:                       # exactly one entry for positive, [] for decoy
  - file: src/total.py
    lines: "3-6"               # the added-line range the bug occupies
    summary: "range(len(v)+1) indexes past the end"
rationale: |
  Why a correct reviewer_b MUST report this (positive), or MUST NOT report
  anything (decoy), quoting the clause of the lane prompt that decides it.
```

## Scoring (fully mechanical -- no judgment at scoring time)

A reported issue MATCHES a planted bug when its `file` equals the planted
`file` AND its `lines` range overlaps the planted `lines` range by at least one
line. That is the whole matching rule, which is why a positive case carries
EXACTLY ONE planted bug at a known range: multi-bug cases would need semantic
matching and reintroduce judgment.

Per arm:

- `recall` = positive cases with a matching issue / positive cases
- `precision` = matching issues / all issues reported across all cases
- `decoy_fp_rate` = decoy cases with >= 1 reported issue / decoy cases
- `positive_noise` = issues on positive cases that matched nothing / all issues
  reported on positive cases

`decoy_fp_rate` is the headline number. The stated risk this bakeoff exists to
test is that a smaller model degrades first as NOISE, which reads as a working
review; recall alone cannot see that.

## Arms

- An llm-scripting-kit endpoint id (e.g. `qwen38-5090-harness`) runs through
  `plugins/git-kit/scripts/run_review_lane.py`.
- An Agent-tool alias (`sonnet`, `opus`) CANNOT run through that script -- it
  exits 2 on an alias by design. The harness emits a ready-to-send prompt file
  per case for those arms and ingests the returned JSON arrays; the orchestrator
  fans them out as Agent subagents, which is the shipped dispatch path.

## Results

`results/<arm>/<case-id>.json` -- the lane envelope (endpoint arms) or the raw
issue array (agent arms). `results/<arm>/summary.json` -- the four metrics plus
counts. Results are scratch output, not tracked.
