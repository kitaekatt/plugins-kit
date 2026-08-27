# The parked C++ corpus removal test

Owner-designed 2026-08-13, set up, PARKED UNRUN on usage limits. This is the
task's next action. Everything needed to resume without re-deriving the design
or re-doing the recon is here; `dev/tasks/better-md-review/plan.md`
carries only the pointer.

**THE QUESTION IT ANSWERS, in the owner's words:** run the workflow over a
directory tree that already has CLAUDE.md files, put the result in a changelist,
then "review the changelist specifically to identify if it REMOVED STATEMENTS
THAT ARE TRUE THAT ARE LIKELY TO PROVIDE VALUE." Then a second pass: "identify
if any of the likely-to-provide-value statements are ACTUALLY likely to provide
value."

**WHY IT IS TWO STAGES, and do not collapse them.** The admission criteria are
SUPPOSED to remove true-but-worthless statements. A removal is a defect only
when the statement was true AND valuable. Stage 1 finds removals; stage 2 judges
whether each was worth keeping. Without stage 2 the test scores the criteria
WORKING as the criteria FAILING. This is also the first non-void instrument this
task has had on the value vector -- it measures the criteria against real
removals rather than against theory.

**TARGET (selected and verified):**
a large C++ game corpus, in its editor module source tree.
9 code-bearing directories, 92 code files, 3 existing documents, 4 waves:

- wave 0: `Private/ConfigViewer/ShopRotator` (2), `Public/ConfigViewer/ShopRotator` (2), `Test` (1, HAS doc), `UI` (3, HAS doc)
- wave 1: `Private/ConfigViewer` (9), `Public/ConfigViewer` (9)
- wave 2: `Private` (14), `Public` (2)
- wave 3: `.` the root (50, HAS doc) -- composes from 4 children

The 3 directories WITH documents are the only removal-test subjects; the other 6
exercise the new-write path. The counts were corroborated at setup time by the
since-retired `discover_hierarchy.py` (removed in skills-kit 0.56.0)
(`leaves (code directories): 9`, `CLAUDE.md files in tree: 3`); re-corroboration
uses `discover_composition.py`.

**THE BASELINE IS THE DEPOT, NOT A FILE.** Reconstruct the pre-run documents with
`p4 print` at these exact revisions -- a scratchpad copy from the setup session is
gone:

- the corpus root `CLAUDE.md#3` (18 lines)
- the corpus's `Test/CLAUDE.md#1` (34 lines)
- the corpus's `UI/CLAUDE.md#3` (22 lines)

**SEQUENCING, OWNER-ACCEPTED: BASELINE FIRST.** Run the test on the published
skills-kit lane, recording the skills-kit version alongside the baseline number
so the post-adjudicator re-run compares like with like, before building the
entailment adjudicator. The adjudicator is a
stage whose job is to reject claims, so shipping it first makes any removal
unattributable -- we would not know whether it or baseline composition did it.
Run baseline, get the number, THEN build, THEN re-run and compare. The baseline
run also produces the POSITIVE half of the adjudicator's acceptance corpus, which
the ten-claim corpus lacks (it tests rejection only, so a reject-everything stage
scores perfectly against it).

**PERFORCE OPERATIONAL FACTS -- all verified live, do not re-derive:**

- The workspace is P4 (`.p4config.txt` at the corpus's workspace root, no `.git`).
  The owner granted WRITE AUTHORIZATION for it, and wants everything left in a
  NUMBERED PENDING CL, explicitly NOT p4-code-reviewed.
- Existing CLAUDE.md files are READ-ONLY until `p4 edit`. An agent told to
  overwrite one gets `PermissionError: [Errno 13]` -- hard failure, not silent,
  file not truncated. Open them BEFORE dispatch, not mid-run.
- The 6 NEW documents create fine (directories are writable) but land UNTRACKED.
  Nothing in the lane runs `p4 add`. Plan an explicit add pass or they are
  invisible to everyone and lost on a clean sync.
- `p4` resolves its config from the CURRENT DIRECTORY, and `P4CONFIG` is a p4
  registry setting, NOT an OS env var. `p4 <cmd> <abs-path>` run from a directory
  outside the P4 workspace (this repo's root, for instance) fails with `must
  create client '<this machine>'`. Pass `-d <dir
  inside the workspace>` or set cwd. `vcs_ignore._p4_ignored` gets this right
  (`vcs_ignore.py:240`); ad-hoc p4 calls will not.
- **ANOTHER USER HOLDS TWO OF THE THREE.** `Test/CLAUDE.md` and `UI/CLAUDE.md` are open
  in another user's workspace, changelist 138349
  (far behind head 159724, so possibly long-abandoned -- do not assume). The
  owner chose to EDIT THROUGH the collision for full test power; that decision
  stands, but re-check `p4 opened -a` before running, since it may have changed.
  The root `CLAUDE.md` is open by nobody.
- SETUP DONE AND THEN UNDONE: CL 164118 was created and the three files opened,
  then reverted (unmodified) and the CL deleted when the park was called. The
  workspace is clean. Re-create the CL on resume.

**VCS-IGNORE IS FINE ON P4 -- a worry that was checked and closed.**
`vcs_ignore.py` detects P4 via `_p4_config_above` (:109-137) and shells
`p4 ignores -i` (:224-260); it correctly prunes the corpus's `Scripts/uvcache`,
which contains a vendored plugins-kit checkout INCLUDING the deliberately
malformed golden-corpus CLAUDE.md fixtures. Ingesting those would have been
actively harmful. The module's own docstring called the P4 path UNVERIFIED; it
was verified against the live server on 2026-08-13 and that docstring should be
updated (bundle with the next skills-kit change; not worth a bump alone). Only
`--diff` mode remains git-only (discover_coverage.py:612-693, diff_roots), and it fails
loudly -- just do not pass `--diff`.

**THE ONE THING THAT MUST LAND BEFORE THE TEST RUNS:** the P4 ambient-chain fix,
item `ambient-chain-is-git-only`. Without it every subject in that tree gets an
empty or self-only ambient chain, `already-ambient-suppressed` is inert, and the
regenerated documents are written blind to `Source/CLAUDE.md` and `main/CLAUDE.md`
-- the confound would sit directly on the quantity the test measures.
