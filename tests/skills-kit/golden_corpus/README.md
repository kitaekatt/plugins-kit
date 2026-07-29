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
- `expected-lanes/` -- one recorded JSON golden per artifact family
  (`skill-lanes.json`, `claude-md-lanes.json`, `project-doc-lanes.json`,
  `references-lanes.json`): the LLM detect/classify lane output over the
  same fixtures, same `<CORPUS>` normalization, `perFile` sorted by path.
  Recorded 2026-07-28 against skills-kit 0.35.0 by invoking each member's
  `workflow/detect.js` (references: `classify.js`) via the Workflow tool
  with the models the scripts pin (opus, high effort). See "Lane goldens"
  below.
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

Lane goldens (`expected-lanes/`, recorded 2026-07-28, skills-kit 0.35.0):
the LLM detect/classify lane verdicts over the same fixtures. Unlike
`expected/`, these are NOT compared byte-for-byte by pytest -- LLM output
varies run to run. They are the reference record for the md-domain
restructure: the folded lanes must reproduce the per-file VERDICTS
(COMPLIANT / NON-COMPLIANT / NOT-AUDITED) and the FAIL-level criteria hits
on the same fixtures; finding wording and PASS-row detail are informative
only. Recorded verdicts: skill s1/s5 COMPLIANT, s2/s3/s4 NON-COMPLIANT;
claude-md c1/c4 COMPLIANT, c2/c3/c5 NON-COMPLIANT; project-doc CLAUDE.md
NOT-AUDITED (the PD-1 routing decline) with cited.md/orphan.md COMPLIANT;
references: both scanner findings classified I_illustrative/FIX.
Re-record the same way: stage fixtures (corpus_runner.stage), run each
member's `workflow/detect.js` (references: `classify.js`) via the Workflow
tool with the args contract in each script's header, normalize with the
`corpus_runner.normalize` contract, sort `perFile` by path.

NOT covered: the remediate lanes (they edit files; the corpus is
detection-only).

## Re-recording

```bash
uv run python tests/skills-kit/golden_corpus/record.py
```

Every changed line under `expected/` is a behavior change the next release
ships -- review the diff, do not blanket-accept it.
