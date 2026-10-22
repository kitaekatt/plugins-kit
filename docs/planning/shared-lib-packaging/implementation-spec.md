# Shared-lib packaging -- implementation spec (databench increment)

Status: advisor-approved. Sol approved the architecture and the plan with amendments; Astra rechecked the release state machine (B) and runtime mode (C) and ruled AMEND; this spec applies every amendment. Date: 2026-10-22. The user chose public PyPI and the namespace `katsoft-<plugin>` (scheme A) on 2026-10-22: distributions `katsoft-contracts` and `katsoft-llm-kit`, import package `katsoft_contracts`; the import package `llm_kit` is unchanged. U1r and U5 are implemented as local commits 65edb82c and 769bb9fa (not pushed; code review pending).

## Units and order
| # | unit | needs namespace | files owned (exclusive) | parallel |
|---|---|---|---|---|
| 1 | U1r release machinery | no (tests use fixture registries) | packages/registry.toml (empty, no entries), packages/released.json (`{}`), packages/_build/** (hook, constraints.txt), scripts/_packages.py, scripts/publish.py, .github/workflows/release-packages.yml, tests/repo-scripts/test_package_release_states.py, tests/repo-scripts/test_publish_packages.py, docs/reference/package-release.md, "Packages" section of docs/reference/publish-reconcile.md | with U5 |
| 2 | U5 CLI goldens | no | tests/llm-kit/goldens/cli/**, tests/llm-kit/test_cli_goldens.py | with U1r |
| 3 | U2 contracts source | yes | packages/katsoft-contracts/** (hatchling, src layout, version 1.0.0), tests/contracts/**, scripts/sync_contracts_copy.py | after U1r |
| 4 | U3 llm-kit distribution + runtime mode | yes | packages/katsoft-llm-kit/pyproject.toml (wheel-only, external force-include of plugins/llm-kit/lib/llm_kit, deps katsoft-contracts>=1.0,<2 + plugin extras, console script llm-kit), plugins/llm-kit/lib/llm_kit/_deployment.py, _runtime.py, routed import sites, cli/__init__.py, tests/llm-kit/test_runtime_mode.py, test_dist_wheel.py, test_dist_metadata_parity.py, test_import_graph.py edge + architecture doc edge, llm-kit plugin.json + plugin pyproject bump | after U2 |
| 5 | U4 clean-venv closure | yes | tests/packaging/test_wheel_closure.py, tests/packaging/conftest.py (opt-in slow marker; --wheel-source local|pypi) | after U3 |
| 6 | U7 first PyPI release | yes + user actions | packages/registry.toml entries | last |

User actions before U7: PyPI account with 2FA; GitHub environment `pypi` in kitaekatt/plugins-kit; pending publishers for `katsoft-contracts` and `katsoft-llm-kit` (owner kitaekatt, repo plugins-kit, workflow release-packages.yml, environment pypi); gh authenticated on the publishing machine; a publish go-signal.

## Preflight policy (U1r)
- R (release set) = registered packages whose pyproject version has no entry in released.json.
- `--only` is refused whenever R is non-empty.
- Recovery/publishability: a run is publishable if the range has plugin bumps, OR R is non-empty, OR F:packages/released.json has entries that origin/master:packages/released.json lacks (content comparison). Replaces the bare refusal in _require_version_bump (publish.py:876).
- Partial semver check: removing a name listed in a library's public surface (llm-kit: contract/public-surface.json; contracts: __all__) requires a major bump. Catches listed names only, not behavioural breaks -- document as partial.
- Source drift without a package version bump is REPORTED, not released (releases are deliberate version bumps).
- Registry/dependency checks with `packaging.specifiers` (add `packaging` to root dev extras if missing): cycles refuse; every first-party requirement (of each package in R and each shipping plugin) must match at least one version in released.json or the pending version in R; processing order topological.
- Workflow-on-default-branch: if R is non-empty, refuse unless origin/master:.github/workflows/release-packages.yml exists and equals the blob in F. Bootstrapping: U1r lands with an empty registry; the next ordinary publish projects the workflow to master (it lives outside plugins/ and always ships). Later workflow edits ship in a publish releasing no package.

## B. Release machinery
### B0 Fixed identities
- P = refs/heads/dev resolved once after the clean-tree gate. All preflight reads use `git show P:<path>` (manifests, local/published plugin sets, version bumps, pyproject sync, registry), never the working tree.
- B = P + regenerated derived artifacts, committed in a PRIVATE index (GIT_INDEX_FILE temp): read-tree P, add blob, write-tree, commit-tree -p P. Never amend; delete commit_derived's amend path.
- One helper advances dev: `git push origin <new>:refs/heads/dev` (fast-forward only; rejection refuses), then `git update-ref refs/heads/dev <new> <expected-old>`; if local dev moved, leave the local ref and print `git pull --no-rebase`; `git restore --source=<new> --staged --worktree -- <publish-owned paths>` touching only publish-owned paths (marketplace.json, packages/released.json).
- F = last ledger commit, or B. Refuse unless `git diff --name-only P F` is a subset of publish-owned paths. F is passed explicitly to projection (read-tree F), manifest reads (F:), the fast-forward push (`git push origin F:refs/heads/master`) and verify() (compare master to F, not origin/dev). No branch name is read after F is fixed.

### B1 Observations (re-read every run)
- J = released.json[d][v] at origin/dev.
- T = peeled commit from `git ls-remote origin refs/tags/d-vv^{}` (never the tag-object SHA).
- PyPI via GET https://pypi.org/pypi/<d>/<v>/json: ABSENT (404); FILES_PRESENT (200, exactly one file, packagetype bdist_wheel, filename `<norm(d)>-<v>-py3-none-any.whl`, sha256 digest present, not yanked); YANKED (any file yanked); ANOMALY (200 with no files, extra files, or other filename); OBS_ERROR (network/5xx/timeout/malformed JSON).
- Runs: `gh run list --workflow release-packages.yml --json databaseId,status,conclusion,createdAt,displayTitle`, filtered to displayTitle == `release d-vv`; the newest by createdAt is authoritative.
- Build inputs S(X): the registry entry's source roots including external force-included paths, packages/<d>/**, packages/_build/** (hook, constraints.txt), packages/registry.toml. same_src(X,B) = `git diff --quiet X B -- S(B)` and S(X)==S(B).
- OBS_ERROR and any verifier/build/tool error: stop, no state change, exit 1, "observation failed; re-run". Permanent refusal only on a demonstrated artifact mismatch.

### B2 Transitions per (d,v), topological order
1. J present, T == J.commit, FILES_PRESENT with sha256 == J.wheel_sha256 -> RECORDED; nothing to do.
2. J present, any other T/PyPI combination -> refuse; user decides (name YANKED explicitly).
3. J absent, T absent, ABSENT -> UNRELEASED: (a) every first-party dep of d must be RECORDED, else stop; (b) snapshot build from B (B3); (c) wheel-content test; (d) advance dev to B and confirm `ls-remote refs/heads/dev` == B; (e) `git tag -a d-vv B` and `git push origin refs/tags/d-vv` (rejection = concurrent claim -> re-enter table) -> TAGGED.
4. J absent, T absent, PyPI != ABSENT -> refuse (uploaded outside the machinery).
5. J absent, T present, not same_src(T,B) -> refuse ("v is tagged at <T> with different build inputs; bump v"); tags never moved or deleted.
6. J absent, T ok, ABSENT -> TAGGED: poll every 10 s. Newest run queued/in_progress: keep polling (cap 20 min). Run succeeded: keep polling PyPI up to 15 min (CDN staleness). Run failed/cancelled, or no run after 120 s: STOP and print the run URL and the user's resume command (`gh workflow run release-packages.yml --ref master -f tag=d-vv` or `gh run rerun <id>`). NO automatic redispatch. On the next publish.py run the newest run decides; older failures are ignored. Timeout stops with no state change.
7. J absent, T ok, ANOMALY -> refuse; user decides.
8. J absent, T ok, FILES_PRESENT -> UPLOADED: Verify (B4). Pass -> ledger commit (B5) -> RECORDED. Demonstrated mismatch -> permanent refusal ("PyPI artifact differs from the tag source; bump v; yanking is your call"). Error -> stop, no state change.
Serialization: the atomic tag push is the claim on (d,v); workflow `concurrency: release-${{ inputs.tag || github.ref_name }}`, cancel-in-progress false; a ledger write finding an identical entry is a no-op.

### B3 Deterministic build (release and reference builds)
`git archive <X> -- S(X)` extracted to a temp dir keeping repo-relative layout; `uv build --wheel --build-constraint packages/_build/constraints.txt` (all build deps pinned); SOURCE_DATE_EPOCH = committer time of X; hook inputs exactly RELEASE_TAG, RELEASE_COMMIT (peeled), RELEASE_TAG_URL -- hook refuses if any is missing; hook writes them into llm_kit/_deployment.py (U3) or katsoft_contracts/_release.py (U2) and [project.urls] Source.
Workflow: triggers `push: tags: ['*-v*']` and `workflow_dispatch` (input tag); `run-name: release ${{ inputs.tag || github.ref_name }}`; checkout refs/tags/<tag> explicitly; assert HEAD == peeled tag and pyproject version == tag version; B3 build; wheel-content test; pypa/gh-action-pypi-publish with environment pypi, permissions id-token: write.

### B4 Verify
Download the file URL (sha256 must equal the JSON digest); build a reference wheel from T with B3; archive member sets equal; every member except *.dist-info/RECORD has equal recomputed sha256 of decompressed bytes, RECORD equal after parsing; record the downloaded wheel's sha256; fresh venv `uv pip install <downloaded file>` with first-party deps at their RECORDED versions; smoke: import, metadata version == v, runtime marker reads `package`, COMMIT == T. Steps up to member comparison are the mismatch test; a smoke failure on matching bytes refuses with "bump v" (not recorded, not a mismatch).

### B5 Ledger commit
Fetch origin/dev tip E; private index: read-tree E, write entry {version, tag, tag_url, commit, filename, wheel_sha256, verified_at}, commit-tree -p E; advance dev with expected parent E; on push rejection rebuild on the new tip, max 3 tries, then refuse. Result = F. Projection follows (publish-reconcile.md:175: master must not gain content dev lacks).

### U1r red-on-revert tests
One per B2 row; test_rerun_after_ledger_before_projection_publishes; test_projection_and_ff_use_frozen_F_when_dev_moves; test_verify_compares_master_to_F; test_no_amend_of_derived_or_ledger; test_ledger_refuses_moved_parent; test_tag_waits_for_B_on_remote; test_peeled_tag_used; test_obs_error_no_state_change; test_yanked_and_anomaly_refuse; test_member_mismatch_permanent_but_verifier_error_not; test_downloaded_file_is_installed; test_no_automatic_redispatch; test_newest_run_wins; test_dependent_tag_waits_for_dep_recorded; test_unsatisfiable_and_cycle_refuse; test_workflow_absent_on_master_refuses_release; test_same_src_covers_hook_and_constraints; test_only_refused_when_release_set_nonempty; test_no_release_set_makes_no_network_call.

## C. Runtime mode (U3)
C1 Marker: plugins/llm-kit/lib/llm_kit/_deployment.py committed as MODE = "legacy". The wheel excludes it and force-includes a hook-generated one: MODE = "package", DIST, VERSION, TAG, COMMIT.
C2 llm_kit/_runtime.py mode(), cached per process: (1) _deployment absent or MODE not in {legacy, package} -> RuntimeIdentityError. (2) sys.frozen, loader not SourceFileLoader, or __file__ not a real file -> UnsupportedLayout. (3) legacy: if any distribution named DIST exists in importlib.metadata.distributions() -> RuntimeIdentityError ("this environment holds both the plugin's llm_kit (bootstrap shared-lib .pth) and the katsoft-llm-kit package; use separate environments -- the plugin venv for the plugin, a consumer venv without the bootstrap llm_kit link for the package"); remove nothing. (4) package: exactly one DIST distribution (duplicates raise); direct_url.json with dir_info.editable -> UnsupportedLayout; its RECORD lists llm_kit/_deployment.py with sha256 equal to the loaded file's bytes; marker VERSION == dist version; no other distribution's files include any llm_kit/ path (overlapping provider, e.g. the plugin project `llm-kit`) -> raise.
C3 Route only execution_event, interrupt_contract, model_declaration via load(name): package mode imports katsoft_contracts.<name>; only a ModuleNotFoundError whose exc.name is katsoft_contracts or that submodule becomes ContractsMissing ("install katsoft-contracts"); every other ImportError propagates unchanged. Legacy mode = today's path with every existing diagnostic (too-old and absent probes). Sites: registry/declaration_syntax.py:32,41; jobs/model.py:562,572; dispatch/declaration.py:188,201; execution/event_probe.py:42-43 (used :104); execution/contract_probe.py:32-33 (used :140); pipeline/execution/interrupts.py:91 (used :161); pipeline/execution/events.py:138 (U3 confirms). Package mode skips the bootstrap_lib pre-probes and symbol probes at these sites.
C4 Other bootstrap_lib imports: require_legacy(feature) raises UnsupportedInPackageMode(feature) before any bootstrap_lib import -- codex: adapters/codex_backend.py:96,543, adapters/harness_adapters.py:252,269; claudx: adapters/backends.py:569, jobs/run.py:351, pipeline/execution/drivers/claude_bg.py:326; skill material: completion/skill_context.py:98,102; code review: dispatch/review_lane.py:23,34 (module top). dispatch/reachability.py:319: package mode skips the bootstrap_lib import and uses its existing PATH + --version check (a real verdict, not a fallback). registry/models.py:260,265 config layering: OPEN USER DECISION -- package mode either uses shipped defaults or MODEL_ENDPOINTS_REGISTRY (documented package behaviour; planner's proposal) or refuses when MODEL_ENDPOINTS_REGISTRY is unset. Until ruled, implement it as a single clearly named policy point.
C5 cli.main(argv=None) calls reexec_under_plugin_venv only when mode() == "legacy".
C6 Tests (each red on revert): test_marker_missing_raises, test_marker_invalid_raises, test_legacy_with_dist_metadata_requires_separation, test_duplicate_dist_metadata_raises, test_overlapping_provider_raises, test_editable_raises, test_zip_layout_raises, test_contracts_missing_named, test_internal_importerror_preserved, test_package_mode_never_imports_bootstrap_lib, test_only_three_contracts_routed, test_unsupported_features_in_package_mode, test_reachability_codex_path_check_in_package_mode, test_cli_main_argv_none_package_no_exec, test_cli_main_argv_none_legacy_execs (stub plugin venv under scratch HOME, os.execv patched). U4 asserts the installed wheel's marker is package and COMMIT equals the build commit.

## U2 contracts (dormant)
bootstrap_lib/{execution_event,interrupt_contract,model_declaration}.py stay full modules, byte copies of katsoft_contracts (edit canonical, run scripts/sync_contracts_copy.py). Non-vacuous guard: goldens captured once from bootstrap_lib at commit a1257278 -- frozen literals, error messages for a fixed list of bad inputs, canonical event bytes, exception class names -- test_goldens_frozen; plus test_bootstrap_lib_copy_identical and test_wheel_contents.

## Transition and deferred
- Until `llm-kit-package-consumption` lands, every llm_kit edit still needs an llm-kit plugin bump (check-staged-version-bump.sh:66-67 owns plugins/llm-kit/); a package release is a separate deliberate version bump.
- llm-kit-package-consumption (later): consumer plugin pyprojects declare katsoft-llm-kit>=X.Y,<X+1; drop llm_kit from shared_lib_imports; targeted stale-.pth revocation; move source to packages/katsoft-llm-kit/src (enables sdist); then U6.
- U6 (deferred): venv stamp folds the verified in-range target from the marketplace clone's packages/released.json; behind venv syncs with --upgrade-package/--refresh-package; failing to install a declared target is a FAILED convergence (no stamp written); only an explicit `bootstrap run` is promised to pick it up.
- U8 (deferred): contracts activation -- bootstrap_lib shims, contracts dependency in all 13 bootstrap_lib-linking plugins (incl. transitive git-kit, p4-kit, skills-kit), revocation, standalone-interpreter inventory.
- Also deferred: Mode 4 doc revision, databench handoff, spiritcrossing, bootstrap_lib general/engine split.

## Hypotheses to settle in the named unit
hatch hook can exclude/replace llm_kit/_deployment.py from packages/_build (U3); uv build --build-constraint + SOURCE_DATE_EPOCH gives equal member payloads (U1r); gh run list exposes displayTitle for push and dispatch runs (U1r); Trusted Publishing accepts a dispatch run from master that builds a tag checkout (U7); packaging available in root env (U1r); CI wait 1-3 min (U7 measures).
