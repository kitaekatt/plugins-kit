# Reviewer B bakeoff

The corpus is read from `scripts/review-bakeoff/corpus/`. Results are written
to `scripts/review-bakeoff/results/`. Run these commands from the repository
root.

1. Run an endpoint arm:

   `python3 scripts/review-bakeoff/run_bakeoff.py run --arm qwen38-5090-harness`

2. Create prompts for an Agent alias:

   `python3 scripts/review-bakeoff/run_bakeoff.py prompts --arm sonnet`

3. After each Agent returns its raw JSON issue array, ingest it:

   `python3 scripts/review-bakeoff/run_bakeoff.py ingest --arm sonnet --case CASE_ID --json result.json`

4. Score one or more arms:

   `python3 scripts/review-bakeoff/run_bakeoff.py score --arm qwen38-5090-harness --arm sonnet`

Alias arms use the prompts and ingest steps because `run_review_lane.py`
rejects Agent-tool aliases. The prompt file includes the canonical system and
user messages, so the Agent receives the same prompt as an endpoint arm.
