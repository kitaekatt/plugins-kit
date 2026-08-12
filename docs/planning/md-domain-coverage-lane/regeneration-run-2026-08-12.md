# The first completed 0.52.0 generation run, and what it measured

Status: settled measurement of ONE run over ONE subtree, with its confound
stated. This is the first time compose -> verify -> apply has completed at all.

Subject: skills-kit 0.52.0, loaded from the installed plugin cache
(`~/.claude/plugins/cache/plugins-kit/skills-kit/0.52.0/`), not from a working
copy. The session that published 0.52.0 never restarted and ran on cached
0.51.0 throughout, so before this run nothing about the release had been
observed working.

Corpus: `godot/` from `D:/dev/woodworking-sim` at `af947fc`, copied by
`git archive` into a scratch tree, its 11 CLAUDE.md files snapshotted as a
control and then stripped. The scratch tree got its own git repo so
VCS-ignore semantics matched. Nothing was written to woodworking-sim.

## Why this subtree

The plan's pre-registered expectations are stated over `godot/`, so the
subtree was fixed by the plan rather than chosen after the fact. The
candidate counts confirmed it before the run: 88 candidates across 12
assessed directories, of which **13 name `godot/` as their destination and 1
names `godot/extensions/woodkernel/`** -- matching G3's table (13 / 1) exactly.

## What ran

`workflow/claude-md-generate.js` from the installed 0.52.0, dispatched via the
Workflow tool over 16 composition subjects (12 with a coverage report, 4
code-free). 21 agents, 0 errors, ~2.43M subagent tokens, ~33 minutes.

The 12 coverage reports were the EXISTING ones from the corpus campaign, with
only their paths rebased onto the scratch tree. Candidate content was
untouched, so the candidate set is identical to the one the earlier loss was
measured against. See the confound below -- this choice is what makes the
result a controlled comparison AND what limits it.

## P2 -- the enumeration rule (PASSES)

`discover_composition.py` was run in anger for the first time, on two real
trees. On the corpus it returns 16 composition subjects including `godot/`,
`godot/extensions/`, and `godot/extensions/woodkernel/` marked *code-free;
composed from children only*. Those are precisely the three directories that
were not subjects under the old rule, which is why 14 admitted facts had a
destination no run could ever write. `godot/art/` is correctly NOT a subject
(assets, no code beneath), so the rule is not simply admitting everything.

`godot/CLAUDE.md` and `godot/extensions/woodkernel/CLAUDE.md` both exist after
the run. That is the P2 precondition, and it holds at the artifact level and
not merely at the enumerator level.

## Quantity 1 -- the mechanical wave record (HEALTHY)

| | |
|---|---:|
| candidate hoists proposed | 12 |
| dispositions returned | 12 |
| `hoist-verified` | 8 |
| `hoist-rejected` | 4 |
| `hoist-unverifiable` | 0 |
| hoists applied | 6 |

Every proposed candidate got exactly one disposition, so the run is in the
"phase ran" row of the plan's four-state table, not the "entered and did not
complete" row. The 4 rejections each cite the refusal condition named in the
candidate's own `check.expected`, over bounded reads of 1-23 files. The plan
warned that a phase rejecting nothing is the shape a rubber stamp takes; a 4/12
rejection rate with stated mechanical results is evidence the phase is doing
work.

## Quantity 2 -- content outcome

Expression of the same 88 candidates, scored by reading the documents:

| Verdict | Control (pre-fix documents) | Regenerated (0.52.0) |
|---|---:|---:|
| EXPRESSED | 74 (84.1%) | 74 (84.1%) |
| PARTIAL | 1 (1.1%) | 4 (4.5%) |
| **ABSENT** | **13 (14.8%)** | **10 (11.4%)** |

**Loss fell from 14.8% to 11.4%, a 3.4 point drop.**

The control number is worth stating on its own, because it is a better
baseline than the retired 23% corpus headline: in the control, **every one of
the 13 losses was a fact destined for a parent document that could not exist**
(12 -> `godot/`, 1 -> `woodkernel/`). Where the destination document existed,
loss was essentially zero -- the whole `scenes/` subtree scored 34 expressed,
1 partial, 0 absent. So the pre-fix loss on this subtree was 100% structural,
which is exactly the class P2 and Option C target.

What the fix actually recovered:

- the `woodkernel/` deployment fact (hand-run `build.bat`, committed
  `.dll`, `reloadable = false`) landed, correctly hoisted to
  `godot/extensions/woodkernel/CLAUDE.md`;
- of the 12 `godot/`-destined facts, roughly half now land, 5 of them carried
  in `godot/CLAUDE.md` itself;
- the residue is 7 still absent at `godot/scripts`, plus 2 at `godot/tests`
  and 1 at `godot/scenes` that the control did not have.

One of the `godot/tests` absences (`tests#8`) names a repo-root `CLAUDE.md`,
which is outside the chosen subtree and unreachable by construction. It is a
scope artifact of this test, not a lane defect; excluding it puts the
regenerated loss at 9/88 (10.2%).

## The confound, stated plainly

**This run was fed PRE-AMENDMENT coverage reports.** Their candidates still
carry non-local destinations (`godot/CLAUDE.md`, `CLAUDE.md (repo root)`) --
the retired PROMOTE-era vocabulary that G12 identified and that the settled
model says coverage should no longer emit. Under the settled model a child
writes the fact locally and the PARENT hoists it from the child's finished
document.

So a fact whose report told the child "this belongs to your parent" can still
be deferred by the child, and a fact the child never wrote down is invisible to
a parent that composes from child DOCUMENTS. That is the most likely mechanism
behind the residual 7, and it is visible in the control too, where
`godot/scripts/CLAUDE.md` explicitly said such facts were "not restated here".

Consequence: this measures the fix's effect on OLD reports. It does not
measure the intended end-to-end configuration, which needs coverage re-run
under 0.52.0 so destinations are local. **The 11.4% should be read as an upper
bound on the residual loss, not as the fix's final number.**

## A defect this run found

**A composition that takes the null branch silently discards its verified
hoists.** `godot/assets/` has no direct code, so everything available to it was
a hoist from a child. It set `written: false` and said so explicitly in its
notes -- "the document that should exist here is exactly the set of five
candidates below, and it should be written by the run that follows their
verification". Verification then ran and verified 2 of its 5 candidates. Those
2 were never written, because the apply step is gated on
`t.verified.length && t.r.written` (`claude-md-generate.js:933`).

So 8 hoists were verified and only 6 applied, and nothing counts or logs the
difference. The affected shape -- a code-free composition subject whose entire
content would be hoists -- is exactly the shape the P2 enumeration rule was
added to bring into scope, so this is not a rare corner.

By contrast `godot/extensions/` took the null branch correctly and for a stated
reason (a single child, where a document could only restate it), which is the
behaviour the model wants.

## Smaller observations

- `houseStyle` is documented as a `claude-md-generate.js` arg but is not
  consumed anywhere in the executable body of the shipped 0.52.0 script.
- A coverage report cited `docs/AUDIO_BRIEF.md` as an invariant-discovery input
  and leaned on it in four rationales. The file does not exist anywhere in the
  corpus. The composer verified this and dropped the affected clauses rather
  than carrying them, which is the right behaviour, but a report citing a
  nonexistent file as a read input has unknown reliability elsewhere.

## What this does and does not establish

Established: 0.52.0 loads and runs; the enumerator works on real trees; P2
brings the previously-unwritable parents into scope and they get documents; the
propose -> verify -> write phase completes, discriminates, and refuses with
reasons; and measured candidate loss on this subtree falls 14.8% -> 11.4%.

Not established: the fix's effect in the configuration it was designed for
(local-destination reports), the result on any other subtree, and anything at
corpus scale. One run, one subtree, old reports.
