# The fresh-reports run: 0.53.0 criteria end to end, confound closed

Status: settled measurement of ONE end-to-end run (fresh coverage ->
generation -> scoring) over the godot subtree, in the configuration the
settled model was designed for -- LOCAL destinations only. This supersedes
the confound stated in regeneration-run-2026-08-12.md: that run fed
pre-amendment reports; this one does not.

Subject: the DEV working copy at commit 15fc84e (skills-kit 0.53.0,
unpublished at run time) -- coverage-detect.js, claude-md-generate.js,
coverage-standards.md all loaded from the dev tree via wrapper workflows, so
this run is also the ONE REAL DISPATCH the lane-shipping rule requires for
the 0.53.0 claude-md-generate.js prompt edits.

Corpus: godot/ from D:/dev/woodworking-sim at af947fc, git-archived into a
scratch tree with its own git repo, 11 control CLAUDE.mds snapshotted and
stripped. Same construction as the prior run.

## What ran, and cost

1. discover_coverage.py x 12 directories (the same 12 the 88-candidate
   answer key assesses) -> 12 subjects, empty ambient chains.
2. coverage-detect.js (dev tree), depth=advanced, criteria = the 0.53.0
   coverage-standards.md: 12/12 lanes, 0 errors, ~2.3M subagent tokens,
   ~16 min. 122 candidates, no ceiling hits.
3. discover_composition.py -> 16 composition subjects (matches the prior
   run exactly; godot/art correctly excluded).
4. claude-md-generate.js (dev tree): 23 agents, 0 errors, ~3.0M tokens,
   ~60 min. 14 documents written, 2 recorded null branches (godot/assets,
   godot/extensions), 121 sections. Hoist ledger fully accounted: 14
   proposed -> 7 verified / 7 rejected / 0 unverifiable; 33 child claims
   considered-and-not-proposed with reasons.
5. Scoring: 3 agents against the old 88-candidate answer key; 3 agents
   against the fresh 122; 1 decomposition agent. ~0.7M tokens.

## Result 1 -- the amended criteria emit LOCAL destinations only

All 122 fresh candidates name the assessed directory as destination.
Zero non-local. (The old reports: 14 of 88 non-local -- the retired PROMOTE
vocabulary that made the prior run's 11.4% an upper bound.) The
configuration confound is closed.

## Result 2 -- delivery loss in the designed configuration: 2/122 (1.6%)

Expression of the fresh 122 candidates in the documents generated from them:

| Verdict | Count |
|---|---:|
| EXPRESSED (own doc or ancestor) | 112 |
| ACCOUNTED (recorded, reasoned decline) | 8 |
| Unaccounted (silent) | 2 (1.6%) |

The 8 accounted declines each carry a verified reason in droppedCandidates
-- including one candidate whose lead clause the composer PROVED FALSE by
grep (the manifest `loop` key is never read by any script) and corrected
before writing, which is the 0.53.0 restated-rule discipline observably
working.

The 2 unaccounted losses, named: scripts candidate 19 (the Audio
owner-scoped loop rule -- process_mode DISABLED does not stop an
AudioStreamPlayer; stop_owner on deactivate) and tests candidate 3 (seed
0xC0FFEE before defect-coverage assertions). Both subjects returned empty
droppedCandidates and empty notProposed, so nothing accounts for them: the
compose step silently wrote neither. This is the exact gap the deficiency
plan's P3 (per-candidate terminal dispositions as the run's OUTPUT CONTRACT)
was designed to close and which was never implemented -- the lane records
hoist dispositions but does not require a disposition per own-candidate.
2/122 is the measured cost of that gap in this run.

## Result 3 -- the old answer key now mostly measures sampling, not delivery

Scored against the old 88-candidate key: 54 EXPRESSED / 14 PARTIAL / 20
ABSENT (22.7% absent -- HIGHER than the control's 14.8%). Read alone that
number looks like a regression; decomposed per miss it is not:

| Class | Count | Meaning |
|---|---:|---|
| SAMPLING | 24 | No fresh candidate covers the fact -- two independent advanced assessments admitted different samples. A property of coverage (expressly non-idempotent), not of delivery. |
| PLACEMENT | 6 | The fact IS in the corpus, at the leaf that owns it (stations/, tests/, furniture/, powertools/) instead of the old key's PROMOTE-era destination or assessed dir. Under the settled model this placement is the intended one. |
| DECLINED | 1 | Dropped with verified reasons, incl. a real falsifier (shaders/wood.gdshader's second cm-conversion constant refuting a proposed godot/-level units hoist). |
| DELIVERY-LOSS | 3 | Reduce to the SAME 2 orphaned candidates as Result 2 (one is scored twice by the key). Cross-validation: two independent analyses converge on exactly these two. |

Consequence for measurement going forward: the 88-candidate key has served
its purpose. It scored delivery while the delivery configuration was the
question; now that destinations are local, key-vs-corpus deltas are
dominated by assessment sampling variance (24 of 34 misses), and the honest
delivery metric is expression-of-own-input (Result 2). Re-running the key is
no longer informative about the machinery.

## Known defect reproduced, now loudly counted

The null-branch-composer defect (a code-free composition subject taking the
null branch discards its verified hoists) REPRODUCED at godot/assets: 3
candidate hoists, survivors verified, written: false, 1 verified candidate
not applied. Unlike the first observation it is now counted and flagged in
the lane's own summary line ("1 verified candidate(s) were NOT applied to a
document -- review these"). Still open as the task's
null-branch-composer-drops-verified-hoists item; this run adds a second
observation of the same shape.

## Smaller observations

- One internal-coherence wobble: a proposed hoist ("source asset owns
  addresses") was REJECTED at godot/scenes on grounds (unique_id may make
  positional addressing survivable) that sit in tension with three leaf
  documents asserting silent retargeting. The leaf claims match their
  candidates; the tension is between the verifier's reasoning and the leaf
  assertions, not a factual error in any document. Worth an eye at corpus
  scale.
- The escalateToAncestor disclosure path was used 3 times (all in the
  scenes cluster's dropped candidates naming repo-root depth), functioning
  as designed for facts judged to belong above the run's root.

## What this does and does not establish

Established: the 0.53.0 criteria emit local-only destinations on a real
corpus; the 0.53.0 generation prompt runs end to end (the lane-dispatch
shipping requirement is DISCHARGED); silent delivery loss in the designed
configuration is 1.6% with every other non-expression carrying a recorded
verified reason; the restated-rule check observably corrected a false
candidate clause before it reached a document; the null-branch defect still
reproduces and is now counted.

Not established: anything about review effect (no reviewer touched these
documents); behaviour at corpus scale beyond this subtree; the value of the
122-candidate sample vs the 88 (richer or just different -- ungraded).
