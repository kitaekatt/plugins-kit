# clean-code-kit tool forks and pins

Maintainer reference for the metric tools clean-code-kit runs: where the
code comes from, how the plugin pins it, and how a pin moves. Consumers of the
plugin never need this; it lives here rather than under
`plugins/clean-code-kit/` because nothing in it is read on a machine that only
installs the plugin.

Facts below marked with a date were observed on that date against the forks on
GitHub and the local clones.

## The forks

Each tool is used from a fork under `github.com/kitaekatt`, never from
upstream directly.

| Tool | Fork | Upstream | Used by |
|---|---|---|---|
| dryer | https://github.com/kitaekatt/dryer | https://github.com/unclebob/dryer | the duplication lane |
| crapper | https://github.com/kitaekatt/crapper | https://github.com/unclebob/crapper | the complexity lane |
| mutator | https://github.com/kitaekatt/mutator | https://github.com/unclebob/mutator | no lane in step 1 (mutation testing is not part of it) |

### The practice: fork, fix in the fork, then open the PR upstream

When a tool needs a change for clean-code-kit, the change is made in the fork
on its own fix branch cut from upstream's default branch, merged into the
fork's integration branch, and pinned. The upstream pull request is opened
from the same fix branch afterwards. The plugin never waits on upstream
review, and upstream receives each change as one reviewable branch.

As of 2026-11-14 no pull request from these forks is open upstream
(`gh pr list -R unclebob/<tool> --author kitaekatt --state all` returns none
for all three).

### Fix branches (as of 2026-11-14)

| Fork | Branch | Change |
|---|---|---|
| dryer | `feat/exclude-paths` | Adds a repeatable `--exclude <glob>` option. |
| dryer | `feat/group-identical-candidates` | Adds `--group-identical`: report structurally identical forms once per set, not as every pair. |
| crapper | `fix/no-implicit-global-pip-install` | Refuses to pip-install test tools into an interpreter that is not a project venv. |
| crapper | `feat/python-test-dir` | Adds `--python-test-dir` for tests kept outside each source tree. |
| crapper | `feat/python-installer` | Adds `--python-installer` (pip or uv) for installing Python test tools. |
| crapper | `feat/python-subprocess-coverage` | Adds `--python-subprocess-coverage` to measure Python child processes. |
| crapper | `feat/function-location` | Adds `:path` (POSIX, relative to the scanned root) and `:line` (1-based start line) to every `.metrics/crap.edn` entry. |
| crapper | `feat/score-default-export` | Scores JS/TS `export default` anonymous functions as name `<default>` with `:path` and `:line`. |
| mutator | `fix/overlay-test-symlink-escape` | Keeps Python files that read `__file__` inside the worker overlay. |

The crapper branches serve the coverage-backed complexity lane of a later
step; step 1 runs crapper with `--no-coverage`, but the pin already carries
them.

## The integration branch: `clean-code-kit`

Each fork the plugin pins has one integration branch named `clean-code-kit`.
It holds merges of the fix branches plus a few integration-only commits made
on the branch itself (listed per fork below); the plugin's pin is always a
commit on it.

**It is never rebased and never force-pushed.** History only moves forward:
by merge commits and by integration-only commits added on top. A published plugin
version names its tool commits by sha, and that version keeps building its
venv from those shas for as long as any machine runs it. Rewriting the branch
leaves an already-published pin pointing at a commit no branch contains, and
GitHub does not guarantee that such a commit stays fetchable, so a machine
still on that plugin version could fail to provision. Adding commits only
forward keeps every pin that was ever published reachable.

State as of 2026-11-14:

- dryer `clean-code-kit` = `ad6a0cec33f8e9d0a26e217edf2c881f55d8654c`: merges
  of `feat/exclude-paths` and `feat/group-identical-candidates` over upstream
  `master`, plus one commit on the branch itself (`ad6a0ce`, a test exercising
  both options together).
- crapper `clean-code-kit` = `50236c15b469e6fb37175474c2ab472df92e6282`:
  merges of all six crapper fix branches (`feat/function-location` at
  `a37c041da931047d0e1bc3a24323408cdcde3226`, then `feat/score-default-export` at
  `44a1af23131b519dea07955fdd9b6c1d21fd3585`), plus two commits on the branch
  itself (earlier than that merge): `e6379aa` (refuse installing outside a project venv with either
  installer -- it corrects the interaction of
  `fix/no-implicit-global-pip-install` and `feat/python-installer`, and is on
  no fix branch) and `83b3758` (a test exercising the options together).
  `e6379aa` exists only on the integration branch. An upstream PR built from
  the fix branches alone would not carry it, so it needs its own upstream
  pull request, opened after `fix/no-implicit-global-pip-install` and
  `feat/python-installer` have merged upstream.
- mutator has no `clean-code-kit` branch; the fork holds `main` and
  `fix/overlay-test-symlink-escape` only.

## The pins

The pins live in `plugins/clean-code-kit/pyproject.toml` as PEP 508 direct
references to the forks:

```
dryer @ git+https://github.com/kitaekatt/dryer@ad6a0cec33f8e9d0a26e217edf2c881f55d8654c
crapper @ git+https://github.com/kitaekatt/crapper@50236c15b469e6fb37175474c2ab472df92e6282
tree-sitter-language-pack==1.25.0
```

A pin is a full 40-character commit sha, never a branch name or a tag. Both
tools report package version 0.1.0 at every commit, so the sha is the only
identity a pin has. uv records the installed commit in the venv's
`<dist>-0.1.0.dist-info/direct_url.json` (`vcs_info.commit_id`), and the
runner compares that with the sha in `pyproject.toml` before every run,
refusing with exit 2 ("installed tool does not match the pinned commit; run
bootstrap") when they differ.

### Why `tree-sitter-language-pack` is pinned exactly

Both tools fetch their tree-sitter parsers at first scan from the
language-pack release (about 26 MB for 1.25.0) into a cache directory named
for the pack version (`.../tree-sitter-language-pack/v1.25.0/`; the runner
points it at the plugin data directory with
`TREE_SITTER_LANGUAGE_PACK_CACHE_DIR`). Upstream requires only
`tree-sitter-language-pack>=0.7`, and the plugin's `uv.lock` is untracked, so
without an exact pin each machine resolves whatever pack version is current
when its venv is next synced. Two consequences the exact pin prevents:

- A sync that picks a different pack version changes the cache directory, so the
  next run downloads the parsers again; a machine without network at that
  moment fails with `DownloadError` (exit 1) although it worked before.
- Different machines can run different parser versions over the same code,
  so their findings are not comparable.

Moving the pack pin is a deliberate change made like a tool pin (below), and
it costs every machine one parser download. The plugin's
`custom_bootstrap.py` makes that download during the next bootstrap pass
(it treats the cache as warm only when the pinned version's directory holds the
release manifest and a parser bundle), so the runner's first-use fetch is
needed only on a machine whose pass had no network. The layout that check
reads (`manifest.json` and `bundles/*.tar.zst` under
`tree-sitter-language-pack/v<version>/`) was observed for 1.25.0 on
2026-11-14; confirm it still holds when the pack pin moves.

## Moving a pin

1. In the fork, merge the fix branch into `clean-code-kit` (a merge commit;
   no rebase), run the tool's own tests, and push `clean-code-kit` to the
   fork. The push must be a fast-forward.
2. Read the tool's help text at the commit being pinned. If an option the lane passes
   changed spelling or meaning, the lane's `argv` must change in the same
   plugin change.
3. Edit the sha in `plugins/clean-code-kit/pyproject.toml`.
4. Bump the plugin version in `plugins/clean-code-kit/.claude-plugin/plugin.json`
   and the same version in `pyproject.toml` (the two must match).
5. Run `tests/clean-code-kit/` and smoke-test with `claudx`, then publish with
   `scripts/publish.py` when the owner has given publish intent. Publishing is
   owner-gated; the pin edit alone reaches no consumer.

After the release, bootstrap re-syncs the plugin venv and the provenance
check passes against the pinned sha. A machine whose venv has not re-synced
gets the exit-2 refusal rather than running the old tool silently.

## When a fix merges upstream

1. Fetch upstream and merge its default branch into the fork's
   `clean-code-kit` (merge, never rebase). Resolve any conflict between the
   upstream form of the fix and the fork's form in that merge.
2. Confirm the change the fix branch carried is present and the tool's tests
   pass, then move the pin as above, including step 2: an upstream version of
   a fix may spell its option differently.
3. The fix branch is no longer carried. Deleting it is a branch deletion and
   needs the owner's agreement.

Whether a pin should ever point at upstream directly, once no fork-only
change remains, is not settled here; until it is, pins stay on the fork's
integration branch.
