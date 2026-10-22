# databench and task-kit architecture review (2026-10-21)

Status: review by opus (read-only), awaiting the user's decisions in section 6.


This was a read-only review; I changed no files. **[C]** marks a premise I confirmed and gives the file:line I read. **[H]** marks a hypothesis I did not measure.

### 1. Overlap map

| Concern | databench | task-kit | Overlap |
|---|---|---|---|
| Server and launcher | FastAPI/uvicorn on fixed port 8765. It probes `/api/v1/instance` and attaches to a running server (README:2436-2449). It runs **in the foreground** of the caller's terminal (`cli.py:849-850,974`). `--restart` stops the server by bare pid (README:2451-2459) [C] | stdlib `ThreadingHTTPServer` on port 0, detached. Identity is pid + create time + nonce in `server.json`. It restarts when the code hash changes and re-adopts live runners. FastAPI was explicitly rejected (architecture.md:72-95, :73, :853) [C] | Full duplicate. task-kit has the stronger lifecycle; databench has the richer stack. |
| Browser open | `webbrowser.open` (`cli.py:642,783`) | Waits on the opener and classifies failures (dashboard.md:146-150) | Duplicate |
| Security | Loopback, Host check, write-intent header. Opt-in LAN/tailnet access with a token. System-open is refused to remote clients (README:2470-2519) | Loopback, per-instance token, Origin check, CSP, `MUTATION_ROUTES` table (architecture.md:130-153) | Two models. databench's remote mode is what the planned "ask the other machine" check needs (working-sessions-plan:481-487) |
| Projects | `dev_roots.yaml` ($DEVROOT minus exclusions; `tasks` is excluded, :13) plus `projects.yaml` | `project_directories` in `~/.claude/task.local.yaml` (`discovery.py:466-482`) | One fact, two configs |
| Repo identity | `normalize_remote_url` (`index_pages_survey.py:225`); one copy per origin (README:1175-1179) | `normalize_remote` (`repo_identity.py:79`); checkouts merged per origin (dashboard.md:7) | Duplicate |
| Generation and freshness | Markup labels via `llm-kit jobs`, keyed on the recorded HEAD. Index pages keyed on input sha, generator fingerprint, and the `llm_kit.pipeline` scheduler, gate and failure cache (README:1017-1047, 1132-1136; CLAUDE.md:79-88) | Today: summary with a fingerprint. Planned: Q&A answers keyed on an inputs fingerprint, stored as committed per-host records (plan D1) | Same pattern: generated output keyed on its inputs, with provenance |
| Model consent | `generate.yaml` `harnesses: {claude, codex}`; a disabled harness id is **refused** (`generate_config.py:72-81, 348-353`) | Planned `answers.spend`, free/paid split (plan:247-260) | Two switch designs for one policy |
| Background work | Starts markup refresh at server startup and on project open (`server.py:645-650, 689-694`) | Scheduler thread plus a one-minute session reconcile/publish pass (`server.py:245-249`; dashboard.md:140) | Both servers already host background work |
| Multi-machine state | Index pages are committed and pushed. Markup labels sit in a per-user cache (shared-generated-content plan:43-50) | Git is the truth; one writer per file (distributed-truth.md section 1-2, section 9) | The markup cache breaks principle 1; the active databench task `shared-generated-content` fixes it |

Side finding [C]: databench's markup launch sends the detached child's stderr to DEVNULL (`server.py:676-681`). plugins/CLAUDE.md:886 forbids this for plugins.

### 2. Dependency direction

Facts:
- **databench is not a plugin.** [C] Only its own project manifest installs it: an editable checkout at `$DEVROOT/databench`, with the launcher in `~/.local/bin` (`.claude/bootstrap.json` tool `databench-launcher`). No user-layer manifest names it (grep empty). [H] A machine that never starts a session in that checkout has no databench.
- **databench already uses plugin libraries.** [C] It links `llm_kit` and `bootstrap_lib` through `project_venv.shared_lib_imports` and declares `plugins: [plugins-kit:llm-kit]` (`.claude/bootstrap.json:27-39`). The owner ruled: "i would throw away the standalone install goal over putting code in this project that belongs in a plugin" (CLAUDE.md:889-932).
- **A plugin cannot install databench.** [C] Plugin dependencies are `dependencies`, `plugins[]` and `shared_lib_imports` (plugins/CLAUDE.md:425-427, 485-505). The only edge to a non-plugin repo is `git_deps`: "clone once; ... no steady-state pull" (bootstrap SKILL.md:229).

**(A) Leading option, recommended: task-kit is built on databench.** databench is the host: the process, the UI shell, the service loop and the switches. task-kit owns task semantics and exports them as a shared library. The code dependency therefore runs from databench to task-kit's library, through the same mechanism databench already uses for `llm_kit`. task-kit reaches databench only at run time (a PATH lookup and the instance probe), and degrades when databench is absent.

Costs of (A):
1. **task_system becomes a shared library.** It lives at `skills/task/scripts/task_system` and hard-imports `skills_kit_lib` (`discovery.py:83-84`). databench's venv would then need `skills_kit_lib` and psutil, plus openai if answers run in-process. A rename follows the deprecation policy. Shared libraries pin no version (plugins/CLAUDE.md:498-505), so databench must probe the newest symbol it uses. If the probe fails, the tasks view refuses and names the update command.
2. **databench's boundary rule must change.** README:25-27 says "Project-specific names, paths, and imports do not belong in this repository", and :43-44 admits domain semantics only through project declarations. It would need to allow first-party apps through a host adapter. databench already uses that pattern for index pages: a portable `index_pages` library plus an `index_pages_host` adapter (README:1138-1145).
3. **The UI is rewritten.** task-kit's vanilla JS (`static/app.js`) becomes a Preact view. Doing the detail-view redesign (rounds 3-4) there avoids building it twice.
4. **HTTP routes are ported.** The handlers in `server.py` (982 lines) become FastAPI routes, and each route must declare its path source (`path_sources`, README:152-162).
5. **databench must reach every machine** (see the facts above).

I found no blocker that makes (A) wrong; cost 5 comes closest.

Rejected options:
- **(B) task-kit imports databench**, through a git dependency in pyproject or `git_deps`. databench's `setup.py` builds the web client with npm. [H] A second copy inside task-kit's venv would also compete with the DEVROOT checkout for port 8765.
- **(C) A new shared lifecycle library.** It is not needed when databench is the only host. It also cannot go in `bootstrap_lib`, which must stay stdlib-only (plugins/CLAUDE.md:538-541), while the lifecycle code needs psutil.
- **(D) Stay separate.** That means two always-on processes and two UIs, against your stated direction.

### 3. Service integration

**What moves into databench:**
- **Service lifecycle:** detached start, identity record, restart on code change, stop protocol. Port these from task-kit's `launcher`/`procspawn`/`procstamp` (architecture.md:74-129), as `databench service ensure|stop|status`.
- **Tasks UI and routes:** a thin `tasks_host.py` adapter over the task library.
- **The background loop:**
  - the task scheduler; its runner interpreter can be injected (`scheduler.py:333-347, 1424`), so pass task-kit's venv interpreter;
  - the session reconcile/publish pass;
  - answer passes;
  - markup refresh, which is already there;
  - index pages, only if you choose that.
- **Hold handshake:** `task_hold.py` probes databench instead of task-kit's `server.json`.

**What stays in task-kit:**
- `task.py` and the skill.
- All task semantics: discovery, validation, revision-checked mutation, translation, acceptance, `runner_cli`, sessions and the publisher, and the Q&A catalog, readers, store, chain and pass engine.
- `task-kit characterize`.
- The SessionStart hook, which calls `databench service ensure` when databench is on PATH.
- The runtime DB and run directories, which stay in task-kit's data dir (plugins/CLAUDE.md:400).
- The `task-kit` command, which becomes "open the databench Tasks view" and refuses with the remedy when databench is absent. Retire task-kit's own server once the databench view has parity.

**Contracts that change or break:**
- **"No dependency on the dashboard running"** (principle 9, distributed-truth.md:92-97) still holds, because every verb, including `task.py answers refresh`, works without databench. Plan D4 rejected running answers "inside the dashboard server" (plan:399). It gave two reasons: principle 9, and "closing the dashboard must not stop it" (plan:304-310). A detached databench service meets both, so that row reverses. databench is foreground-only today [C], so it must gain a detached mode first.
- **Security:**
  - In remote mode, Queue/Retry (unattended Claude runs) and the run-token helper route would be reachable over the network. Make run-starting and authoring routes local-only, the same way system-open returns `403 system_open_remote` (README:2518-2519).
  - [H] databench's Host check plus write header protects against browser CSRF as well as task-kit's token does; a test should confirm it.
- **Ports and commands:** the fixed port replaces the OS-chosen one. `task-kit status/stop/restart` and their exit codes (README:70-79) move to `databench service`. architecture.md decision 1 ("task-kit owns the launcher, HTTP server, UI", :45) and the stdlib-HTTP decision (:73) are superseded.
- **Working sessions:** the files and verbs do not change; only the host of the reconcile pass does. [H] The sqlite handshake hold duplicates the git registration hold (dashboard.md:132). It could be dropped except where registration is refused: Windows, and `record: false`.
- **Runs:** they survive a server restart (dashboard.md:158), so databench re-adopts them the same way.

### 4. The three switches (owned by databench, per your ruling)

**What llm-kit can do today** [C]: `describe` and `run` take `exclude=` (`declaration.py:520-530, 597-598, 870-876`). Each entry's class can be derived from registry facts:
- **Free:** a transport entry with `billing.mode: unmetered` (`model_endpoints.py:47-48, 464-467`). This matches the fleet rule in `is_paid` (`frontdoor/acceptance.py:90-92`). A front-door entry counts as free only if every one of its tiers is unmetered; an undeclared tier is paid (:15-25).
- **Usage:** a claude or codex harness entry (`model_endpoints.py:55-58, 107-108, 151-152`). These are billed to the subscription (`codex_backend.py:5-7`, `backends.py:633-634`). Caveat: `ANTHROPIC_API_KEY` and similar variables divert a claude harness to metered billing (`claude_bg.py:177-197`), so the classifier should then report the entry as spend.
- **Spend:** every other transport, such as the OpenRouter `or-*` entries (`models.py:124-143`). [H] opencode harness entries also belong here, since they use the providers in your own opencode config (`models.py:96-99`).

**Recommended llm-kit addition (small):** a `cost_class(entry, env)` function, plus an `allow=` set on `describe`/`run`. Entries the switches remove would get a new `switched-off` disposition that is shown in the output. Today `excluded` entries are hidden (`declaration.py:85-96`), so status could not say "waiting: only usage entries remain".

**State, in databench:** the per-machine `generate.yaml` (README:1216-1218) gains `regeneration`, `usage` and `spend`, replacing `harnesses: {claude, codex}`. Migration: if either harness was true, `usage` becomes true.

The semantics change from refusing a config at load (`generate_config.py:348-353`) to filtering at dispatch, with disclosure. An operation whose model list filters to empty is skipped with a status line instead of exiting 2.

**How the switches combine:** for each operation, the allowed models are the operation's declared list, keeping free entries always, usage entries only when `usage` is on, and spend entries only when `spend` is on. Declared order remains the preference order. `regeneration` decides only whether the service runs the operation in the background. It does not affect a by-hand run.

databench applies this filter to markup job lists, to the index-pages judgment and generation lists, and to the task answering it hosts. D2's two-phase "free first" dispatch becomes optional: the fleet declaration already lists qwen first.

**What task-kit configures:** only what to ask -- the question catalog, per-question overrides, `answers.models`, effort and batch size. Drop `answers.service` and `answers.spend` (plan:312-316).

**The by-hand path when databench is not running:** `task.py answers refresh` reads the same switch file, read-only. That makes the file a one-path contract (plugins/CLAUDE.md:400-415) and duplicates databench's config-root resolution in about 10 lines. If the file is absent or unreadable, only free models run, and the output says so. No databench process is needed. The consent text (plan:252-260) applies to both usage and spend, because both send task text to OpenAI or Anthropic.

### 5. Staged migration

- **S0, docs only:** record the rulings. Amend databench's README boundary rule and CLAUDE.md map, and architecture.md decision 1.
- **S1, ships first (most value for the cost):** llm-kit `cost_class` plus `allow=` / `switched-off`, and databench switch file v2 with filter semantics. This unifies spend control for databench's existing generation straight away.
- **S2:** task-kit exports task_system as a shared library, with deprecation shims. Q&A units U1-U5 and U7 keep their substance; U3 uses the classifier and reads the switch file. Orientation answers then work through `review` and `answers refresh` with no service running.
- **S3:** databench service mode -- detached lifecycle, ensure/stop/status, background loop gated by `regeneration`. It hosts answer passes and markup. Fix the DEVNULL stderr. Add the start triggers, and solve databench's reach to every machine.
- **S4:** a read-only Tasks view in databench (list, and detail with answers). `task-kit` opens it.
- **S5:** authoring routes, scheduler and runners, the helper route, session reconcile, the handshake retarget, and local-only enforcement. Then retire task-kit's server, static files and launcher internals.
- **S6:** land `shared-generated-content` (tracked labels) before turning on markup regeneration on several machines. Without it, every machine pays for the same labels and keeps a private copy of a shared fact.

**Changes to `summary-on-commit-plan.md`:**
- **D2:** use three classes: free, usage, spend. `luna` and `haiku` are harness entries (`registry/models.py:104-107, 153-156`), so `usage` gates them; `spend` gates metered API entries only.
- **D4:**
  - The host becomes the databench service.
  - Delete `task-kit service ...` and the service state under task-kit's data dir (`service.json`, `service.lock`, `service.stop`).
  - The hook calls `databench service ensure`.
  - Reverse the "inside the dashboard server" row, with its reasons.
  - Keep the pass algorithm (prepare, answer, commit) as task-library code that databench calls.
- **Switches:** remove them from `task.local.yaml`; point at databench's file; `regeneration` replaces `service`.
- **Units:** U6 moves to the databench repo and U8 to the databench UI. Add an llm-kit unit before U3. Move the razor gate entries for the switches to databench.

### 6. Your decisions

1. **Service and UI host.** Should databench become the always-on per-machine service and host the tasks UI, with task-kit's own dashboard retired once the databench view has parity rather than kept as a fallback? This changes databench's "no domain code" boundary.
2. **How databench reaches every machine.** Add a user-layer bootstrap entry for the DEVROOT checkout, or make databench a plugins-kit plugin?
3. **Switch granularity.** One set of three switches per machine, or one set per operation (task answers, content markup, index pages)?
4. **Index pages in background regeneration.** They are expensive, and each run pushes commits to every repository's origin. If they are included, the `generated_index_pages` insight in USER-PREFERENCES.md (regenerated "at the user's discretion"; agents do not schedule them) needs rewording so that the switch is that discretion.
5. **Remote access to task actions.** In tailnet mode, which task actions may a remote client use? I recommend view only, and never Queue.

## Addendum: a layer below databench (unit databench-taskkit-arch)

This was read-only; I changed no files. **[C]** marks a confirmed premise, **[H]** a hypothesis.

**Recommendation:** add one small lower layer, inside llm-kit. It holds the model-filter policy and the per-machine usage and spend settings. Nothing else needs a shared layer.

### What goes into llm-kit, and why it pays for itself

1. **Classifier and filter** (this is S1 in my earlier report, unchanged).
   - Add `cost_class(entry, env)` returning free, usage or spend.
   - Add an `allow=` set on `describe` and `run`, with a visible `switched-off` disposition.
   - The facts it uses are already llm-kit's [C]:
     - `kind`, `harness` and `billing_mode` in `model_endpoints.py:107-108, 151-160, 464-467`;
     - the fleet paid rule `is_paid` in `frontdoor/acceptance.py:90-92`;
     - the variables that divert a claude harness to metered billing, in `claude_bg.py:177-197`.
   - Every consumer would otherwise re-implement this: databench's generate, `task.py answers refresh`, and any later unattended caller.

2. **Storage and reader for `usage` and `spend`** (this replaces part of my earlier section 4).
   - Earlier I put all three switches in databench's `generate.yaml`. That means task-kit must copy databench's config-path resolution, a file contract across two repos that no shared test covers.
   - Instead, llm-kit owns one per-machine file in its own data directory, with `load_consent()` / `save_consent()`.
     - llm-kit already keeps per-machine state there for usage pacing (`dispatch/usage_budget.py:112`) [C].
     - That directory is the home plugins/CLAUDE.md:400 assigns to user- and machine-scoped plugin data.
   - databench's UI shows the checkboxes and writes the file through `save_consent()`. That keeps them databench controls, as you ruled. The databench service passes `allow=load_consent()` for every operation it hosts.
   - The `task.py` refresh you run yourself calls the same function, with no databench process and no duplicated path. If the file is absent, only free models run, and the output says so.
   - There is a second payoff. The two booleans are the simplest form of two mechanisms llm-kit already has:
     - `usage` mirrors subscription pacing (`conserve_usage`, `usage_budget.pinned_evaluate`);
     - `spend` mirrors the USD cap (`pipeline/runtime/spend_ledger.py:485`).
     If you later want "spend up to $X a day" instead of on/off, it lands where those mechanisms already live.
   - **Scope rule:** the filter is opt-in per caller. Interactive callers keep their current routing, so orchestrate and the review lanes are not gated by a background-regeneration setting. Those are the session-caller paths in `declaration.py:534-536`.

3. **The `regeneration` switch stays in databench.** It controls whether databench's own service runs background passes, and it never gates a refresh you run yourself. It has only one reader.

### What I considered and would not put in a lower layer

- **Service core** (detached start, process identity, ensure/stop, the background loop).
  - After the migration databench is its only host, so the code should simply move into databench.
  - It cannot go in `bootstrap_lib`, which must stay stdlib-only (plugins/CLAUDE.md:538-541), while this code needs psutil.
  - A new plugin to hold it would cost more than it saves.
  - Revisit only if a second long-running host appears. [H] llm-kit's front-door server could be one; I did not measure its lifecycle.
- **Repo-identity normalization** (task-kit `repo_identity.py:79`, databench `index_pages_survey.py:225`).
  - databench's copy has to run on a bare interpreter (README:2396-2399), so it cannot import a `.pth`-linked library.
  - The duplicate is small. Leave both, or let databench's host adapter use task-kit's copy.
- **Freshness and provenance record formats** (index-page decision records, markup labels, task answer files).
  - The three differ in who owns and writes them: commits from a private worktree, a per-user cache, and per-host committed files.
  - A shared format would couple three schemas for little reuse. The per-attempt record already comes from llm-kit's `RunResult.attempts`.

### Changes to the earlier report

- **section 4:** the per-machine `usage` and `spend` state moves from databench's `generate.yaml` to llm-kit's data directory, still written through databench's UI. `regeneration` stays in databench. `task.py`'s refresh path calls `llm_kit` instead of copying a databench path.
- **section 5:** S1 adds `load_consent` / `save_consent` and the consent file to the llm-kit unit. databench's switch-file v2 then carries only `regeneration`, plus the migration that turns the old `harnesses: {claude, codex}` into the new `usage` value. Edit `summary-on-commit-plan.md` D2 and D4 to read consent from llm-kit.
- **section 6, one added decision for you:** should every unattended llm-kit caller honor the usage and spend consent by default (job runs, workflow-kit nodes), or only the callers databench hosts plus `task.py`'s refresh path? I recommend opt-in per caller now, and widening it only if you ask.

## User decisions (2026-10-21, verbatim answers)

- Host: "Yes, retire task-kit's (Recommended)".
- Reaching every machine: "Can I distribute databench without source code I.e. via npm" (open; see distribution investigation).
- Switch granularity: "One set per machine (Recommended)".
- Index pages in background regeneration: "A drop down configures what type of regen is enabled. It all is by default."

## Distribution investigation (sonnet, read-only, 2026-10-21)

**Short answer:** Yes, you can ship databench without its source. The practical way is a frozen per-platform binary, because npm and wheels both ship readable source. The harder problem is that databench imports `llm_kit` and `bootstrap_lib` at import time. That must be decided before freezing. I did not check task-kit's task_system, so the task integration is untested. Items marked (general) are general knowledge, not from the repos.

### 1. What a frozen binary must bundle

Python dependencies (`/Users/christina/Dev/databench/pyproject.toml`):
- `fastapi>=0.115`, `pillow>=11,<13`, `pyyaml>=6`, `uvicorn>=0.34`. Requires Python 3.12 or newer.
- Pillow is the only native wheel. (general) PyInstaller handles it, and uvicorn needs its protocol modules listed as hidden imports.
- Package data is `assets/*.css`, `dev_roots.yaml` and `index_pages_refs/*.md`. These are read through `importlib.resources` (`dev_roots.py:288`, `index_pages.py:758`), which works in a frozen build once the files are declared as data.

Web client:
- `setup.py:15-31` runs `npm ci` and `vite build` into `databench/static` during `build_py`. The server serves `Path(__file__).with_name("static")` (`server.py:593-598`).
- Node is therefore needed only at build time. There is a built `dist/databench-0.1.0-py3-none-any.whl`.
- From a checkout, `cli.py:726-747` rebuilds `web/dist` on demand. A frozen build must take the packaged static path.

Source-hash self-reads that break or change when frozen:
- `index_pages_generation.py:962` hashes `Path(__file__).read_bytes()`.
- `index_pages_worktree.py:845-882` globs `index_pages*.py` from `_PACKAGE` to compute a fingerprint.
- In a PyInstaller build there are no `.py` files, so these fail or fingerprint something different. They need rewriting, for example to use a build-time hash constant.

Shared libraries:
- `index_pages_runner.py:161-163` does `from bootstrap_lib import execution_event` and `from llm_kit.pipeline.execution import deadlines, failure_cache, scheduler`, plus `llm_kit.pipeline.runtime.gate`.
- `_require_shared_libraries()` runs at import (line ~156). It pins `LLM_KIT_VERSION = "0.1.9"` and `bootstrap 0.135.0` (lines 72-73) and probes signatures.
- It also shells out to the `llm-kit` CLI via `resolve_llm_kit()` (lines 690-720). That path looks in `~/.claude/plugins/data/plugins-kit/llm-kit/.venv/bin/llm-kit`, then PATH. This is already the subprocess pattern.
- `.claude/bootstrap.json` links them with `project_venv.shared_lib_imports` (`llm_kit`, `bootstrap_lib`). It also declares the plugin `plugins-kit:llm-kit` at `min_version` 0.1.9 and a launcher that runs the editable checkout.
- Task-kit: `grep` shows no task_system import in databench `src` today, so the task integration does not exist yet.

### 2. Distribution options

**(a) PyInstaller or Nuitka binary, shipped as npm platform packages (esbuild pattern)**
- Source hiding:
  - PyInstaller ships `.pyc` inside the archive, which (general) is trivially decompiled. It is obfuscation, not protection.
  - Nuitka compiles to C and is materially harder to reverse, but still not secure. The web JS bundle is minified but readable in any browser.
- Update path: `npm i -g databench` pulls the optional dependency matching the OS and CPU (`@databench/darwin-arm64`, `win32-x64` and so on). Updates are `npm update`, or bootstrap if it manages the npm global.
- Reach: needs Node on every machine. Node is already a bootstrap tool (`node18` in `bootstrap.json`).
- Cost: a 3-platform CI matrix. Each needs a native runner, since neither tool cross-compiles. Binaries will be 40-80 MB each (hypothesis). Windows and macOS need code-signing or notarization; unsigned macOS binaries may be quarantined if downloaded through a browser. npm publishing needs a registry, which can be private.
- Note: "without source" means the registry contains no source. You are not forced to use public npm.

**(b) The same binary through bootstrap's `download:` recipes**
- From `manifest-reference.md:452-476` and `:826`, a tool entry takes `download.url` plus `download.sha256`. The archive is extracted to `~/.local`, with a per-OS nesting allowed. `check` and path linking come with it. A failed download falls through to `install.<os>.command`.
- Evidence gap: the `download:` mechanism is documented for tools and fonts. I did not verify that it handles a single executable (as opposed to an archive that holds one) or that it makes the file executable.
- Source hiding is the same as (a).
- Update path: bump the URL and sha256 in a manifest. Bootstrap's `check` probes the installed version, which is how a version bump is detected (verify with a test).
- Reach: this is the fleet's existing channel and needs no Node or npm. It matches the "bootstrap owns dependency provisioning" rule (`CLAUDE.md:49`).
- Cost: you need a host for the artifacts, for example GitHub Releases, and for a private repo an auth token. The recipe uses a plain `url`, so private-repo auth is unverified. You also pay the CI matrix and signing.
- This is the cheapest path to every machine.

**(c) Wheel or pipx**
- Ships Python source, so it does not hide it. The `.whl` already exists, and `setup.py` bundles the client.
- Update with `pipx upgrade`. It is easy and gives `llm_kit` as plain imports through the pipx venv.
- It does not meet your goal.

**(d) Make databench a plugins-kit plugin**
- Source ships in the plugin cache, so it is visible. It would reach every machine via the marketplace and bootstrap.
- It gives you shared_lib linking for free and a venv per plugin.
- It conflicts with always-on service use and with `plugins/CLAUDE.md:457` on plugin boundaries. It also puts the whole Vite app in the plugin tree.
- Choose it only if source visibility is acceptable.

### 3. The integration constraint

A frozen binary cannot use `.pth` links. `_require_shared_libraries()` would raise at import. Options:

1. **Bundle at build time.** Freeze `llm_kit` and `bootstrap_lib` into the binary at pinned versions.
   - This is robust and self-contained. But the binary carries a copy that drifts from the plugin.
   - It conflicts with the `CLAUDE.md:427` rule that cross-plugin imports declare REQUIRED, REFUSE or DEGRADE, with the probe against the newest symbol. A bundled copy is "always present", so that rule collapses to a build pin.
   - It also bypasses the "a shared-lib link pins no version" stance.
   - The runner already hard-codes minimum versions, which gives you the pin mechanism.
2. **Subprocess CLIs with JSON contracts.** Call `llm-kit` (already done via `resolve_llm_kit()`), `task-kit/bin/task-kit` (exists) and `models describe`.
   - This fits freezing best and fits the cross-plugin rule, since the CLI is the boundary.
   - Cost: the runner's scheduler, deadlines and gate usage are in-process library calls (`index_pages_runner.py:161-163`), not CLIs. That code would have to be redesigned or stay bundled (option 1).
   - Task-kit has a CLI entrypoint (`plugins/task-kit/bin/task-kit`). Whether it has JSON output for the tasks UI is unverified.
3. **A plugin-provided extension loaded from a path at runtime.**
   - (general) PyInstaller does allow `sys.path` additions and importing external `.py` files at runtime. The frozen binary embeds its own interpreter and stdlib, so the external code must be pure Python and compatible with the bundled version (3.12 in this case).
   - It would work for `llm_kit` and `bootstrap_lib` only if their native dependencies are also present in the bundle.
   - It re-creates the version-skew risk, with no ImportError diagnosis. Hypothesis: not worth it.

**Fit for a sole-user fleet:** option 2 for the task-kit tasks UI, which is new code with no legacy. Keep llm_kit and bootstrap_lib bundled under a pin (option 1) for the existing index-pages runner, until it is refactored to CLIs. That respects the plugin boundary and avoids version skew. Both are lower risk than (3).

### 4. Recommendation and smallest first step

Use (b): a Nuitka or PyInstaller binary per OS and CPU, released as artifacts and installed by a `download:` recipe in the fleet bootstrap manifest. Add npm platform packages later only if you want a public channel. Task-kit integration goes through the `task-kit` CLI as a subprocess. Bundle `llm_kit` and `bootstrap_lib` at a pinned version for now. Hypothesis: this is the least total work, but confirm each assumption below.

Smallest first step, on macOS arm64 only:
1. Build one PyInstaller onefile binary of the current `src/databench/cli.py` with the static client, plus the two shared libs bundled in.
2. Fix the `__file__` hash and glob at `index_pages_generation.py:962` and `index_pages_worktree.py:845-882`.
3. Run `databench --version` and the web UI from a directory with no checkout and no `.venv`.
4. If that works, test whether the `download:` recipe accepts a single executable from a local `file://` or private URL.

Open questions I did not resolve: which license the project wants, how code-signing is handled, and whether the private-repo download needs a token.
