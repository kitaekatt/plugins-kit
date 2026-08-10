# Porting a Pipeline

The subsystem-first method for collapsing an existing monolithic LLM-batch
tool onto `content_pipeline`. The audience is someone who already has a
working tool -- a single large module (or a handful) that reads authored
content, hashes it for staleness, calls an LLM, validates, and writes results
-- and wants to replace its generic machinery with the shared library while
keeping its behavior provably intact.

The method is not "rewrite against the new API." It is "extract a shared seam,
port one subsystem, prove equivalence with the tool's OWN tests, then move to
the next." Porting in dependency order with a pinned equivalence baseline at
each step keeps risk at a minimum and makes every collapse auditable.

## 0. Inventory the monolith against the 12 subpackages

Before porting anything, map the existing tool's modules onto the library's
subpackages. For each existing module, name the subpackage it collapses onto:

| Concern in the monolith | Subpackage it collapses onto |
| --- | --- |
| Staleness hashing, "needs regen" checks, ensure-on-diff | `freshness` |
| The canonical record, human/machine attribution, regen-preserving merge | `store` |
| Rule checks (in-loop and post-hoc), advisory diagnostics | `validate` |
| Prompt context assembly, per-variant context | `providers` |
| The LLM client wrapper, cache, cost, retry, backend switch | `llm` |
| The generate/apply orchestration; the candidate-iteration loop | `pipeline` |
| Writing results (in place or as artifacts), the VCS choreography | `deliver` / `vcs` |
| Human question/answer or export/intake loops | `roundtrip` |
| Post-hoc coverage / findings / cost reporting | `audit` |
| The command-line facade | `cli` |

The output of this step is a table: every existing module, the subpackage it
maps to, and a one-line note on what will remain project-side after the
collapse (the genuinely project-specific residue -- field schemas, prompt
content, domain rules). A module that maps to nothing in the library is a
signal either that it is pure project residue (keep it) or that it is a
capability the library does not cover (flag it).

Config loading itself -- content-root discovery, reading and caching a config
file -- is the standing example of the first case. The library deliberately
covers no config subpackage: every consumer so far wanted a different root
marker, file layout, and merge order, so config loading stays project-side and
its outputs are passed in through the injection points named below.

## 1. Port freshness first

Port the staleness subsystem onto `content_pipeline.freshness` before anything
else. It is the purely-functional subsystem -- no LLM, no VCS, no I/O side
effects to mock -- so it validates the entire "extract a seam, port one
system, prove equivalence" workflow at the lowest possible risk before you
touch any LLM-bearing subsystem.

Concretely: replace the tool's hand-rolled hashing with `hashing.content_hash`
/ `shared_snapshot` / `combined_hash` / `corpus_hash`; replace its
"needs-regen" branching with the single `classify.classify` +
`needs_generation` predicate; replace its write-on-change logic with
`ensure.ensure`. The tool's own staleness field names map through `classify`'s
`human_field` / `machine_field` / `hash_field` parameters -- no data reshape.

## 2. Pin the existing test suite as an equivalence baseline

Before deleting or collapsing any module, run its EXISTING test suite against
the ported code. A green run of the tool's own tests -- not new tests written
against the library -- is the proof the seam is right. A module is not
"ported" until its own tests pass against the new implementation.

This matters because the library makes deliberate semantic unions where the
two source systems differed (each subpackage's `__init__` docstring records
them in a "Deviations" section). Most are behaviorally invisible -- e.g.
`freshness` canonicalizes strings uniformly rather than preserving a legacy
raw-UTF-8 path, and the pinned property (a bare string and a dict wrapping it
hash differently) still holds. But a few change an observable label (an empty
recorded hash classifies as `STALE`, not `MISSING`). The existing test suite
is what tells you which deviations your tool actually depended on. Where a
test fails on a deviation the library documents as intentional, update the
test to the new contract and record why (step 5); where it fails on an
unintended divergence, the seam is wrong -- fix the port, not the test.

## 3. Port the remaining subsystems in dependency order

With freshness proven, work outward in dependency order, pinning the relevant
existing tests at each step:

1. **`store`** -- the attributed record, `MergePolicy`, candidate cells.
   Lift the tool's hardcoded preservation logic into `MergePolicy` /
   `CollectionMerge` data (field-name lists), so the merge module learns no
   domain field name. Prove with the tool's merge/preservation tests.
2. **`validate`** -- collapse the tool's rule checks onto the `Validator`
   contract and `Severity` tiers. A tool that raised on any violation maps to
   `HARD` + `assert_valid`; a tool with a soft/advisory axis maps to `SOFT` /
   `ADVISORY`. Prove with the tool's validation tests.
3. **The LLM boundary (`llm`)** -- replace the client wrapper, cache, cost,
   retry, and backend switch with `platform.call_llm` / `submit_validated` and
   `backends.route`. This is where a mock seam earns its keep: point the
   tool's LLM tests at `MockBackend` and confirm the validate-until-valid loop
   reproduces the old retry/feedback behavior.
4. **`providers`** -- move prompt-context builders behind the tiered registry
   and single-owner `assembly`. If the tool used opaque per-item labels in
   batched requests, that technique is now `assembly.assign_labels` / `relabel`.
5. **`deliver` / `vcs`** -- collapse the write path onto one delivery mode
   (`inplace` or `projection`) driving an injected `VcsBackend`. The
   changeset choreography (placeholder-up-front, per-item moves, description
   rebuilt from the moved subset, delete-if-empty) is `deliver.deliver_changeset`
   now, not per-backend code. Tests run against `NullVcs`.
6. **`cli` last** -- the facade is ported last because it composes everything
   below it. Decompose the monolithic command function onto `cli.scaffold`
   dispatch, with `cli.budget` / `cli.bulk` / `cli.unsupported` for the
   halt-guard, bulk-warm, and sticky-stub concerns. The thin per-command
   handlers are the only substantial project-side CLI code that remains.

`roundtrip` and `audit` port whenever their prerequisites (`store`;
`store`/`freshness`/`validate`) are done -- they are opt-in and have no
downstream dependents, so they can slot in late without blocking the CLI.

## 4. Re-run the existing suite as the final equivalence oracle

After the full port, re-run the existing project's ENTIRE test suite (not a
new one written against the library) as the final equivalence oracle. The
project's tests encode the real behavior the library implementation must
reproduce; a green library-only test suite is not sufficient proof the port
preserved behavior. Only when the tool's own suite passes end to end against
the ported code is the port complete.

## 5. Record retired surfaces honestly

For every module that collapses, record explicitly what stays in the
project-side binding versus what disappears entirely into the library:

- A module whose generic body was the WHOLE module -- it collapses to "nothing
  remains" -- is deleted outright, not kept as a stub. A stub "just in case"
  is dead code that hedges against a decision already made.
- A module that leaves a project-specific residue (a field schema, a prompt
  template, a domain rule set) keeps only that residue, now wired onto the
  library's injection points (`MergePolicy` data, a `Validator` list, an
  `InplaceSpec`, an `AuditSpec`).
- Where you changed a test to match a documented library deviation (step 2),
  note it: the port log states the deviation, the source-system semantics it
  replaced, and why the new contract is acceptable for this tool.

Honest annotation means the port log is a truthful map of what moved, what
stayed, and what was intentionally dropped -- not a pile of hedged stubs.

## The project-venv consumption note

Once ported, a project imports `content_pipeline` as an ambient package. The
bootstrap `shared_libs` mechanism publishes the library's source to a stable,
version-independent path
(`~/.claude/plugins/data/plugins-kit/_shared_libs/content_pipeline/`)
and registers a `.pth` on the shared standalone interpreter, so a process
running under that interpreter can `import content_pipeline` with no path
work. A consuming plugin that declares `shared_lib_imports:
["content_pipeline"]` gets the same `.pth` written into its own venv.

A project that runs its OWN interpreter (not the shared standalone, and not a
plugin venv the engine linked) needs a **fail-soft shim** to reach the shared
path. The pattern, generically:

```python
# project bootstrap shim -- fail soft, never hard-fail an unbootstrapped machine
try:
    import content_pipeline  # already importable (linked .pth) -- nothing to do
except ImportError:
    import os, sys
    shared = os.path.expanduser(
        "~/.claude/plugins/data/plugins-kit/_shared_libs/content_pipeline"
    )
    if os.path.isdir(shared):
        sys.path.insert(0, shared)
```

Three properties make it fail-soft:

1. **Import-first.** When the `.pth` already exposes the package, the shim is a
   no-op -- it never shadows or double-inserts.
2. **Existence-gated.** It inserts the path only when the shared dir actually
   exists (bootstrap has run). On a fresh, unbootstrapped machine the shim
   does nothing rather than inserting a dead path.
3. **Degrade, do not crash.** A caller that still cannot import after the shim
   should surface a clear "run bootstrap first" message and exit cleanly, not
   raise an opaque `ImportError` deep in a stage. The library is a
   reuse-by-availability dependency: its absence is a provisioning state, not
   a program bug.

The stable path is the contract; pin the shim to it once, in the project's
entry point, and no other module does path work.
