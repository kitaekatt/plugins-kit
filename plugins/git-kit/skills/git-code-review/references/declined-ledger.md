# Declined-findings ledger

Reviews re-run against the same change re-surface findings the author already
declined -- both generic code-review issues and md-domain subject-lens findings.
`git-code-review` keeps a small ledger so a re-run renders those previously-declined
findings COLLAPSED instead of re-litigating them. The ledger is advisory memory,
NOT a gate: it never changes a verdict, only whether a finding is re-asked.

Shared implementation: `bootstrap_lib.code_review.ledger` (consumed by both
git-code-review and p4-code-review via prepare_review.py, like the rest of the
pipeline). This doc is the operational detail behind step 9's collapse region
and the post-decision `--ledger-record` step.

## Change identity + baseline

- `change_id` (`bundle.change_id`) = the diff range spec (e.g. `origin/main..HEAD`). It is the outer ledger
  key -- entries are bucketed per change.
- `baseline` (`bundle.ledger_baseline`) = the range base SHA (`git rev-parse <base>`). Every recorded entry
  stores the baseline it was declined at; on a later run prepare_review recomputes
  the current baseline and emits only entries whose baseline STILL MATCHES as
  `bundle.ledger_hits`. When the baseline moves the entry is stale and the finding
  re-surfaces (it is re-asked, and `record_declined` prunes it).

## The key (aligned with skills-kit attribution)

A finding is keyed by criterion/reason + taxonomy + a NORMALIZED anchor -- never
line numbers, never exact wording (both churn on trivial edits):

- code-review issue: `file` + `reason` (`bug`|`claude_md`) + normalized-`description` anchor.
- md-domain finding: `file` + `criterion` + `taxonomy` + normalized-`message` anchor.
  (Its wire `kind` in `declined.json` is the literal `md_audit` -- the value
  `bootstrap_lib.code_review.ledger` keys on. Do not rename it.)

The normalized anchor is the lowercased first 8 alphanumeric tokens of the
message/description. File paths are lowercased + posix-slashed for matching.

**Limits (know them).** The anchor is a lossy fingerprint. Two distinct findings
that share file + criterion/reason and open with the same 8 tokens collapse to one
key (false merge); a finding reworded in its FIRST 8 tokens gets a new key and
re-surfaces (false miss). Both degrade only to "asked once more" / "not re-asked
once" -- never to a wrong verdict -- which is why the ledger is advisory.

## Collapse (step 9)

For each current issue / md-domain finding, compute its key and check
`bundle.ledger_hits`. On a match, render it COLLAPSED under a one-line
`previously declined (N): <labels>` note in its own section and do not re-ask it
in the decision pass. EXCEPTION: a **SERIOUS** md-domain finding is NEVER collapsed
-- it always renders and is always decided. (SERIOUS findings are never written to
the ledger in the first place, so a hit can never exist for one; the collapse rule
is belt-and-braces.)

## Record (post-decision step)

After the decision pass, collect the findings the author DECLINED (code-review
issues rejected + md-domain remediations not applied), write them to
`<bundle.bundle_dir>/declined.json`, and run:

    ${CLAUDE_PLUGIN_ROOT}/scripts/prepare_review.py --ledger-record <bundle.bundle_dir>/declined.json

The payload is `{change_id, baseline, declined:[{kind, file, ...}, ...]}` using
`bundle.change_id` and `bundle.ledger_baseline`. `--ledger-record` computes keys
deterministically, drops SERIOUS md-domain findings, prunes stale entries for the
change, and dedups by key. NEVER hand-edit the ledger JSON -- always go through
`--ledger-record`.

## Storage

A single JSON file in the plugin's version-independent data dir, a sibling of the
per-change bundle dirs: `~/.claude/plugins/data/plugins-kit/git-kit/reviews/ledger.json`. Never written into the user's repo
working tree. Shape:

    {"version": 1, "changes": {"<change_id>": {"entries": [<entry>, ...]}}}
