# Publish, reconcile, and landing-page procedures

The publish flow and its adjacent procedures, extracted from the root CLAUDE.md
(2026-07-22 md-audit; publish mechanics added 2026-08-31). Read when: publishing
a release, authoring a commit-scoped pre-commit check, clearing a
master-only-content refusal, or previewing the marketplace landing page against
dev work. The safe-publish gotchas, recovery procedure, and cache-version trap stay
in CLAUDE.md. `scripts/publish.py` remains the source of truth for the publish
flow itself.

## Publishing changes

The plugin cache syncs from the remote repository's default branch, not the
local working copy. Develop on the `dev` branch; merge to `master` only when
releasing a version bump. `master` is the cache source, so this branch policy
prevents same-version divergence between the cache and the working copy.

**How.** Commit the code and version bump on `dev`, then:

```bash
uv run python scripts/publish.py            # preflight, publish, verify
uv run python scripts/publish.py --check    # preflight only; no writes, no pushes
```

**`scripts/publish.py` is the source of truth for the flow** -- steps, guards,
and post-verification live in code so this document cannot drift from what
actually runs. Read its module docstring for the mechanics. Do not hand-run the
steps; the script exists because three of them are easy to get wrong in ways
that fail silently (a page generated from the build machine's installed
plugins instead of the repo; a merge that publishes a dev-only plugin; an
`index.html` that lands outside the release commit).

**Definition.** "Publish" means **all** of: version bump + regenerated
`marketplace.json`, regenerated `index.html` inside the release commit, `dev`
pushed, and the release landed on `master`. Anything less is not a publish
-- a bump without the master merge, a bare `git push`, or a master merge without
a bump each leaves consumers on the release in their cache. `publish.py` refuses
each of these rather than half-shipping.

**Publication completeness is conditional on the manifest.** A plugin whose
manifest has `published: true` (or no `published` key) is complete when it is
included in the release. A plugin whose manifest has `published: false` is
complete when its files are held back from the release. The latter is an
intentional dev-only state, not a missing publication and not a publish error.

`.claude-plugin/marketplace.json` is **derived data** -- rebuilt from each
plugin's `plugin.json`, filtered by `"published"` (missing = `true`; `false` =
excluded). Never hand-edit its plugin entries; the pre-commit hook rejects
drift.

`scripts/regen_marketplace.py` also takes its own `--only <plugin>`
(repeatable), distinct from `publish.py --only` below: it rewrites ONLY the
named plugins' entries from their `plugin.json`, leaving every other entry
byte-identical to what is already in `marketplace.json`. Use it to commit one
plugin's version bump while another session's plugin.json in this shared
tree holds an unstaged bump of its own -- a bare regen would rebuild every
entry from disk and pick that up too. An unknown plugin name exits non-zero.

### Partial release: `--only <plugin>`

`uv run python scripts/publish.py --only <plugin>` (repeatable) ships one
published plugin and holds every other published plugin at master's content.
It is the same projection as the dev-only hold-back with a larger hold-back
set -- the projection holds back the dev-only plugins plus every published
plugin not named -- so it cannot conflict and is idempotent for the same
reasons. Three things differ from a bare publish, each on purpose:

- **The derived artifacts are regenerated from the projected tree**, which
  lives only in the projection's temporary Git index.
  `regen_marketplace.regenerate(from_index=True)` reads that index for
  `marketplace.json`; for `index.html`, the ~50 generator inputs (each
  plugin's `plugin.json`, `poster.yaml` and `SKILL.md` files, plus
  `.claude-plugin/`'s page files) are extracted with `git checkout-index` into
  a scratch directory that is removed afterwards, and `generate.py --registry`
  reads them there. So master's `marketplace.json` and `index.html` describe
  the tree master is about to hold: the named plugins at their new versions,
  the held-back ones at the versions master still carries. Nothing on dev is
  regenerated or committed -- the dirty gate admits uncommitted work inside
  held-back plugins, and a dev-side regen would read those working-tree
  manifests while `commit_derived` would sweep another session's staged work
  into the publish commit. `verify()` therefore judges master's artifacts
  against master's manifests rather than dev's; dev's `index.html` catches up
  at the next bare publish.
- **The publish range does not advance.** The projection commit carries
  `Published-Only:` and `Built-From:` trailers, not `Published-From:`, so
  `range_base()` ignores it and the held-back plugins' commits stay in the
  range. The next bare publish sees them as the bumps they are, and the
  per-plugin bump gate still judges their files. (Stamping `Published-From:`
  would drop them from the range and let an unbumped change ship later under
  a version consumers already hold -- gotcha 3.)
- **The gates judge only what ships.** A bump is required on a named plugin
  (a bump elsewhere is not this release's bump); a held-back plugin changed
  without a bump does not block; uncommitted work INSIDE a held-back plugin
  does not block, for the same reason dev-only work never did. Uncommitted
  work anywhere else still refuses.

Trying it: `--check --only <plugin>` is the dry run (preflight only, no
writes). After a real run, the script's own `verifying:` step is the
acceptance test -- it reads master's `marketplace.json` and `index.html`
back and checks every published plugin against the version master's
`plugin.json` carries. A run that prints `published.` passed; nothing needs
checking by hand.

What it deliberately does not do: check cross-plugin coupling. A plugin that
consumes a shared library another plugin owns (`shared_lib_imports`, or a
`dependencies` edge) can be shipped ahead of that library's change, and the
consumer's venv would then resolve the older library. The bare publish is the
one that cannot do that. Use `--only` for a self-contained change, and read
the range first as gotcha 1 requires.

`--only` is also the route when an UNRELATED plugin blocks a bare publish --
preflight refuses because another plugin changed files without a version
bump (observed: unreal-kit, 2026-09-24). Ship the ready plugins with `--only
<plugin>` (repeatable) and leave the unbumped plugin to its owner; the range
does not advance, so the held-back plugin's commits ship whole at the next
bare publish.

### Commit-scoped generated-data checks

The pre-commit check is **index-aware and scoped to the commit**
(`regen_marketplace.py --check --staged`): it judges the staged blobs and stays
out of the way when a commit stages neither `marketplace.json` nor any
`plugin.json`. A worktree-wide check would block every commit over an in-flight
bump it did not contain, while still passing an inconsistent pair that was
staged, since history is built from the index. `publish.py` regenerates and
re-verifies before pushing, so drift cannot reach `master`. A bare `--check`
(without `--staged`) keeps the full worktree behavior for standalone and CI use.

This is the convention for every check in
`scripts/pre-commit-version-check.sh`, not a quirk of one check. A check must
judge the commit (the git index) and must return success when the commit stages
none of its inputs. `scripts/_gitindex.py` is the shared implementation:
`classify_scope` returns `SCOPE_SKIP`, `SCOPE_INDEX`, or `SCOPE_WORKTREE`; do not
re-copy its helpers into a check. When the index cannot be read, fall back to
the worktree loudly -- an unavailable input must never read as a pass.

Two facts are worth not rediscovering:

- **`git commit -- <paths>` is safe under this, and it was established
  empirically.** That form commits the working-tree contents of the named paths,
  so a `--cached` check looks as if it would see an empty staged set and skip.
  It does not: git builds a temporary index holding those paths' contents and
  exports it to hooks as `GIT_INDEX_FILE`, so the checks see exactly the commit.
  This matters because `git commit -F <msg> -- <paths>` is the form used in a
  shared tree.
- **Test a check against a temporary `GIT_INDEX_FILE`, never the real index** --
  staging things to prove a hook works is how another session's work gets
  committed.

**A dev-only plugin's FILES are held back unconditionally.** `_publish_projection`
derives the dev-only set from the MANIFESTS (`local_plugins()` + `is_published`),
not from the exclusion set, so the hold-back runs on every projection whether or
not `--exclude-dev-only` was passed. For each held-back plugin the projection
removes its prefix from the temporary index and, where `master` carries that
prefix, reads master's subtree back in (`git read-tree --prefix`). So a file
`master` already carries is restored to master's content, and a file only
`dev` has is absent from the projected tree. Both halves of a plugin count --
`plugins/<name>/` and `tests/<name>/`.

Read that consequence carefully, because it is the opposite of what "ships by
default" suggests. A dev-only plugin's COMMITS are not excluded from the release
(they appear in the shipping list), but its FILES do not move. So `master`'s tree
does NOT match `dev` for such a plugin, and a copy already on `master` is
re-checked-out on every release -- it can only go stale, never forward. Removing
it from `master` once is what makes the hold-back start deleting it instead,
because master then has no subtree to read back in.

`--exclude-dev-only <plugin>` therefore does NOT control the file hold-back. It
governs commit bookkeeping only: which commits land in `excluded` (hence the
mixed-commit refusal below), the "Held back on dev" line in the projection commit
message, and eligibility for the fast-forward shortcut.

With an exclusion in force, one case is still refused: a single commit touching
**both** that plugin and files that would otherwise ship. Excluding it withholds
released work, including it defeats the exclusion, and splitting someone else's
commit is a judgment call. Split it, or drop the plugin from
`--exclude-dev-only`.

**What the script will NOT do:** decide that a plugin's `published` status has
changed. That edit is yours.

## dev -> master reconcile: master-only content

A release is a PROJECTION: `publish.py` computes master's next tree from dev's
committed tree (holding back dev-only plugins) with git plumbing, so it never
merges and never conflicts. What remains of "reconciling" is the case
`publish.py` refuses on purpose: master carries content dev lacks
(`_require_no_master_only_content`, which names the paths). Landing dev's tree
over it would silently drop that content, so the guard stops the publish
instead.

The remedy happens entirely in the project folder, on dev -- no merge, no
second checkout, no branch switch:

- For each path the refusal names, run `git diff dev origin/master -- <path>`
  and read the `+` lines (content master has that dev LACKS).
- **Generated / JSON files** (`marketplace.json`, every `plugin.json`,
  `index.html`): nothing to keep -- dev's versions are >= master's by
  construction, and the publish regenerates the derived files.
- **Non-generated text** (`.gitignore`, `CLAUDE.md`, `*.md`, `*.py`, etc.):
  **back-port** any `+` lines worth keeping to dev and commit them there. If
  there are none, dev is already a superset. (In practice most of these are
  textual-only: dev carries master's content via a different commit.)
- **`published: false` plugins**: their files are held back on every
  projection, so their divergence never reaches consumers -- take dev and move
  on. Read the current set from the field rather than from memory.

Then run a normal `publish.py`. The back-port-then-project order is what makes
"dev wins" safe rather than blind.

A hand merge is exactly how content goes wrong without a conflict: on
2026-09-07 a merge-based reconcile left
`plugins/llm-scripting-kit/lib/llm_scripting_kit/completion/capabilities.py`
with the "canonical guarantee subjects" block landed twice and the `BYPASS`
docstring stranded between the copies, because the file merged cleanly and was
never inspected. A projection cannot do that: it takes dev's blobs rather than
combining two sides.

## Master infra drift (retired procedure)

Every bare publish projects dev's WHOLE tree, so repo infrastructure -- a
CLAUDE.md gotcha, a test file, a `.gitignore` tweak, dev tooling -- reaches
master with the next release, so no separate infra-drift sync exists. A
`--only` release holds everything outside the named plugins at master's
content, and the next bare publish carries it.

Master's history contains hand-made sync and reconcile commits from before the
projection release (`53645fd2`, 2026-08-27). `range_base()` in `scripts/publish.py` searches DOWN master's
history for the most recent `Published-From:` trailer (bounded by
`_RANGE_BASE_SEARCH_DEPTH`) rather than reading master's tip, so those
untrailered commits do not hide the publish boundary.

## Publishing rationale

Narrative and rationale behind the root CLAUDE.md's operative publish
guardrails.

**Publication hold rationale.** A publication hold on ONE plugin belongs in
root CLAUDE.md, not in a task folder: a release ships the whole range, so a
hold anywhere a publisher does not read binds nobody. Recorded because it was
tested and failed: a secrets-kit hold was kept in a task folder through
2026-09-16 and four separate publishes (0.8.25, 0.8.26, 0.8.27-0.8.29, 0.8.30)
carried the plugin to `master` anyway, each by a session that had no reason to
open that folder and did nothing wrong. The hold was later accepted as
overtaken rather than retracted.

**Safe-publish gotcha explanations.**

- Gotcha 1 (ship the whole range): what the mandatory range check is actually
  for -- knowing what went out so a bad release can be traced (the main
  reason, sufficient on its own); catching a plugin that changed without a
  version bump (preflight also catches this, but seeing it in the range first
  is cheaper than reading a refusal); and confirming the dev-only hold-back
  covers what it should. Earlier revisions of this rule told you to STOP when
  the range held anything beyond your own commits, and to escalate the choice
  to the user; both are retired, because they made every release wait on a
  quiet tree, which a shared tree never is, and asked the user to adjudicate
  readiness the pushing session had already declared.
- Gotcha 2 (`git add` sweeps pre-existing modifications): the dev tree is a
  live workspace, so the index may already hold another session's (or your
  own earlier) staged work before you touch it -- `git add <your files>`
  followed by `git commit` commits the ENTIRE index, not just the files you
  named, so a pre-staged rename or WIP rides along under your commit message.
  This is how a `workflow-glue -> workflow-kit` rename once landed inside an
  unrelated test-coverage commit. `git diff --staged` is the only guard: run
  it every time and confirm the staged set is exactly your files.
- Gotcha 3 (burned version numbers): cache entries on consumer machines key
  off `(plugin, version)`, so retracting a bad version doesn't evict caches
  that already pulled it -- same version means same code forever, from the
  cache's view. The 0.11.1 / "patch-bump 4 plugins to force-refresh
  post-retraction caches" commits on master are an example of this recovery
  pattern.

**Submit gate background.** The gate verifies every changed plugin is
version-bumped since the last publish, each stated pyproject version matches
plugin.json, and marketplace derived data matches the manifests. This exists
because the cache keys on version: the same version means the same code
forever, so a `bootstrap.json` change without a bump is structurally invisible
to consumers (manifest edits count as code edits), and fresh installs between
releases copy HEAD code under the old version string (silent divergence).
`plugin.json` and `marketplace.json` versions must move together, enforced by
the regenerator plus `scripts/pre-commit-version-check.sh`. Never copy files
directly into the plugin cache, and do not omit the version field hoping for
rolling updates -- Claude Code substitutes a git SHA that becomes a static
cache key anyway.

## Landing-page preview

The repo-root **`index.html`** is the marketplace's public landing page (the
GitHub-Pages-style poster listing every plugin and its skills). It is generated,
not hand-edited, by awesome-kit's plugin-ecosystem skill. `scripts/publish.py`
invokes that generator, and `regenerate()` in that script carries the flags and
is the source of truth for the invocation.

Repo-side inputs for the page are all under `.claude-plugin/`:
`marketplace.json` (the listing), `poster.yaml` (the marketplace subtitle and
URL), and `index-page.yaml` (the page copy).

At publish time the index.html regen is `publish.py`'s job -- never hand-run it
there. To **preview** the page against dev work, call the same function the
publish calls, so the preview cannot drift from the shipped flag set:

```bash
uv run python -c "import runpy; runpy.run_path('scripts/publish.py')['regenerate']()"
git diff --stat -- index.html .claude-plugin/marketplace.json   # look, then:
git restore index.html .claude-plugin/marketplace.json          # unless publishing them
```

`regenerate()` writes a synthetic registry naming each `plugins/<name>`
directory with its own `plugin.json` version into a temporary directory,
passes it as `generate.py --registry`, and removes it afterwards. Nothing under
`~/.claude` is rewritten, so there is no mode to restore.

**Every flag is load-bearing -- a regen without them produces a page worse
than the published one, and `--marketplace` produces one that leaks.** The
generator's default job is to describe the machine it runs on, not the public
marketplace. Six flags redirect its inputs at the repo:
`--registry`, `--marketplace`, `--public`, `--marketplace-json`, `--poster`,
and `--config`. `--registry` supplies the plugin inventory and versions from
the repo's own manifests instead of `~/.claude/plugins/installed_plugins.json`
and the plugin-cache fallback, which describe what THIS machine has installed.
`--marketplace` is the one whose omission **leaks rather than misreports**:
without `--marketplace plugins-kit`, the page carries every OTHER marketplace
with a `poster.yaml` installed on the machine, including private marketplaces,
into the public repository (observed: 23 plugins across 2 marketplaces instead
of 15 across 1). `--public` drops the on/off/installed state badges,
which describe the generating machine rather than the marketplace; omit it and
a checked-in page carries the generating machine's `"state": "on"/"unmanaged"`
values and loses the flow-to-content-height CSS. `--marketplace-json` overrides
the listing that the phantom-install filter reads: the **cached**
`marketplace.json` lags the source by one publish, so a plugin added by the
release is absent from it and gets dropped from that release's page. That filter
exists to catch plugins *removed* upstream; it misfires on ones *added*.
`--poster` does the same for the marketplace's own `poster.yaml` (subtitle,
URL), which the cached clone lags identically. `--config` takes the page copy
from `.claude-plugin/index-page.yaml` instead of the per-machine
plugin-ecosystem poster configuration. `publish.py` passes all six flags, and
its `verify()` re-parses the generated page to refuse a foreign marketplace or
embedded machine state.

**At publish time this is `publish.py`'s job -- do not hand-run it.** A bare
publish regenerates in the project folder and lands `index.html` *inside* the
release commit, so `master` is never in a state where its page disagrees with
its own `marketplace.json`. A `--only` publish regenerates from the projected
tree instead (see "Partial release" above).

**Preview vs publish -- same mechanism, different commit rule.** At publish
time dev is the about-to-be master, so its page is the published page --
commit it. Outside a publish, dev contains skills and versions not going out,
so the page renders a marketplace that does not exist yet -- look at it, then
restore it. The rule is not "never commit a dev page"; it is "only commit one
whose content is being published in the same commit."

**Equivalence note.** The `--registry` regen of `origin/master`'s tree
reproduces master's committed `marketplace.json` byte for byte, and its
`index.html` differs only in the order of the embedded plugin array
(alphabetical rather than registry order). The page sorts that array on load,
so the rendered page is identical (verified 2026-09-16 when `--registry`
replaced the `dev-tree.py` flip).
