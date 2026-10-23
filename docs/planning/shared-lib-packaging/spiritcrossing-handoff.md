# Spiritcrossing handoff: migrate to the katsoft PyPI packages

Audience: a spiritcrossing session on a machine that has the spiritcrossing source.
The plugins-kit machine that wrote this has no spiritcrossing checkout, so every
statement about spiritcrossing's own code is the user's description (2026-10-22,
`dev/tasks/shared-lib-packaging/log.md`, entry "spiritcrossing answers and surface
check") and is marked UNVERIFIED until you confirm it against the source.

Goal: spiritcrossing imports `llm_kit` and `katsoft_contracts` from two pinned PyPI
distributions installed in its own project venv, with no bootstrap shared-lib link and
no dependency on a plugins-kit install. This removes the need for every spiritcrossing
user to install llm-kit as a plugin.

Companion documents:
- `docs/planning/shared-lib-packaging/packaging-plan.md` (release channel, "Spiritcrossing"
  section, databench handoff used as the model for this one).
- `plugins/bootstrap/skills/bootstrap/references/library-consumption.md`, Mode 4a (the
  supported off-fleet install) and the "Do not combine" warning.

## 1. The packages and the exact pins

Released on public PyPI (`packages/released.json`, `verified_at` 2026-10-22):

| Distribution | Import package | Version | Tag |
|---|---|---|---|
| `katsoft-llm-kit` | `llm_kit` | 1.0.0 | `katsoft-llm-kit-v1.0.0` (commit `5f44fbbd`) |
| `katsoft-contracts` | `katsoft_contracts` | 1.0.0 | `katsoft-contracts-v1.0.0` (commit `4f824b5b`) |

`packages/released.json` is the source for the version; read the entry for each
distribution before you pin, and use the newest `version` it lists. If it lists a
version later than 1.0.0, pin that one and re-run the acceptance run in section 8.

Pin both, exactly, in spiritcrossing's own `pyproject.toml` (or requirements file):

```
katsoft-llm-kit[pipeline,sdk]==1.0.0
katsoft-contracts==1.0.0
```

- Extras named here come from the user's description plus the surface check:
  `pipeline` (pyyaml, needed for `llm_kit.scaffold.dispatch`) and `sdk` (openai, needed
  for the OpenRouter HTTP path). The distribution defines `config`, `sdk`, `local`,
  `frontdoor`, `jobs` and `pipeline` (`packages/katsoft-llm-kit/pyproject.toml`).
  Add another extra only when a code path you run fails with a missing third-party
  module. Which extras `llm_kit.adapters.claude_runner` needs is UNVERIFIED.
- `katsoft-contracts` is also a declared dependency of `katsoft-llm-kit`
  (`katsoft-contracts>=1.0,<2`); pin it directly as well so the exact version is
  explicit and `llm_kit` declaration routing cannot run without it. Declaration-based
  routing (`route(models=...)`, `CONTENT_PIPELINE_LLM_MODELS`, `create-run --models`)
  fails without the contracts distribution (log, surface check).
- Python: `requires-python = ">=3.12"` for `katsoft-llm-kit`. Spiritcrossing's own Python
  version is UNVERIFIED; confirm it is 3.12 or newer, or the install fails.
- Install into the project venv only, with a normal installer (`uv pip install` or `pip`)
  against public PyPI. Never install from a git URL or a vendored copy
  (library-consumption.md Mode 4b).

## 2. Step 0: find out how spiritcrossing reaches `llm_kit` today

Run these checks before changing anything, and record the answers in your task log. They
decide which of the removals in section 3 applies.

1. Search the project's tracked files for `shared_lib_imports` and `llm_kit`
   (`.claude/bootstrap.json`, any `bootstrap.json`, `pyproject.toml`).
   - A `project_venv` block whose `shared_lib_imports` lists `llm_kit` or `bootstrap_lib`
     is Mode 3a.
   - A `sitecustomize` file or a path shim that adds
     `~/.claude/plugins/data/<marketplace>/_shared_libs/llm_kit` to `sys.path` is Mode 3b.
   - A `plugins` entry for `plugins-kit:llm-kit` (with a `min_version`) is a plugin edge
     that only exists to supply the library.
2. In the project venv (`SpiritCrossing/Scripts/python.bat` is the user's description of
   the host entry; its target venv is UNVERIFIED), list the site-packages directory for
   `llm_kit.pth`, `bootstrap_lib.pth` and `content_pipeline.pth`.
3. With the venv interpreter, run the import and print `llm_kit.__file__`. A path under
   `_shared_libs` means the fleet copy is in use. A path under `site-packages/llm_kit`
   means a wheel is already in use.
4. Search the source for imports of `bootstrap_lib`. Spiritcrossing is not known to
   import it directly (UNVERIFIED). If it does, the only supported contract modules are
   `execution_event`, `interrupt_contract` and `model_declaration`, and each moves to
   `katsoft_contracts.<same name>`. Any other `bootstrap_lib` import has no PyPI
   replacement (library-consumption.md Mode 4b) and must be removed or reported back.

## 3. Step 1: remove the shared-lib link and any stale `.pth`

A bootstrap `.pth` prepends the shared copy to `sys.path`, so it shadows an installed
wheel and the project keeps running the fleet copy while its pin says otherwise. Bootstrap
does not remove a `.pth` once written, so editing the manifest alone does not undo it
(library-consumption.md, Mode 4a; `log.md` round-2 item (e)).

- Mode 3a: delete `llm_kit` (and `bootstrap_lib` if listed) from
  `project_venv.shared_lib_imports`; delete the `plugins-kit:llm-kit` edge if it only
  supplied the library. Bootstrap revoking an undeclared link itself is planned, not shipped (plan draft, Wave 1, E2: UNVERIFIED), so do
  not rely on it.
- Mode 3b: delete the `sitecustomize` shim or path entry.
- Delete `llm_kit.pth`, `bootstrap_lib.pth` and `content_pipeline.pth` from the venv's
  site-packages. Alternatively create a fresh venv, which is the cleaner route.
- Remove or reword any user-facing text that tells people to run `claude plugin install`
  to obtain llm-kit.
- The databench migration is the worked example: it removed `shared_lib_imports` and the
  plugin edge, deleted the three stale `.pth` files from its own venv, and added a test
  that fails and names any stale `.pth` found (`dev/tasks/shared-lib-packaging/log.md`,
  2026-10-22 23:30). Other machines' venvs keep a stale `.pth` until someone deletes it, so
  add the same kind of check to spiritcrossing.

## 4. Step 2: install the pins and point the host at the venv

1. Create a 3.12 venv (or reuse the project venv after section 3 cleanup) and install the
   pins from section 1.
2. Confirm `SpiritCrossing/Scripts/python.bat` (UNVERIFIED path) launches that venv's
   interpreter, so `loc.py` and `first_pass.py` run with the pinned libraries. Do not
   point the host at a bootstrap-managed interpreter.
3. Pin the same versions in whatever file recreates the venv on another machine, so a
   new machine installs the packages from PyPI with no bootstrap step.
4. Read the llm-kit version through `importlib.metadata.version("katsoft-llm-kit")`, not
   through `~/.claude/plugins/installed_plugins.json` (databench changed
   `generate_config.py` for the same reason; packaging-plan.md, databench handoff).
5. If any code launches the `llm-kit` command, resolve it as the console script beside
   the venv interpreter, not from PATH. The user states spiritcrossing runs no llm-kit CLI
   subprocess (UNVERIFIED against source).

## 5. What to check in the code

The user's description of spiritcrossing, each item UNVERIFIED against source:

- `loc.py` (with `loc_ops`) and `first_pass.py` (with `firstpass_ops`) run host-side in
  the project venv. Only `first_pass.py refresh --ue` touches Unreal, through unreal-kit's
  `ue_runner`; unreal-kit is a plugin, not part of this migration.
- Libraries used: the `llm_kit.pipeline` API; `first_pass.py` also uses
  `llm_kit.adapters.claude_runner`. Localization mounts `llm_kit.cli.run.build_commands`
  and `llm_kit.scaffold.dispatch` in-process.
- Backends: `openrouter` and `claude-cli`. Codex is unused.

### 5.1 Measured surface (llm-kit 0.4.0)

Measured on 2026-10-22 against the 0.4.0 wheel (a prior release candidate, not 1.0.0)
in a fresh 3.12 venv in isolated mode, `bootstrap_lib` blocked (`log.md`, surface check):

- 162 of 163 modules import. The one failure is `llm_kit.dispatch.review_lane`, which
  needs `bootstrap_lib` and is not a spiritcrossing module.
- Everything works except declaration-based routing without the contracts distribution,
  `execution.events.project_run` and `store.request_interrupt`. With the pins in section 1
  the contracts distribution is installed, which resolves the first item. The other two
  were not re-measured on 1.0.0: UNVERIFIED. Run them in the acceptance run if
  spiritcrossing calls them.
- `llm_kit.scaffold.dispatch` is a function in `llm_kit/scaffold/__init__.py`, not a
  module. `import llm_kit.scaffold.dispatch` fails; `from llm_kit.scaffold import dispatch`
  works (source: `plugins/llm-kit/lib/llm_kit/scaffold/__init__.py`).
- The jobs commands spiritcrossing mounts are `llm_kit.cli.run.build_commands` (the
  execution-ledger set), not `llm_kit.jobs` (packaging-plan.md, Spiritcrossing).
- The 1.0.0 wheel was verified for the databench surface only
  (`WHEEL_CLOSURE_SOURCE=pypi uv run --extra dev pytest tests/packaging`, 35 passed,
  2026-10-22). That suite does not exercise the spiritcrossing modules above: UNVERIFIED
  for 1.0.0 until your acceptance run.

### 5.2 Public-surface gaps

The promised stable surface is `plugins/llm-kit/contract/public-surface.json`, which
`covers` only `llm_kit.pipeline`, `llm_kit.scaffold` and `llm_kit.cli.run`. These
spiritcrossing-relevant modules lack `__all__` or listing, so no stability promise
applies to them:

- `llm_kit.pipeline.core.validate.*` (contract, floor_guard, riders)
- `llm_kit.pipeline.artifacts.freshness.*`
- `llm_kit.pipeline.artifacts.store.*` (attributed, intermediary)
- `llm_kit.pipeline.execution.events`
- `llm_kit.adapters.claude_runner` (`llm_kit.adapters` is not in `covers`)
- `llm_kit.pipeline.runtime.gate` (2 of 11 names listed)

(Module names expanded from the log's short forms by directory listing of
`plugins/llm-kit/lib/llm_kit/`; the gap list itself comes from the surface check in
`log.md` and packaging-plan.md.)

What to do on your side:
- Pin exactly (section 1) and treat an import from any module above as unsupported API:
  a release of `katsoft-llm-kit` may change it with no deprecation notice.
- List every such import spiritcrossing uses and report the list to the plugins-kit task
  that owns the surface work (adding `__all__` and listing to `public-surface.json`).
  That work is plugins-kit's and is not done: UNVERIFIED that any item has been closed.
- Do not vendor or copy these modules to freeze them.

## 6. CLI goldens findings that matter

The CLI goldens (`tests/llm-kit/goldens/cli/`, 17 files) cover the verbs databench calls:
`completion complete|request-schema|repair-json`, `models endpoints|describe|resolve`,
`jobs --version`. Spiritcrossing is not described as calling the CLI, so these matter only
if section 2 or section 4 finds a subprocess call. If it does:

- `llm-kit models describe` with no usable target writes its error JSON to stderr and
  leaves stdout empty (`models_describe_mismatch.json`).
- The `completion complete` envelope key is `endpoint`, not `entry` (`complete_ok.json`).
- `--request-file` combined with `--prompt` exits 4 (`complete_request_file_conflict.json`).
- `llm_kit.cli.main(argv=None)` re-executes into a plugin venv only in the legacy
  (plugin) runtime mode. In an installed package it stays in the interpreter that started
  it, and an explicit `argv` list never re-executes (source:
  `plugins/llm-kit/lib/llm_kit/cli/__init__.py`, `main`). This was read from source, not
  run on Windows: UNVERIFIED there.
- With no model registry configured, the installed package uses the shipped model list;
  `MODEL_ENDPOINTS_REGISTRY` overrides it (`package_mode_model_config` in
  `llm_kit/registry/models.py`; user ruling 2026-10-22 16:00). Pass the key through
  `OPENROUTER_API_KEY`, which has top precedence.

## 7. Consequence of retiring the `_shared_libs/llm_kit` publication

Planned, not shipped: `dev/tasks/shared-lib-packaging/plan-draft-end-state.md` unit C4
removes `shared_libs` from the llm-kit plugin's `bootstrap.json`, so bootstrap stops
publishing `llm_kit` into `~/.claude/plugins/data/<marketplace>/_shared_libs/llm_kit`. The
retirement is decided (D1 option (a); `dev/tasks/shared-lib-packaging/sol-ruling-end-state.md`,
cross-check answer 4) and planned as unit C4 in the final combined publish (P3 merged with P4).
It has not shipped; whether it has shipped is UNVERIFIED here, so check the
shared-lib-packaging task log for the publish before relying on either timing.

What follows once C4 ships:
- A fresh machine that installs llm-kit afterwards has no shared-lib `llm_kit`. A
  spiritcrossing project that reaches `llm_kit` through Mode 3a or 3b then fails at
  `import llm_kit` until it uses the PyPI install.
- Machines that already hold the tree keep a frozen copy, because trees are not deleted
  (plan draft, D1 option text). Do not treat that as a reason to wait: the frozen copy
  receives no updates, and it shadows the installed wheel while its `.pth` exists
  (section 3).
- Therefore spiritcrossing must complete this migration before C4 ships, or in place of
  relying on the shared lib. Do the migration first; the order has no downside because the
  PyPI packages are already released.

## 8. Acceptance: a clean-machine run

Done means the following run passes on a machine state that has no bootstrap and no
plugins-kit. The model is the databench acceptance run (log, 2026-10-22 23:30), which used
`env -i`, a scratch HOME, a fresh 3.12 venv and wheels from PyPI.

1. Start a clean environment: on macOS or Linux, `env -i` with only `HOME` set to an empty
   scratch directory and a minimal `PATH`; on Windows (the host appears to be Windows),
   a new shell with `USERPROFILE` and `HOME` set to an empty scratch directory and no
   `CLAUDE_*`, `BOOTSTRAP_*` or `PYTHONPATH` variables. The Windows form is UNVERIFIED.
2. Create a fresh 3.12 venv in a scratch directory and install spiritcrossing's pinned
   dependencies from PyPI (section 1), plus spiritcrossing itself.
3. With the venv interpreter run isolated (the `-I` flag), assert:
   - `llm_kit.__file__` and `katsoft_contracts.__file__` are under the venv's
     `site-packages`;
   - `importlib.metadata.version` reports the pinned versions for both distributions;
   - `import bootstrap_lib` raises `ModuleNotFoundError` (no bootstrap).
4. Import every `llm_kit` module spiritcrossing uses (section 5) and, for each, call the
   entry it relies on without a model call.
5. Run the model-free verbs of `loc.py` and `first_pass.py` through
   `SpiritCrossing/Scripts/python.bat` (name from the user's description; UNVERIFIED), skipping
   `first_pass.py refresh --ue`, which needs Unreal.
6. Record results in the spiritcrossing task log: what ran, what was skipped, and module
   origins. A run that skipped a path is reported as not tested.

Not testable without a model call or Unreal, so report them as untested unless you run
them separately: a live OpenRouter completion, a live `claude-cli` backend call,
`first_pass.py refresh --ue`, and full generation.

## 9. Open items to report back

- Which mode (3a or 3b) spiritcrossing used, on which machines.
- Spiritcrossing's Python version and target operating systems.
- Whether it routes by declaration (needs the contracts distribution; it is pinned).
- The list of imports from the modules in section 5.2.
- Any `bootstrap_lib` import found in section 2, step 4.
- Extras actually needed beyond `pipeline` and `sdk`.
