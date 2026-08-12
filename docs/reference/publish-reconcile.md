# Publish reconcile + landing-page preview procedures

Rare-event procedures extracted from the root CLAUDE.md (2026-07-22 md-audit).
Read when: doing a full dev/master reconcile, syncing master's infra drift, or
previewing the marketplace landing page against dev work. The always-on publish
rules stay in CLAUDE.md; `scripts/publish.py` remains the source of truth for
the publish flow itself.

## dev -> master reconcile: conflict-resolution policy

A full `dev`/`master` reconcile (the "publish: reconcile master with dev"
release) conflicts because both branches independently edit the same files
(marketplace.json, plugin.json versions, CLAUDE.md, .gitignore, skills). `dev`
is the source of truth for a reconcile -- master's divergent commits are prior
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
- **`published: false` plugins** (agent-glue, workflow-kit): dev-only by
  design and filtered out of `marketplace.json` by the regenerator, so their
  divergence never reaches consumers -- take dev and move on; don't agonize
  over their conflicts.

Mechanics: `git checkout master && git merge --no-commit --no-ff dev`, resolve
each conflict per the rules above (`git checkout --theirs <file>` takes dev
while on master; `git rm` honors a dev-side delete), then
`python scripts/regen_marketplace.py`, run `pytest tests/bootstrap` +
`regen_marketplace.py --check`, commit the merge, and push master. The
back-port-then-clobber rule is what makes the wholesale "dev wins" resolution
safe rather than blind.

## Master infra-drift sync (periodic, no version bumps)

The publish flow cherry-picks feature commits (plugin code + version bumps) to
master; it never carries not-tied-to-a-feature changes -- a CLAUDE.md gotcha, a
new test file, a `.gitignore` tweak, dev tooling. Master silently falls behind
dev on repo infrastructure. This is expected (per-publish scoping causes it),
not a bug -- reconcile it from time to time. Do it in the **master tree**,
against `origin/dev`'s committed state (never the live dev working tree),
keeping dev-only plugins back:

```bash
git diff --name-only origin/master origin/dev \
  | grep -vE '^(plugins|tests)/(agent-glue|workflow-kit)/' \
  | xargs git checkout origin/dev --
```

Then confirm no dev-only plugin content leaked
(`git diff --cached --name-only`), run the brought tests, commit, push master.
No version bumps, no `marketplace.json` change -- pure infra sync, so consumers
are unaffected. Skip the master->dev merge-back when the dev tree is being
actively edited: the content already matches on both branches, so the history
merge can wait for a calm moment.

## Landing-page preview (dev-tree regen by hand)

At publish time the index.html regen is `publish.py`'s job -- never hand-run it
there. The manual sequence exists for **previewing** the page against dev work
(`claude-dev` / `pk-dev` do the same installPath rewrite for a whole session):

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

**Always restore dev-tree mode.** Leaving it on silently repoints every plugin
at the working copy for all later sessions -- a footgun far worse than a stale
page.

**Every flag is load-bearing -- a regen without them produces a page worse
than the published one, and `--marketplace` produces one that leaks.** Without
`--marketplace plugins-kit` the page carries every OTHER marketplace with a
`poster.yaml` installed on this machine -- on a box holding a private
marketplace, committing that publishes it (observed: 23 plugins across 2
marketplaces instead of 15 across 1). `--public` drops the on/off/installed state badges,
which describe the generating machine rather than the marketplace; omit it and
a checked-in page carries this box's `"state": "on"/"unmanaged"` values (and
loses the flow-to-content-height CSS). `--marketplace-json` overrides the
listing that the phantom-install filter reads: the **cached** `marketplace.json`
under `~/.claude/plugins/marketplaces/` lags the source by one publish, so a
plugin added in the current release is absent from it and gets dropped from its
own release's page. That filter exists to catch plugins *removed* upstream; it
misfires on ones *added*. `--poster` does the same for the marketplace's own
`poster.yaml` (subtitle, url), which the cached clone lags identically, and
`--config` takes the page copy from `.claude-plugin/index-page.yaml` instead of
the per-machine `~/.claude/.local-data/awesome-kit/plugin-ecosystem-poster.yaml`.
`publish.py` passes all of them, and its `verify()` re-parses the generated page
to refuse a foreign marketplace or embedded machine state.

**Preview vs publish -- same mechanism, different commit rule.** At publish
time dev is the about-to-be master, so its page is the published page --
commit it. Outside a publish, dev contains skills and versions not going out,
so the page renders a marketplace that does not exist yet -- look at it, then
`git restore index.html`. The rule is not "never commit a dev-tree page"; it is
"only commit one whose content is being published in the same commit."

**Equivalence note.** The dev-tree regen is byte-identical to the old
post-merge regen (verified 2026-07-15 on the bootstrap 0.40.0 release by
generating both ways and diffing) -- the dev tree and the freshly-published
cache are the same content; only the path differs.
