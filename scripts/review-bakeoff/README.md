# Reviewer B bakeoff

The corpus is read from `scripts/review-bakeoff/corpus/`. Results are written
to `scripts/review-bakeoff/results/`. Run these commands from the repository
root.

1. Run an endpoint arm. `--arm` is the registry id passed to the lane as
   `--model`, with no prefix:

   `uv run python scripts/review-bakeoff/run_bakeoff.py run --arm qwen38-5090-harness`

2. Create prompts for a `harness: claude` id:

   `uv run python scripts/review-bakeoff/run_bakeoff.py prompts --arm sonnet`

3. After each Agent returns its raw JSON issue array, ingest it:

   `uv run python scripts/review-bakeoff/run_bakeoff.py ingest --arm sonnet --case CASE_ID --json result.json`

4. Score one or more arms:

   `uv run python scripts/review-bakeoff/run_bakeoff.py score --arm qwen38-5090-harness --arm sonnet`

`harness: claude` arms (fable, opus, sonnet, haiku) use the prompts and ingest
steps because `run_review_lane.py` does not dispatch them; `run` refuses them. The prompt file includes the canonical system and
user messages, so the Agent receives the same prompt as an endpoint arm.
