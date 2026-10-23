# Package release

How `scripts/publish.py` releases the Python distributions registered in
`packages/registry.toml` to PyPI. Read when: registering a package, bumping a
package version, or resuming a release that stopped. The mechanics live in
`scripts/_packages.py` (states, observations, ledger) and
`packages/_build/release_build.py` (deterministic build, wheel-content test);
this document states the contract and the operator's commands.

Distributions are named `katsoft-<plugin>`: `katsoft-llm-kit` (import
`llm_kit`) and `katsoft-contracts` (import `katsoft_contracts`).

## Files

| Path | Role |
|---|---|
| `packages/registry.toml` | The registered distributions. An entry: `name`, `import`, `marker` (wheel path of the release marker), `sources` (build inputs outside `packages/<name>/`), optional `surface` (`{ json = <public-surface.json> }` or `{ all = <module with __all__> }`). Top level: `schema = 1`, `repository` (the GitHub URL tag links are built from). |
| `packages/<name>/pyproject.toml` | The distribution's project; its static `version` is the version that releases. It points both hatchling hooks at `../_build/hatch_build.py` and lists `urls` as dynamic. |
| `packages/_build/` | `release_build.py` (B3 build, wheel-content test, wheel comparison), `hatch_build.py` (writes the release marker and the Source URL), `constraints.txt` (every build dependency pinned). |
| `packages/released.json` | The ledger: `{dist: {version: {version, tag, tag_url, commit, filename, wheel_sha256, verified_at}}}`. Only `publish.py` writes it, one ledger commit per verified release. |
| `.github/workflows/release-packages.yml` | Builds the tag and uploads it with Trusted Publishing (environment `pypi`). |

A package release is a deliberate version bump in `packages/<name>/pyproject.toml`.
Changing a package's build inputs without a bump is REPORTED by preflight
(`note: ... build inputs changed`) and nothing is released.

## Preflight policy

The release set R is every registered distribution whose pyproject version
has no entry in `released.json` at P (dev, frozen after the clean-tree gate).
Preflight reads git objects only and makes no network call. It refuses when:

- `--only` is given and R is non-empty.
- A first-party dependency graph has a cycle, or a first-party requirement (of
  a package in R, or of a shipping plugin's `pyproject.toml`) admits no
  released version and no pending version in R. Requirements are judged with
  `packaging.specifiers`; R is processed in dependency order.
- A name listed in a package's public surface was removed since its highest
  released version without a major bump. This check is PARTIAL: it sees listed
  names only, never a behavioural break.
- R is non-empty and `origin/master:.github/workflows/release-packages.yml`
  is absent or differs from dev's. GitHub runs tag-push and dispatch
  workflows from the default branch, so the workflow reaches master in a
  publish that releases no package (it lives outside `plugins/` and ships with
  every bare publish), and every later workflow edit does the same.

A run is publishable when the range has a plugin bump, OR R is non-empty, OR
dev's `released.json` has entries master's lacks (a run that recorded a
release and stopped before projecting it; re-running publishes it).

## Release states

For each (d, v) in R, in dependency order, every run re-reads: J (the ledger
entry at `origin/dev`), T (the PEELED commit of `refs/tags/d-vv` on origin),
the PyPI JSON for `https://pypi.org/pypi/<d>/<v>/json`, and the newest
release run titled `release d-vv` (`gh run list --workflow
release-packages.yml`). A failed observation, build or tool stops the run
with `observation failed; re-run` and changes nothing.

| J | T | PyPI | Action |
|---|---|---|---|
| present | == J.commit | one wheel, sha256 == J | RECORDED; nothing to do |
| present | other | other | refuse; the user decides (YANKED is named) |
| absent | absent | absent | UNRELEASED: dependencies must be RECORDED; snapshot build from B and content test; dev advanced to B and confirmed on origin; `git tag -a d-vv B` pushed (a rejected push is a concurrent claim: re-read) |
| absent | absent | present | refuse: uploaded outside the machinery |
| absent | present, other build inputs | any | refuse: bump v (tags are never moved or deleted) |
| absent | present | absent | TAGGED: wait for the run, then for PyPI |
| absent | present | anomaly or yanked | refuse; the user decides |
| absent | present | one wheel | UPLOADED: verify, then record |

PyPI states: absent (404); one file (`bdist_wheel`, `<norm(d)>-<v>-py3-none-any.whl`,
sha256 present, not yanked); yanked (any file yanked); anomaly (no files,
extra files, another filename).

Waiting in TAGGED polls every 10 s: a queued or running newest run is waited
on for up to 20 min; a successful run is followed by up to 15 min of PyPI
polling; a failed or cancelled run, or no run after 120 s, STOPS the publish
with the run URL and the resume commands below. `publish.py` never
re-dispatches a run. On the next `publish.py` run the newest run decides;
older failures are ignored.

## Build (B3) and verify (B4)

Build: `git archive <commit> -- <inputs>` (sources, `packages/<d>/`,
`packages/_build/`, `packages/registry.toml`) extracted with repo-relative
paths; `uv build --wheel --build-constraint packages/_build/constraints.txt`;
`SOURCE_DATE_EPOCH` = the commit's committer time; the hook receives exactly
`RELEASE_TAG`, `RELEASE_COMMIT` (peeled) and `RELEASE_TAG_URL`
(`<repository>/tree/<tag>`) and refuses to build without them. A reference
build runs the `release_build.py` committed at the tag, so both builders are
the same code.

Verify: the downloaded file must match PyPI's sha256; a reference wheel built
from T must have the same member set, the same decompressed bytes for every
member except `*.dist-info/RECORD`, and an equal parsed RECORD. A difference
is a permanent refusal (`PyPI artifact differs from the tag source; bump v;
yanking is your call`). The DOWNLOADED file is then installed into a fresh
venv with first-party dependencies at their recorded versions; the smoke
test imports the package and requires metadata version v, marker `MODE ==
"package"` and `COMMIT == T`. A smoke failure on matching bytes refuses with
"bump v" and records nothing.

Record: the ledger entry is committed in a private index on the current
`origin/dev` tip and pushed fast-forward only (rebuilt on a moved tip, at most
three times). The projection then carries it to master.

## Operator commands

```bash
uv run --extra dev python scripts/publish.py --check   # preflight, no network
uv run --extra dev python scripts/publish.py           # release packages, then plugins
gh run rerun <run id>                                   # resume a failed run
gh workflow run release-packages.yml --ref master -f tag=<dist>-v<version>
```

`packaging` comes from the root `dev` extra; it is imported only when the
registry has entries.

Before the first release, the user provides: a PyPI account with 2FA; the
GitHub environment `pypi` in kitaekatt/plugins-kit; a pending Trusted
Publisher per distribution (owner `kitaekatt`, repository `plugins-kit`,
workflow `release-packages.yml`, environment `pypi`); an authenticated `gh` on
the publishing machine; and a publish go-signal.

## katsoft-llm-kit

Install and extras (the llm-kit plugin's venv requires
`katsoft-llm-kit[<every extra below>]>=1.2,<2`; 1.2.0 added
`llm-kit account status --json`, the document the plugin's
`custom_bootstrap.py` reads, so the plugin's floor is 1.2;
`tests/llm-kit/test_dist_metadata_parity.py` holds the requirement to the
extras the distribution offers):

```bash
pip install katsoft-llm-kit
pip install "katsoft-llm-kit[sdk,jobs,config]"
```

| Extra | Brings |
|---|---|
| `config`, `jobs`, `pipeline` | PyYAML |
| `sdk` | the OpenAI client (the OpenRouter HTTP path) |
| `local` | psutil |
| `frontdoor` | FastAPI, uvicorn, httpx |

The base install depends on `katsoft-contracts>=1.0,<2` only. The console
script is `llm-kit`; the import package is `llm_kit`.

Package mode (`llm_kit._runtime.mode() == "package"`, set by the hook-written
`llm_kit/_deployment.py`):

- Fleet availability is a separate question from package mode:
  `llm_kit._runtime.fleet()` is True when `bootstrap_lib` imports in the
  environment (cached per process), as in a plugin venv bootstrap links to the
  marketplace's shared libraries. Features that need `bootstrap_lib` (the Codex
  backend and adapter, claudx propagation, skill context, the code-review
  lane) call `require_fleet`, which raises `UnsupportedInPackageMode` only when
  `fleet()` is False. Whether the linked `bootstrap_lib` is new enough stays
  with each feature's own probe, not with the gate.
- Without the fleet, Codex reachability uses the PATH + `--version` check and
  the model registry reads the shipped defaults, and
  `MODEL_ENDPOINTS_REGISTRY` overrides the registry file (user ruling
  2026-10-22); the single policy point is
  `registry.models.package_mode_model_config`. With the fleet, Codex
  reachability uses `bootstrap_lib.codex.detect_codex` and the model registry
  reads the layered config (fleet, machine and project layers). The wheel's
  off-fleet closure is unchanged: `tests/packaging` blocks `bootstrap_lib`.
- Do not install the package into an environment that also holds the plugin's
  linked `llm_kit` (the bootstrap shared-lib `.pth`). `mode()` raises
  `RuntimeIdentityError` there and removes nothing; use separate
  environments.

## Measured premises

Measured 2026-10-22 (uv 0.12.5, hatchling 1.32.0, CPython 3.12.9, macOS
arm64), building the llm_kit source (`plugins/llm-kit/lib/llm_kit`; the source moved to `packages/katsoft-llm-kit/src/llm_kit` in katsoft-llm-kit 1.2.1) as `katsoft-llm-kit` 1.0.0 in
a scratch repository: three builds (two from one clone, one from a second
clone at another path) produced byte-identical wheels; the hook refused a
build without the identity variables. `gh` 2.92.0 exposes `displayTitle` in
`gh run list --json`; that it equals `run-name` for both tag-push and
dispatch runs is GitHub's documented behaviour, not observed on a run of
this workflow as of 2026-10-22.
