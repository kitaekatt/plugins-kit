# Publish, reconcile, and landing-page procedures

The publish flow and its adjacent procedures, extracted from the root CLAUDE.md
(2026-07-22 md-audit; publish mechanics added 2026-08-31). Read when: publishing
a release, authoring a commit-scoped pre-commit check, doing a full dev/master
reconcile, syncing master's infra drift, or previewing the marketplace landing
page against dev work. The safe-publish gotchas, recovery procedure, and cache-version trap stay
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
that fail silently (a half-restored dev-tree that makes a session load
plugins from the working copy; a merge that publishes a dev-only plugin; an
`index.html` that lands outside the release commit).

**Definition.** "Publish" means **all** of: version bump + regenerated
`marketplace.json`, regenerated `index.html` inside the release commit, `dev`
pushed, and `master` fast-forwarded and pushed. Anything less is not a publish
-- a bump without the master merge, a bare `git push`, or a master merge without
a bump each leaves consumers on the release in their cache. `publish.py` refuses
each of these rather than half-shipping.

`.claude-plugin/marketplace.json` is **derived data** -- rebuilt from each
plugin's `plugin.json`, filtered by `"published"` (missing = `true`; `false` =
excluded). Never hand-edit its plugin entries; the pre-commit hook rejects
drift.

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

**Dev-only commits are EXCLUDED, not a reason to refuse.** When
`origin/master..dev` holds commits touching a dev-only (`published: false`)
plugin, the script publishes a **filtered release**: it replays only the
shippable commits onto `master` in a temporary worktree and pushes from there,
printing every commit it held back. `dev` is untouched and keeps that work.
`published: false` records that the plugin does not go to consumers, so the
script honors that decision per commit.

None of that runs by DEFAULT any more. Every dev-only plugin's commits ship, so
`master`'s tree matches `dev` and `published: false` does its whole job on its
own -- filtering the plugin out of `marketplace.json`, which is what makes it
uninstallable. Source on a public `master` that nobody can install is the
intended end state, and it is where these plugins already sat: an exclusion
never held for long, because a plugin's files arrive with the first commit that
touches anything else.

The filtering above is therefore opt-in, per plugin, via
`--exclude-dev-only <plugin>` -- for a plugin whose SOURCE must not appear on a
public `master` at all.

With an exclusion in force, one case is still refused: a single commit touching
**both** that plugin and files that would otherwise ship. Excluding it withholds
released work, including it defeats the exclusion, and splitting someone else's
commit is a judgment call. Split it, or drop the plugin from
`--exclude-dev-only`.

**What the script will NOT do:** decide that a plugin's `published` status has
changed. That edit is yours.

## dev -> master reconcile: conflict-resolution policy

A full `dev`/`master` reconcile (the "publish: reconcile master with dev"
release) conflicts because both branches independently edit the same files
(marketplace.json, plugin.json versions, CLAUDE.md, .gitignore, skills). `dev`
is the source of truth for a reconcile -- master's divergent commits are
publish/reconcile artifacts that `dev` supersedes. Resolve **toward dev**, with
one guard that prevents silently dropping a master-only fix:

- **Generated / JSON files** (`marketplace.json`, every `plugin.json`,
  `index.html`): clobber with dev unconditionally. `plugin.json` versions are
  dev >= master by construction; `marketplace.json` is regenerated from them
  anyway (`scripts/regen_marketplace.py` after the merge); `index.html` is a
  post-publish regen.
- **Non-generated text** (`.gitignore`, `CLAUDE.md`, `*.md`, `*.py`, etc.):
  first run `git diff dev origin/master -- <file>` and inspect the `+` lines
  (content master has that dev LACKS). If any are important, **back-port them
  to dev first** (commit on dev), then clobber with dev. If there are no
  master-only lines, dev is a superset -- clobber with dev directly, no loss.
  (In practice these conflicts are usually textual-only: dev already contains
  master's content via a different commit, so the `+` set is empty and the
  clobber is safe.)
- **`published: false` plugins**: dev-only by design and filtered out of
  `marketplace.json` by the regenerator, so their divergence never reaches
  consumers -- take dev and move on; don't agonize over their conflicts. Read
  the current set from the field rather than from memory; the field is the
  load-bearing record and this list has gone stale before.

Mechanics, in a **master worktree** -- never `git checkout master` in the shared
dev tree, which silently redirects whatever a concurrent session commits next
(root CLAUDE.md, "Anti-pattern: creating a branch"; the worked incident is
`shared-tree-git-discipline.md`):

```bash
git worktree add ../plugins-kit-master origin/master
cd ../plugins-kit-master
git merge --no-commit --no-ff origin/dev
```

Resolve each conflict per the rules above (`git checkout --theirs <file>` takes
dev while on master; `git rm` honors a dev-side delete), then
`python scripts/regen_marketplace.py`, run `pytest tests/bootstrap` +
`regen_marketplace.py --check`, commit the merge, and push master. Remove the
worktree when done (`git worktree remove ../plugins-kit-master`). The
back-port-then-clobber rule is what makes the wholesale "dev wins" resolution
safe rather than blind.

## Master infra-drift sync (periodic, no version bumps)

The publish flow cherry-picks feature commits (plugin code + version bumps) to
master; it never carries not-tied-to-a-feature changes -- a CLAUDE.md gotcha, a
test file, a `.gitignore` tweak, dev tooling. Master silently falls behind
dev on repo infrastructure. This is expected (per-publish scoping causes it),
not a bug -- reconcile it from time to time. Do it in a **master worktree** (`git worktree add <dir> origin/master` -- never
`git checkout` in the shared dev tree, which redirects concurrent sessions' commits),
against `origin/dev`'s committed state (never the live dev working tree),
keeping dev-only plugins back:

```bash
# Derive the dev-only set from the field, reading ORIGIN/DEV -- never the
# checked-out master tree, and never hardcode plugin names here. A dev-only
# plugin that does not exist on master yet is absent from the master tree, so
# deriving there yields an incomplete set and the filter below leaks that whole
# plugin onto master.
DEVONLY=$(git ls-tree -r --name-only origin/dev \
  | grep 'plugins/.*/\.claude-plugin/plugin\.json' \
  | while read -r f; do
      git show "origin/dev:$f" | python3 -c "
import json,sys
d = json.load(sys.stdin)
print('$f'.split('/')[1] if d.get('published', True) is False else '')"
    done | grep . | paste -sd'|' -)
test -n "$DEVONLY" || { echo "refusing: empty DEVONLY"; exit 1; }

git diff --name-only origin/master origin/dev \
  | grep -vE "^(plugins|tests)/(${DEVONLY})/" \
  | xargs git checkout origin/dev --
```

Then confirm no dev-only plugin content leaked
(`git diff --cached --name-only`), run the brought tests, commit, push master.
No version bumps, no `marketplace.json` change -- pure infra sync, so consumers
are unaffected. Skip the master->dev merge-back when the dev tree is being
actively edited: the content already matches on both branches, so the history
merge can wait for a calm moment.

## Landing-page preview (dev-tree regen by hand)

The repo-root **`index.html`** is the marketplace's public landing page (the
GitHub-Pages-style poster listing every plugin and its skills). It is generated,
not hand-edited, by awesome-kit's plugin-ecosystem skill. `scripts/publish.py`
invokes that generator, and `regenerate()` in that script carries the flags and
is the source of truth for the invocation.

Repo-side inputs for the page are all under `.claude-plugin/`:
`marketplace.json` (the listing), `poster.yaml` (the marketplace subtitle and
URL), and `index-page.yaml` (the page copy).

At publish time the index.html regen is `publish.py`'s job -- never hand-run it
there. The manual sequence exists for **previewing** the page against dev work:

```bash
uv run python scripts/dev-tree.py dev        # installPaths -> this working copy
python plugins/awesome-kit/skills/plugin-ecosystem/scripts/generate.py \
  --marketplace plugins-kit --title "plugins-kit marketplace" \
  --marketplace-json plugins-kit=.claude-plugin/marketplace.json \
  --poster plugins-kit=.claude-plugin/poster.yaml \
  --config .claude-plugin/index-page.yaml \
  --output ./index.html --public --no-open
uv run python scripts/dev-tree.py normal     # ALWAYS restore, even if the regen failed
uv run python scripts/dev-tree.py status     # confirm: installPaths @ cache: <n>, not 0
```

Keep this in step with `regenerate()` in `scripts/publish.py`, which is the
source of truth for the flag set; a preview built with fewer flags is not
previewing the page that will ship.

The `claude-dev` helper performs the same `installPath` rewrite for a whole
session rather than a single regen; `scripts/dev-tree.py` is what both use.

**Always restore dev-tree mode.** Leaving it on silently repoints every plugin
at the working copy for every session that starts while dev-tree mode remains
enabled -- a footgun far worse than a stale page.

**Every flag is load-bearing -- a regen without them produces a page worse
than the published one, and `--marketplace` produces one that leaks.** The
generator's default job is to describe the machine it runs on, not the public
marketplace. Five flags redirect its inputs at the working copy:
`--marketplace`, `--public`, `--marketplace-json`, `--poster`, and `--config`.
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
plugin-ecosystem poster configuration. `publish.py` passes all five flags, and
its `verify()` re-parses the generated page to refuse a foreign marketplace or
embedded machine state.

**It crawls installPaths, not the working directory.** `generate.py` reads the
installed-plugin registry and walks each plugin's **`installPath`**, filtered by
`marketplace.json`. In a normal session those paths point at the **cache**,
which only refetches from **master** -- so a plain regen before the merge
reproduces the cache-derived page rather than the dev-tree page. **Registry v2
caveat:** Claude Code's registry-v2
format keeps that registry at `{"plugins": {}}`; awesome-kit 0.10.0 and above
has `generate.py` fall back to scanning the cache layout for refs the registry
does not record, so a normal-mode regen renders the machine's cached plugins
rather than an empty page.

**At publish time this is `publish.py`'s job -- do not hand-run it.** The script
repoints `installPaths` at the working copy via `dev-tree.py`, which in
awesome-kit 0.47.0 and above also **synthesizes** entries for repo plugins the
registry does not record (the registry-v2 case). It regenerates, restores in a
`finally`, and post-verifies that the restore landed. It also lands `index.html`
*inside* the release commit, so `master` is never in a state where its page
disagrees with its own `marketplace.json`. The manual sequence in
"Landing-page preview" above is for **previewing** only.

**Preview vs publish -- same mechanism, different commit rule.** At publish
time dev is the about-to-be master, so its page is the published page --
commit it. Outside a publish, dev contains skills and versions not going out,
so the page renders a marketplace that does not exist yet -- look at it, then
`git restore index.html`. The rule is not "never commit a dev-tree page"; it is
"only commit one whose content is being published in the same commit."

**Equivalence note.** The dev-tree regen is byte-identical to the
post-merge regen (verified 2026-07-15 on the bootstrap 0.40.0 release by
generating both ways and diffing) -- the dev tree and the freshly-published
cache are the same content; only the path differs.
