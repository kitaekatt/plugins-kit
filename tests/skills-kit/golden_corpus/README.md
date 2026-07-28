# skills-kit golden-output corpus

Regression corpus for the md-audit member skills: fixture documents with
seeded, categorized defects, plus the recorded output of every MECHANICAL
audit surface over them. Built ahead of the md-domain restructure so the
restructure has a harness proving the folded lanes produce the same verdicts
the member skills did -- built after, it could only bless whatever the
restructure happened to do.

## Layout

- `fixtures/` -- committed inputs, one directory per artifact family:
  - `skill/` -- five SKILL.md cases: valid floor, schema FAIL (missing
    steps), mixed-type (forbidden key), unreachable reference, legacy prose.
  - `claude-md/` -- five CLAUDE.md cases: classic with contract block,
    code-directory (review claims + code siblings), gotcha-prose-only
    (classic; locks the narrowed Signal-B), conventions-only (union floor
    pass), empty block (union floor FAIL).
  - `project-doc/` -- a mini project tree with one cited and one orphan doc.
  - `references/` -- a `.claude/skills` pool with a resolving soft ref, a
    broken soft ref, and a broken hard dep (in prose: fenced blocks are
    masked by design).
- `expected/` -- one recorded JSON golden per case, machine-independent
  (staged paths normalized to `<CORPUS>`, forward slashes).
- `corpus_runner.py` -- staging + per-case runners + normalization (shared).
- `record.py` -- re-records `expected/`; run only for intended changes and
  review the diff like code.
- `test_golden_corpus.py` -- pytest: every case twice (idempotency) and
  against its golden.

## Covered / not covered

Covered (deterministic layer): `skills_kit_lib.audit` reports for skill and
CLAUDE.md fixtures, claude-md-audit `discover.py` role + dimension
classification, project-doc-audit `discover.py` citation/orphan signals,
`references_audit.py` findings.

NOT covered: the LLM detect/remediate lanes. These fixtures are their
intended inputs too -- before the md-domain restructure, run the member
audits over `fixtures/` in a live session and record the lane verdicts; the
folded lanes must then reproduce them on the same fixtures. Lane goldens
cannot be recorded from pytest (they require Workflow-tool sessions with
pinned models).

## Re-recording

```bash
uv run python tests/skills-kit/golden_corpus/record.py
```

Every changed line under `expected/` is a behavior change the next release
ships -- review the diff, do not blanket-accept it.
