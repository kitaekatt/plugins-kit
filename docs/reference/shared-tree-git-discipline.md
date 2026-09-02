# Shared-tree Git discipline: worked incidents

These incidents show what has actually gone wrong in one shared working tree when
concurrent sessions touched the index or the checked-out branch. The governing
rules remain in the root `CLAUDE.md`: do not alter another session's staged state,
and do not create or switch branches in the shared tree. This document keeps the
detailed narratives available on demand.

The first incident turns on the narrow exception to the staging rule, so the
exception is restated here as orientation (the root `CLAUDE.md` remains the
governing statement). Discarding another session's staged state is permitted
only when ALL of: `git diff HEAD` is empty for those paths; the staged content
is demonstrably superseded by HEAD; and nothing is staged as a deletion or an
untrack. Outside that conjunction the index is assumed load-bearing.

## Unstaging another session's work: the exception in practice

**2026-08-08.** A publish preflight refused on 37 dirty files across four plugins. They looked like
another session's in-flight refactor. They were not: `git diff HEAD` was empty (the
working tree was byte-identical to HEAD), while the index held a pre-HEAD snapshot --
`plugins/unreal-kit/.claude-plugin/plugin.json` staged at `0.11.4` against HEAD's
`0.11.5`, and docstrings staged at wording HEAD had already superseded. Committing that
index would have downgraded a published plugin version and reverted 37 files. Nothing
was staged as a deletion. The index was discarded and the tree went clean. Note the
limit of that check: the empty-`git diff HEAD` and no-deletion clauses were verified
across all 37 paths mechanically, but "superseded by HEAD" was confirmed by reading two
files and generalized to the rest. The clause is only as strong as the sample -- read
enough of the staged diff to be sure, and say what you actually checked. The deciding question is always whether the
index holds information that exists nowhere else. Here it did not -- the staged
content was a stale re-add already in history. Where it does -- a staged deletion
or untrack, which records an intent no file carries -- the exception does not
apply and the index must be left alone.

## Creating or switching a branch

**Worked example (2026-08-08).** A `review-bootstrap-cli` branch was created off
`origin/master` to scope a code review to two commits, avoiding the 15 unrelated
commits sitting in `origin/master..origin/dev`. The intent was good and the review
itself was scoped correctly. But `git checkout review-bootstrap-cli` moved the shared
tree, and a concurrent session then committed twice -- `agent-glue: state the consumer
feedback as requirements` and `repo: drop incidental references to a private consuming
project`. Both landed on the throwaway master-based branch. `git branch --contains`
confirmed they existed on that branch and nowhere else: two commits of another
session's work, stranded, one `git branch -D` away from being unreachable.

Recovery took a commit of in-flight work, three cherry-picks, a content-identity check
per commit, and a force-delete. Nothing was lost, but only because the branch was still
there to find. That is the good outcome, not the expected one.
