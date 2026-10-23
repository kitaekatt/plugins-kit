# databench integration plan: a modular databench with a tasks module (2026-10-22)

Status: plan only (units `revise-databench-plan-modules` and `fix-databench-plan-xcheck`,
2026-10-22; the second applies a codex/sol cross-check of `b7e92e26`). No code changed. M7 was re-planned on
2026-10-22 against databench `b526cab` and task-kit 0.17.0 (its section states the code facts).
This revision replaces the stage plan committed as `adced200` (stages S2 to S6 and a consent-default
stage); section 9 says what was dropped. Code facts are as of plugins-kit dev `adced200`
(task-kit 0.13.0, llm-kit 0.4.0) and databench `902d6ef`. plugins-kit paths are
repository-relative; databench paths start with `databench:`.

Sources: `databench-integration-review.md`, `summary-on-commit-plan.md`, `detail-view/`
(round 4; round 5 is being drawn separately), `docs/planning/llm-kit/cost-class-consent-plan.md`,
and the rulings in the task-kit-extraction `plan.md`.

## 1. Rulings this plan carries (2026-10-22, verbatim)

Direction, in order:

> ok i was thinking something similar lets talk this through a bit more.
> Task kit remains a fully functional plugin.
> All code related to the manager lives in databench.
> However, an implication of this is I want to make databench have different modules that can be
> enabled or disabled (we will later maker it so these can be installed and uninstalled, for now
> let's just make it enabled/disabled).
> This approach requires
> (1) identifying what should be core to databench
> (2) identifying what modules should exist, understanding that any module that exists must be
> able to run without dependencies on other modules
> My own thought is:
> - there's an explorer module which covered browsing repositories and generating summary data of
>   what files are
> - there's an html module which covers creating index.html files throughout the repository
> - there's a tasks module which covered showing the task dashboard

Follow-up answers (bracketed text is the question's context, not the user's words):

> (1) it sounds like the concept of a file summary is core, the data is the same, both explorer
> and html can generate these summaries when they need them, and both don't care how the data was
> generated. A task dashboard wouldn't generate these summaries because it doesn't need this data
> (or maybe it would generate summaries of just task files)

> (2) I agree comments should be core

> (3) sounds good [cross-module links are soft: shown only when the target module is enabled,
> through core's registry; no module-to-module imports]

> (4) sounds good [independence is enforced by an import-lint test plus each module's tests
> running with every other module disabled]

Answers to the previous revision's questions:

> (q1) the databench consent configuration is for databench and its modules only. Plugins-kit
> will manage consent in its own way and not use databench's settings. I don't think we need to
> make consent configurable for plugins-kit modules right now we could but I don't think it's
> necessary.

> (q2) we absolutely can and will ship databench with some consent on by default we have no
> restrictions on what our default configuration is.

> (q3) yes but we aren't focused on databench performing work on tasks right now, we shouldn't
> over invest here until the user need emerges. There is too much to do.

> (q4) open the pdf in my browser and i'll choose

Then, on the round-4 concepts: "C" (intent lenses).

> (q5) why are you asking this question

(Q5 is dropped: the tasks module follows core's access policy like every module.)

> (q6) on by default

On the tasks view:

> i do think we should unify the look and feel with databench in terms of style so can you please
> do a mockup of that. Also there shouldn't be a spend control there I think we have databench to
> configure spend

On task-kit's dashboard (the previous revision's Q2):

> i agree task-kit should remain entirely functional as it is today without the dashboard
> running. It can provide necessary interface functionality so that databench can interface iwth
> it.

On databench's shipped consent (the previous revision's Q1):

> databench ships with usage on spend off

Still binding: "ok that particular concern is offloaded, for now it's just fine that databench
requires plugins-kit and bootstrap to be installed, later we will switch to .venv if we implement
this task" (`dev/tasks/shared-lib-packaging`); index pages "A drop down configures what type of
regen is enabled. It all is by default."; switches "One set per machine (Recommended)". The
earlier "All callers by default" consent answer is scoped by q1 to databench and its modules:
llm-kit's own default (no filtering unless a caller passes `allow`) is unchanged.

## 2. Premise outcomes

**[E]** = measured or read by this unit.

| Premise | Outcome | Evidence |
|---|---|---|
| `filesystem.py` imports `index_pages` | CONFIRMED [E] | `databench:src/databench/filesystem.py:21` (`PAGE_FILENAME`, `is_generated_page`, `parse_marker`, `MarkerError`); it also parses the `index_pages` config block (`:489-581`) and lazily imports `entities` |
| `paths.py` imports `index_pages` | REFUTED [E] | `paths.py` imports only the standard library (`:17-21`); the edge is the reverse, `index_pages -> paths`. Its docstring mentions `index_pages` |
| `server.py` imports `index_pages` and `index_pages_host` | CONFIRMED [E] | `server.py:28-29` |
| `index_pages_cli` imports `markup` | CONFIRMED [E] | `index_pages_cli.py:132` (`_cache_root`, `_file_lock`) |
| `generate_cli` drives markup and index pages | CONFIRMED [E] | `generate_cli.py` imports `markup`, `index_pages_cli`, `index_pages_survey`, `index_pages_worktree`; it builds the phase model lists itself (`:455-458`) |
| Moving or editing a fingerprinted file makes every repository's index pages stale | REFUTED as stated, here and in the `adced200` revision of this plan [E] | section 6 |

Full intra-package import graph [E] (AST over `databench:src/databench/*.py`; edges from core
files into future module files): `cli -> entities, entity_edit, index_pages_cli`;
`filesystem -> index_pages, entities (lazy)`; `server -> entities, entity_edit, index_pages,
index_pages_host`; `generate_cli -> index_pages_cli, index_pages_survey, index_pages_worktree`;
`generate_config -> index_pages_runner (lazy, for resolve_llm_kit)`. No edge runs between the
future explorer and html files; `index_pages_cli -> markup` becomes a module-to-core edge because
file summaries are core (ruling 1).

## 3. Core and modules

The rule: a module imports core and its own files only; anything two modules need is core; a
disabled module is never imported (eagerly, lazily or by literal `importlib`).

| Part | Files (databench `src/databench/`) | Why |
|---|---|---|
| core: host | `cli.py` (front door), `server.py` (app shell), `modules_registry.py` (added), `module_manifest.py` (added), `path_sources.py`, `llm_gateway.py`, `generation.py` (added in M7-U1), `service_cli.py` (added in M7-U1), `background.py` (added in M7-U7) | runs the process and loads enabled modules |
| core: workspace | `projects.py`, `dev_roots.py` (+ `dev_roots.yaml`), `roots.py`, `filesystem.py` (root resolution, exact spelling, reads), `paths.py`, `ignore_rules.py`, `path_walk.py`, `store.py`, `system_viewer.py`, `workspace_scope.py` (added in M1: which repositories `generate` covers) | every module resolves projects, paths and repositories the same way |
| core: access | `remote_access.py` | one access policy |
| core: comments | `comments.py`, `comment_locations.py` | ruling (2) |
| core: file summaries | `markup.py` (label store, work list, generation through `llm-kit jobs`), `label_publisher.py` (added in M7-U5), `generate_config.py`, `generate_cli.py` (the `generate` front door over core's and the modules' generation phases) | ruling (1); the html module's run state already shares markup's cache root and lock |
| core: raw read | the raw route and its CSP contract | explorer's viewers and html's page viewer both read raw |
| explorer | `entities.py`, `entity_edit.py`, `explorer_module.py` (added); browse, tree, context and raw-write routes; entity CLI operations; entity declarations, loaded through explorer's root loader; viewer components | browsing and editing |
| html | every `index_pages*.py`, `index_pages_refs/`, `assets/`, `html_module.py` (added) | index pages |
| tasks | `tasks_module.py`, `tasks_host.py` (added) | the tasks view, through task-kit's JSON CLI |

Web client (`databench:web/src/`): the shell (`main.tsx` router, projects, session, ui, comments,
markup labels) is core; `web/src/modules/explorer/`, `web/src/modules/html/`,
`web/src/modules/tasks/` hold the views. Every module uses the shell's styling (ruling on look and
feel); none ships its own.

**Registry interface (core; as built in databench `362ddfd..8280919`, plus the M1-U6 to U8
additions marked "added"):**

```python
MODULE_SPECS = (ModuleSpec("explorer", "Explorer", "databench.explorer_module:register"),
                ModuleSpec("html", "Index pages", "databench.html_module:register"))
                # M2-U2 appends ModuleSpec("tasks", "Tasks", "databench.tasks_module:register")
class Host:                                   # what register(host) may call
    roots: RootRegistry                       # bound once the app exists; handlers read it per request
    def route(self, method, path, handler, *, path_source, write=False, **options) -> None
        # options are FastAPI route options; the path must start with /api/; write is True
        # exactly for POST, PUT, PATCH and DELETE, and the server then requires core's
        # write-intent header; local-only rules stay core's and a module cannot widen them
    def cli(self, operation, build_parser, run) -> None
        # build_parser given: the operation shares Databench's grammar, build_parser(parser)
        # adds its options and run(args, parser) runs it; build_parser None: the operation
        # parses its own argv and run(argv) receives the whole `databench [<dir>] <op> ...`
    def view(self, view_id, title, *, nav=True) -> None    # client route /-/<view id>
    def context_contributor(self, fn) -> None
    def root_loader(self, fn: RootLoader) -> None          # added (M1-U6), one per module
    def generation(self, phase: GenerationPhase) -> None   # typed by M1-U7
    def enabled(self, module_id) -> bool                    # soft links between modules
# fn(kind, path, probe) -> Mapping[str, object], kind "directory" | "file" | "entry". probe is
# filesystem.ListingProbe: names(directory) -> frozenset | None and visible(path) -> Path | None
# (exact spelling and visibility, the request's names cache), kind, content (the bytes a file
# context already read, b"" otherwise) and raw_view_exclude. A "viewer" mapping merges into the
# file viewer; Raw precedence: a configured raw_view.exclude decides Raw for every file, else a
# contributed raw_allowed decides, else Raw is allowed.
@dataclass(frozen=True) class RootLoad: error: BrowseError | None; data: object        # added
RootLoader = Callable[[BrowseRoot], RootLoad]                                           # added
RootLoaders = tuple[tuple[str, RootLoader], ...]                                       # added
Host.root_loaders() -> RootLoaders     # an immutable snapshot, canonical module order  # added
# No process-wide registry: every root is built with an explicit snapshot.
# BrowseRoot.from_directory(directory, *, loaders: RootLoaders) -- `loaders` is a REQUIRED
# keyword (core-only callers pass ()), so no call site can skip it silently. It runs the
# loaders only when the core configuration, ignore rules and markup overrides loaded cleanly;
# the first loader error becomes configuration_error and stops the rest; each loader's data
# lands in BrowseRoot.extensions[module_id]. RootRegistry takes the snapshot once and uses it
# for the default root and every lazily resolved project root; markup.markup_refresh and
# markup.markup_accept take it as a required keyword too; a module's CLI `run` reads
# host.root_loaders() through its registration closure.
class GenerationPhase(Protocol):                                                        # added
    id: str                                   # also the regeneration type id (M7)
    title: str
    def check(self, models: Mapping[str, Sequence[str]]) -> str | None   # before any write
    def run(self, scope: Scope, targets: Sequence[tuple[str, Path]],
            models: Mapping[str, Sequence[str]]) -> PhaseResult
@dataclass(frozen=True) class PhaseResult: status: int; recap: str                     # added
def load_enabled(path=None) -> tuple[str, ...]   # <config root>/databench/modules.json
def register_enabled(host, enabled) -> None      # imports enabled entries only
GET /api/v1/modules -> {"enabled": [...], "views": [{"module", "id", "title", "nav"}]}
databench modules list | enable ID | disable ID
databench --view VIEW_ID        # fresh server: the CLI's enabled modules decide; attached
                                # server: the views it announces; --restart checks the view
                                # before stopping the running server
```

As built in M1: explorer registers the browse, tree, context, raw-write and entity routes, the
`write`, `entities` and `entity-set` CLI operations and the `explorer` view; html registers the
index-page record and style routes, the `index-pages` operation (own grammar) and its context
contributor, and no web view (its page viewer is used inside explorer's file view, behind the
`index_page` key); module navigation shows only when more than one view has `nav`.

## 4. Stage order

| Order | Stage | Repos | Depends on | User-visible when shipped | An early stop leaves |
|---|---|---|---|---|---|
| 1 | M1 Core split | databench | none | `databench modules list|enable|disable`; disabling explorer or html removes its views, routes and CLI operations, and core runs with no module enabled | a core that runs alone, two switchable modules, and a lint that blocks any further coupling |
| 2 | M2 Tasks module, read-only | plugins-kit (task-kit), databench | M1 | Tasks in databench's header: every project's tasks, project dropdown, sections, needs, items, working sessions (unverified tinted); detail with Orient, Decide and Trace lenses in databench's styling; works with explorer and html disabled; `databench modules disable tasks` | tasks visible in databench; task-kit and its dashboard unchanged |
| 3 | M3 task-kit dashboard cutover | plugins-kit (task-kit) | M2 shipped | `task-kit` and `/task dashboard` open databench's Tasks view; task-kit's browser dashboard and its Queue and Run are gone; every `task.py` verb and the skill work as before | two task views: databench's (read-only) and task-kit's (with runs) |
| 4 | M4 Web module split | databench | M2 | none in behavior; a disabled module's web code is no longer downloaded | module files sit in `web/src/modules/`, but the shell still imports them, held by the web allowlist |
| 5 | M5 Task answers | plugins-kit (task-kit), databench, claude-settings | M2 | the detail lenses show orientation and decisions answers with answerer, as-of commit and a stale marker; `task.py answers refresh` | answers refreshed by hand or by `task review` |
| 6 | M6 Shared file summaries | databench | M1 | file summaries tracked in `.databench/`, shared through git | summaries shared; nothing regenerates in the background |
| 7 | M7 Generation service | databench, plugins-kit (task-kit scoped answers and hook), claude-settings | M1, M5, M6 (landed); Q2 and Q4 gate M7-U3b; Q3 sets only the `publish.labels` default | the always-on server runs the generation phases over each selected scope; a settings panel with regeneration (on by default, type drop-down, all types selected), usage (on by default), spend (off by default) and label publishing; unattended label runs write an overlay and, on a Q3 yes, a publisher pushes through a private worktree; `databench ensure` and a task-kit hook start the server | (end of planned work) |
| 8 | M8 Running work on tasks (deferred stub) | -- | a user need (q3) | -- | -- |

M1 carries the whole backend extraction of explorer and html, because the tasks module must ship
running with both disabled and no expected isolation failure; a core that still imports them
(`server.py:24-29`, `cli.py:38-44`, `filesystem.py:21`) cannot do that. M2 then puts tasks inside
databench, and M3 removes the second task view (ruling of 2026-10-22 on task-kit's dashboard).
M4 makes the web client follow the module rule and has no user-visible change, so it can wait. M5 precedes M6 and M7 because
answers are the detail view's main content and run by hand without a service.

## 5. Stages

Every unit owns the files it names; units that may run at once share none. Every check names the
revert that must turn it red (`docs/reference/vacuous-checks.md`). Every databench unit's
verification runs the fingerprint verifier of section 6.

### M1 Core split

**Landed (databench `362ddfd..8280919`, pushed; fingerprint verifier against `902d6ef`:
unchanged):** M1-U1 registry and per-machine enabled set; M1-U2 import-lint, web boundary test and
fingerprint verifier; M1-U3 explorer backend; M1-U4 html backend (routes, `index-pages`
operation, context contributor, core's `llm_gateway.resolve_llm_kit`); M1-U5 module-aware shell,
`--view` and the isolation harness. The interface as built is section 3. Two allowlist groups
remain, each a gap in the earlier revision of this plan: `filesystem.py -> entities.py` (entity
declarations load when a root is built, which a per-request contributor cannot express) and
`generate_cli.py -> index_pages_{cli,survey,worktree}` (scope resolution, the dev root's
repository list and target selection serve the markup phase too). The isolation test still
explains `entities.py` through that edge. M1-U6 to U9 close them; they run in order U6, then U7
and U8 together, then U9.

**M1-U6 root loader; entities stay explorer's.** Decision: entity declarations are explorer's
input (entity types, lists, details and edits); no core file reads them (comments, markup and
store do not), so with explorer disabled a malformed declaration has nothing to block. Owns
`databench:src/databench/modules_registry.py` (`RootLoad`, `RootLoader`, `RootLoaders`,
`Host.root_loader`, `Host.root_loaders`), `databench:src/databench/filesystem.py`
(`BrowseRoot.extensions`; `from_directory(directory, *, loaders)` as section 3 states; the
`entity_types` field and the lazy `entities` import go), `databench:src/databench/roots.py`
(`RootRegistry(..., root_loaders=...)` for the default and every lazy root),
`databench:src/databench/server.py` (passes `host.root_loaders()` to the registry),
`databench:src/databench/cli.py` (passes the snapshot at every core root construction),
`databench:src/databench/markup.py` (`markup_refresh` and `markup_accept` take `loaders`),
`databench:src/databench/generate_cli.py` (passes the snapshot to markup roots; U7 then owns
this file), `databench:src/databench/explorer_module.py` (registers a loader wrapping
`entities.load_entity_declarations`; its CLI operations pass `host.root_loaders()`),
`databench:src/databench/entities.py` (`_LoadSession` and `list_entity_types` read the
declarations from `root.extensions["explorer"]`; nothing reads `root.entity_types`),
`databench:src/databench/entity_edit.py` (the same), `databench:src/databench/module_manifest.py`
(drops `filesystem.py -> entities.py`), `databench:tests/test_root_loader.py` (added), and the
conftest fixture, which builds a `Host` for the test's enabled set and supplies its snapshot to
any test that builds a root. Checks: with explorer enabled, a root with a malformed entity
declaration gives the same configuration error code and message as at `8280919` through the
browse route, the comments route, the CLI `read` operation, a direct `markup_refresh` and
`markup_accept` call given explorer's snapshot, and the markup phase of `databench generate`
(revert: register no loader -> no error -> red); two apps built in one process, one with
explorer and one without, each keep their own loaders for a project root resolved lazily after
both exist (revert: a module-level loader list -> red); a lazily resolved project root carries
the loader's error and data; a core configuration error still wins over an entity error; with
explorer disabled the same root serves comments and `entities.py` is never imported; a grep
test finds no `entity_types` attribute read outside `entities.py`'s own data structures; the lint
passes with the entry removed.

**M1-U7 core workspace scope and generation phases.** Decision: which repositories `generate`
covers is core, because file summaries (core) and index pages (html) both run over it; each
module contributes a generation phase, which `databench generate` runs from M1 on and M7's service
runs later. No fingerprinted file changes, so core gets its own implementation of the selection
html's files also hold; the parity test of M1-U8 keeps the two equal until the section 6 batch
replaces html's copy with imports of core's. Owns `databench:src/databench/workspace_scope.py`
(added, core):

```python
@dataclass(frozen=True) class Scope: mode: str; base: Path        # "repository" | "dev-root"
@dataclass(frozen=True) class Skipped: name: str; reason: str; explicit: bool
@dataclass(frozen=True) class Selection:
    targets: tuple[tuple[str, Path], ...]   # one checkout per origin, in order
    names: tuple[str, ...] | None           # the dev root's configured list, or None
    skipped: tuple[Skipped, ...]            # refused names and duplicate copies
def resolve_scope(directory: Path) -> Scope         # the four rules of generate_cli's docstring
def is_primary_checkout(directory: Path) -> bool
def origin_identity(checkout: Path) -> str | None   # the normalized origin URL
def select(scope: Scope) -> tuple[Selection, str | None]
    # dev root: primary checkouts one level deep minus dev_roots exclusions, one per origin;
    # the configured list is `index_pages.repositories` (key kept; read through
    # filesystem.index_pages_runner_settings and, for the read-back check, through
    # dev_roots.parse_yaml_subset as the survey reads it); writes the config when absent and
    # returns its text
```

and `databench:src/databench/generate_cli.py` (config gate; each enabled phase's `check`;
scope; selection; core's `file-summaries` phase; then each enabled module's phases in canonical
order; exit status the largest, as at `8280919`; `--skip PHASE` repeatable, with `--no-markup-refresh`
and `--no-index-pages` kept as aliases; skipping a phase that is not registered prints a note),
`databench:src/databench/modules_registry.py` (`GenerationPhase`, `PhaseResult`, typed
`Host.generation`), `databench:src/databench/module_manifest.py` (`workspace_scope.py` in
`CORE`; the three `generate_cli.py` entries dropped), `databench:tests/test_workspace_scope.py`
(added), `databench:tests/test_generate_cli.py` (only a `module("html")` marker is added).
`generate_cli` keeps `NO_ORIGIN` (html's phase imports it from core) and
`ensure_dev_root_config` (delegating to `workspace_scope`). Checks: `tests/test_generate_cli.py`
passes with only that marker added, all modules enabled (repository mode, dev-root
mode, the written config, refused names, duplicate copies, no-origin skips, exit statuses;
revert: drop the configured-list rule -> red); with html disabled,
`databench generate` on a dev root runs file summaries over the same targets and loads no html
file; `generate_cli.py` imports no `index_pages*` file (lint).

**M1-U8 html generation phase** (developed beside U7 against its frozen interface; lands in
the same commit). Owns `databench:src/databench/html_module.py` (registers
`IndexPagesPhase(id="index-pages")`: `check` requires both or neither of the judgment and
generation lists, as `generate_cli` does at `8280919`; `run` applies the origin split
(`index_pages_cli.split_refused`, `index_pages_worktree.has_origin`), builds the argv
(`repository_index_argv`, moved from `generate_cli.py`) and calls `index_pages_cli.main(argv,
models=...)`), `databench:tests/test_html_generation_phase.py` and
`databench:tests/test_workspace_scope_parity.py` (both `module("html")`; the parity test gives
fixture dev roots with duplicate origins, a linked worktree, an excluded name, a configured list
naming a missing and an origin-less repository, plus a dev root with no config, and asserts
that `workspace_scope.select` and `index_pages_cli.select_configured` or `select_targets`
followed by `split_duplicates` agree on the ordered target list (names and paths), on every
skipped entry's name, reason text and `explicit` flag, on the error raised for an invalid
configured list, and that the config text core writes equals what `generate_cli` wrote at
`8280919` for the same root; revert: drop core's one-copy-per-origin rule -> red).

**M1-U9 close-out and release.** Owns `databench:tests/module_isolation.py` (`explained`
admits nothing; the isolation test is strict), the manifest's Python allowlist (must be empty),
`databench:README.md` ("Modules", "Generate" with phases and `--skip`; the boundary rule at
README:25-27 admits first-party modules through the registry), `databench:CLAUDE.md` (directory
map). Verify: full `uv run pytest`; each module's tests with the others disabled
(`--databench-modules`); `cd web && npm test && npm run typecheck`; `web/tests/browser-check.mjs`
with all modules enabled and with explorer disabled; the fingerprint verifier against `902d6ef`;
databench push.

### M2 Tasks module, read-only

**The export contract.** task-kit exports task facts, not manager views. The export lives in
task-kit's domain package, outside `task_system/dashboard/`, and is reached by the `task-kit`
command without importing dashboard code. Manager-derived values (sections, needs, hand-off
command strings, item UI states, counts, filters) are computed by databench's tasks module.

```
task-kit export protocols --format json
    no discovery; kind "protocols", payload {"protocols": [1]}
task-kit export tasks --format json --protocol N [--parent DIR]...
task-kit export item --format json --protocol N --checkout ID --task PATH --item ID
stdout: exactly one JSON document and a newline, for success and for every handled error;
        nothing else is ever written to stdout
stderr: human diagnostics only; a consumer never parses it
success: {"protocol": N, "task_kit_version": "x.y.z", "kind": "<kind>", "payload": {...}}
error:   {"protocol": N, "task_kit_version": "x.y.z", "error": {"code": "<code>", "message": "<text>"}}
exit codes: 0 ok; 1 internal; 2 usage; 3 not found; 4 protocol unsupported
error codes: usage | unknown_checkout | unknown_task | unknown_item | protocol_unsupported |
             discovery_failed | internal
negotiation: the consumer calls `export protocols` first and then passes the highest protocol
             both support; an unsupported --protocol exits 4 with the supported list in the message
compatibility within a protocol: fields are only added; enums are open (a consumer maps an
             unknown value to "other"); a removed, renamed, retyped or newly required field
             takes the next protocol number, and the old one stays served for one task-kit minor
```

Payload of `tasks` (protocol 1; every field present; `?` marks a nullable value):

| Field | Type |
|---|---|
| `generated_at` | string, ISO 8601 UTC |
| `revision` | string; changes whenever any field below changes |
| `repositories[]` | `{id: string, name: string, checkouts: [string]}`; one per origin, checkouts merged |
| `checkouts[]` | `{id: string, root: string (absolute), repository: string}` |
| `tasks[].checkout`, `.path`, `.stub`, `.location` | string; `path` is project-relative (`dev/tasks/<stub>`) |
| `tasks[].revision` | string |
| `tasks[].title?`, `.priority?`, `.description?`, `.summary?` | string |
| `tasks[].summary_source?` | `"task.yaml"` (M5 adds `"answer"`) |
| `tasks[].classification` | open enum: active, blocked, deferred, closed, archived, orphaned, remote, invalid (`validate.py`) |
| `tasks[].blocked_by` | [string] |
| `tasks[].last_activity?` | string |
| `tasks[].updates[]` | `{date: string, time?: string, detail: string}`, newest first, at most 20 |
| `tasks[].updates_total` | integer |
| `tasks[].has_items_block` | boolean |
| `tasks[].items[]` | `{id, title: string, state: open enum available, in-flight, blocked-user, deferred (types.py:43), priority?: string, note?: string}` |
| `tasks[].findings` | `{errors, warnings, item_errors, notes: [string]}` |
| `tasks[].sessions[]` | `{host, session_id, registered_at: string, state: open enum running, not running, unverified (sessions.py:54-56), local: boolean, recorded_at?: string, task_state?: string}` |
| `errors[]` | `{code: string, message: string, root?: string}`: discovery problems that did not stop the export |

Payload of `item`: `{checkout, path, item: <the tasks[].items[] record>, plan_section?: string
(the item's plan.md section text, at most 8000 characters), plan_section_truncated: boolean}`.

**M2-U0 contract freeze** (plugins-kit). Owns
`plugins/task-kit/skills/task/references/task-export-protocol.md` (the text above, normative),
`plugins/task-kit/skills/task/references/task-export.schema.json` (JSON Schema for each kind),
`tests/task-kit/fixtures/task_export/` (one fixture tree of two checkouts of one origin; golden
`tasks`, `item` and error documents for protocol 1).

**M2-U1 task-kit export** (plugins-kit; after U0). Owns `task_system/export.py` and
`task_system/export_cli.py` (added), `task_system/repo_identity.py` (moved from
`dashboard/repo_identity.py`, which re-imports it so the dashboard keeps working),
`plugins/task-kit/bin/task_kit_entrypoint.py` (dispatches `export` before any dashboard import),
`tests/task-kit/test_task_export.py`, `tests/task-kit/test_task_export_contract.py`. The export
reads through the domain modules (`discovery`, `task_items`, `validate`, `sessions`) and imports
nothing under `dashboard/`. Measured for sizing [E]: the dashboard inventory takes 0.65 s cold for
14 projects and 65 tasks. Checks: every golden document validates against the schema and matches
byte for byte (revert: emit `dashboard_payload` -> red); the schema test fails when a protocol-1
field is removed or retyped against the frozen schema (revert: drop `updates_total` -> red);
stdout holds exactly one document for each error code (revert: print a warning to stdout -> red);
`export` imports no `task_system.dashboard` module (sys.modules check; revert: import inventory
-> red); unknown checkout, task and item give distinct codes; the export writes nothing (tree and
data-dir digests before and after).

**M2-U2 tasks module backend** (databench; after U0 and M1, beside U1 with fake executables).
Owns `databench:src/databench/tasks_module.py` and `databench:src/databench/tasks_host.py`,
`databench:src/databench/module_manifest.py` (`MODULES["tasks"]` lists both),
`databench:src/databench/modules_registry.py` (appends `ModuleSpec("tasks", "Tasks",
"databench.tasks_module:register")`; and, because a module added after a machine wrote
`modules.json` must default to on as an absent file does, the file becomes `{"schema": 2,
"disabled": [...]}`: schema 1 is read as disabled = {explorer, html} minus its `enabled` list
and rewritten as schema 2 on the next save; checks: a schema-1 file listing only `explorer`
yields explorer and tasks enabled, html disabled; revert: keep reading `enabled` -> tasks off ->
red), `databench:src/databench/path_sources.py` (a `task-identity` source:
task-kit resolves the identity against its own discovery; databench walks no client path), the
route registry, the tests and helpers that write or assert `modules.json`
(`databench:tests/module_isolation.py`, `databench:tests/test_modules_registry.py`,
`databench:tests/test_explorer_module.py`, `databench:tests/test_module_isolation.py`,
`databench:tests/test_html_module.py`: helpers write schema 2; the schema-1 migration tests stay
strict, using the fixed legacy set {explorer, html}), `databench:.claude/bootstrap.json` (adds `{"ref": "plugins-kit:task-kit",
"min_version": "<the release that ships M2-U1>"}` beside llm-kit),
`databench:tests/fixtures/fake_task_kit/` (current, pre-export, garbage-emitting and hanging
fakes), `databench:tests/test_tasks_module.py` (marked `module("tasks")`).

```python
TASK_EXPORT_PROTOCOLS = (1,)
class TaskKitUnavailable(Exception): code: str
    # task_kit_absent | task_kit_too_old | task_kit_unsupported | task_kit_failed | task_kit_timeout
def resolve_task_kit() -> str
    # $DATABENCH_TASK_KIT (tests), $CLAUDE_BOOTSTRAP_DATA_ROOT or ~/.claude/plugins/data:
    # plugins-kit/task-kit/bin/task-kit[.cmd]; then PATH
class TaskKitClient:
    def __init__(self, executable=None, *, runner=subprocess.run, min_interval_s=2.0,
                 probe_timeout_s=10.0, timeout_s=30.0)
    def negotiate(self) -> int                       # once per process, cached
    def tasks(self, parents: Sequence[Path], *, refresh: bool = False) -> Mapping  # cached
    def item(self, checkout: str, task: str, item: str) -> Mapping
def register(host: Host) -> None
GET /api/v1/tasks?project=&q=&refresh=1
    200 {"available": true, "task_kit_version": ..., "protocol": 1, "view": <tasks-module view model>}
    200 {"available": false, "reason": {"code": ..., "message": ...}}
GET /api/v1/task-item?checkout=&task=&item=        path source: task-identity
databench --view tasks                             (no operation named `tasks`: $DEVROOT/tasks exists)
```

How `negotiate` classifies `task-kit export protocols --format json` [E: task-kit 0.13.0 answers
this argv with exit 2, empty stdout and argparse's "invalid choice: 'export'" on stderr]:

| Outcome | Code |
|---|---|
| executable not found | `task_kit_absent` |
| exit 2 and empty stdout | `task_kit_too_old` (predates `export`; message names `claude plugin update task-kit@plugins-kit`) |
| one valid document whose `protocols` share none with databench's | `task_kit_unsupported` (names both lists and which side to update) |
| no answer within `probe_timeout_s` (process killed) | `task_kit_timeout` |
| anything else (non-zero exit with a document, stdout that is not exactly one JSON document, a schema mismatch) | `task_kit_failed` (keeps the error code or the last stderr line) |

A current task-kit never produces "exit 2 with empty stdout" for this fixed argv, because every
handled outcome writes a document; that is what separates too old from failed. The view model
derives sections, needs, hand-off commands and item UI states from the export; `parents` are
databench's expanded development roots. Checks: each fake maps to its code (revert: one generic
code -> red); the cache holds within the interval; the module's tests pass with explorer and html
disabled.

**M2-U3 tasks views** (databench web; after U2's routes). Owns `databench:web/src/modules/tasks/`
(list, detail and tests). List: prototype A's layout with the project dropdown (approved
2026-10-17). Detail: concept C (ruled): Orient (legacy `summary` labelled legacy until M5, items,
working sessions), Decide (needs, blocked-user items), Trace (task-record updates). Styling is
databench's shell (ruling); the round-5 mockup refines it when it lands. No spend, usage or
regeneration control (ruling); no Queue, Dismiss or authoring control (q3). Checks (vitest):
fixture renders each section and lens; the project filter narrows; each unavailable code renders
its reason; an unverified session is tinted.

**M2-U4 browser release gate** (databench; after U3). Owns `databench:web/tests/browser-tasks.mjs`
and its fixture server (the fake task-kit through `DATABENCH_TASK_KIT`), desktop and the Pixel 7
profile `browser-check.mjs` uses. Cases: (1) tasks enabled with explorer and html disabled: the
list renders, the project filter works, a detail opens with its three lenses; (2) tasks disabled:
no Tasks entry, `/api/v1/tasks` answers 404, and a deep link shows "module disabled"; (3) a fresh
page load of a deep-linked detail URL renders that task; (4) task-kit absent, too old, failed and
timed out: each shows its reason and no stale data. Revert check: register the tasks view
regardless of the enabled list -> case 2 red.

**M2-U5 docs and release.** Owns `databench:README.md` ("Tasks module"), `databench:CLAUDE.md`,
task-kit `references/dashboard.md` (points at the export), task-kit `plugin.json` and
`pyproject.toml` (next free minor: the version M2-U2 declares),
`docs/planning/task-kit-dashboard/architecture.md` (decision 1: manager code lives in databench).
Order: task-kit is published first (`publish.py --only task-kit`, pre-authorized; claudx smoke of
`task-kit export protocols` and `export tasks`), then databench is pushed with the raised
`min_version`. Verify: unit tests; M2-U4; full databench suite; the fingerprint verifier;
git-code-review over both ranges; on this Mac, `databench --view tasks` shows the task counts
task-kit's dashboard shows.

### M3 task-kit dashboard cutover (after M2 has shipped)

Ruling: "i agree task-kit should remain entirely functional as it is today without the dashboard
running. It can provide necessary interface functionality so that databench can interface iwth
it." task-kit keeps and owns the export databench calls (M2); its own dashboard retires. Queue,
run, cancel, retry, authoring forms, endpoint helpers and sub-agent characterization are not
ported (q3; M8).

**M3-U1 retire the dashboard** (plugins-kit). Owns:
- `plugins/task-kit/skills/task/scripts/task_system/dashboard/` (deleted: server, static UI,
  launcher, scheduler, runner and runtime, jobs edge, translation, authoring, helpers,
  characterization, acceptance), `plugins/task-kit/skills/task/scripts/task_hold.py` (deleted),
  and the `tests/task-kit/` files that exercise only those modules (deleted; the unit lists them).
- `tests/task-kit/test_task_kit_package.py`, which mixes kept and retired contracts (`:63-105`):
  the `dashboard` capability test keeps asserting that step 1 runs `bin/task-kit`; the llm-kit
  install-edge test stays; the handshake test is replaced by one asserting that `work`'s first
  step is the work verb and that no capability or reference names `task_hold.py`, and it no
  longer reads `references/dashboard.md` for "## The /task work handshake" or "## Launcher exit
  codes".
- `plugins/task-kit/bin/task_kit_entrypoint.py`, `bin/task-kit`, `bin/task-kit.cmd`:
  `task-kit export ...` unchanged; bare `task-kit` runs `databench --view tasks` when databench
  is on PATH, else exits 1 saying databench provides the task view and how a machine gets it;
  `task-kit stop` stays for one task-kit minor so a server an earlier version started can be
  stopped (identity-checked through the legacy `server.json`, refusing while jobs run unless
  `--force`, as it does at 0.13.0), then goes.
- `plugins/task-kit/skills/task/SKILL.md`: `/task dashboard` runs bare `task-kit`; the `/task work`
  handshake step (`task_hold.py`) is removed, because no scheduler remains to yield; the `queue`
  capability keeps only its background-worker branch; `references/dashboard.md` becomes a short
  pointer to databench and `task-export-protocol.md`.
- `plugins/task-kit/README.md`, `.claude-plugin/plugin.json` (description without a dashboard
  that runs items), `pyproject.toml` (next free minor), `bootstrap.json` comment (psutil stays for
  `procstamp.py` and `harness.py`; openai stays for M5's transport answerers).
- Data: `<task-kit data dir>/dashboard/` (runtime database, run directories, `server.json`;
  2.1 MB on this Mac [E]) is left in place; nothing is deleted.

Checks: the existing tests of every `task.py` verb pass without edits (this unit may not edit
them; reverting any verb change turns them red); no file under `plugins/task-kit/` imports
`task_system.dashboard` (revert: keep `task_hold.py` -> red); bare `task-kit` with a fake
databench on PATH runs `databench --view tasks`, and without one exits 1 with the message;
the M2 export golden tests still pass; `task-kit stop` against a fixture `server.json` naming a
dead pid reports it stale and signals nothing. Verify: serial `tests/task-kit` run; claudx smoke
(`/task work` registers and pushes, `/task hand-off` releases, `/task dashboard` opens databench);
git-code-review; `publish.py --only task-kit` (pre-authorized).

### M4 Web module split

**Landed (databench `5171fcb`):** M4-U1 moved explorer and html web files into
`web/src/modules/{explorer,html}/`. The web allowlist could not be emptied: six edges remain, all
from `web/src/main.tsx` (1904 lines), whose `AppBody`, `ViewerPane`, `DirectoryTree` and listing
sort are the explorer view, whose `MODULE_VIEWS` names `TasksView`, and which imports html's
`IndexPagesViewer`, `indexPageFileKind` and `rawFileUrl` (`main.tsx:92,561,639-650,1106,1571`).
Five test files import helpers from `main.tsx`, and `api-path-builders.test.ts` enumerates its
builders.

**Decisions.**
- **Registration by web entry, reached through one lazy glob.** Each module has
  `web/src/modules/<id>/web.tsx` whose default export is a `ModuleWeb`. The shell names no module
  file: `module-web.ts` holds the one call
  `import.meta.glob<ModuleWeb>("./modules/*/web.tsx", { import: "default" })` (lazy; Vite requires
  literal arguments), and loads
  the entries of the modules `GET /api/v1/modules` reports enabled, and only those, so a disabled
  module's code is never downloaded (the browser counterpart of "never imported").
- **The boundary test sees the glob.** It accepts exactly one `import.meta.glob` in core, in
  `module-web.ts`, with exactly those literal arguments; any other glob, or any `import()` of a
  module path, in a core file fails. The allowlist must be empty.
- **Module-to-module needs go through shell contributions.** html's page viewer reaches explorer's
  file view as a `fileViewers` contribution that explorer reads with `useFileViewers()`, so
  explorer never imports html. `rawFileUrl` is core (the raw route is core).
- **What stays in the shell (core):** `App` and boot (session, modules), `ModuleNav`, the core
  pages, `projects.tsx`, the comments components (`comments-view.tsx`,
  `comments-dashboard.tsx`, `comments.ts`; ruling 2), `markup.ts`, `code-viewer.tsx`,
  `markdown-viewer.tsx`, `ui.ts`, `system-viewer.ts`, `session.ts`, plus three added files:
  `routes.ts` (the client URL scheme `/<slug>/...`, `/-/<view>`, and the core API URLs: raw, root
  metadata, the write header), `context-types.ts` (the server's context payload types, which
  explorer and html both read) and `module-web.ts`. The toolbar (`action-bar.tsx`) stays
  explorer's: only explorer imports it; it moves to core when a second module needs it.
- **Explorer's boundary:** the project routes (`/` and `/<slug>/...`, `AppBody` at `5171fcb`), the viewer
  pane and its actions, the directory tree, listing sort and listing helpers, and the explorer
  API URLs (browse, tree, refresh). Explorer composes the core comments panel and comments
  dashboard into its views; with explorer disabled, comments stay reachable through the CLI and
  API only (no comments page without explorer is built in M4).
- **Tests are outside the boundary rule** (they exercise shell and views together), so the five
  test files repoint their imports; `listing-sort.test.tsx` moves beside the listing code.

**Interface (fixed before U2-U4 start):**

```ts
// web/src/module-web.ts (core)
// the only glob, written literally (Vite does not accept variables as glob arguments):
// const entries = import.meta.glob<ModuleWeb>("./modules/*/web.tsx", { import: "default" });
export type ModuleViewProps = { nav: ComponentChildren };
export type ProjectRoutesProps = { modules: Modules };
export type FileViewerProps = { context: FileContext; rawPrefix: string;
  onNavigate: (path: string) => void };            // IndexPagesViewer relays page links
export type FileViewerEntry = {
  id: string;
  applies: (context: FileContext) => boolean;   // e.g. html: a marked index page not opted out
  Component: (props: FileViewerProps) => JSX.Element;
};
export type ModuleWeb = {
  module: string;                                // equals the directory and the server module id
  views?: Record<string, (props: ModuleViewProps) => JSX.Element>;  // keys = announced view ids
  projectRoutes?: (props: ProjectRoutesProps) => JSX.Element;       // at most one enabled module
  fileViewers?: FileViewerEntry[];
};
export function loadModuleWeb(enabled: readonly string[],
  entries?: Record<string, () => Promise<ModuleWeb>>): Promise<ReadonlyMap<string, ModuleWeb>>;
  // calls only the enabled ids' loaders; a `module` mismatch or two projectRoutes owners throw
export const FileViewersContext: Context<FileViewerEntry[]>;
export function useFileViewers(): FileViewerEntry[];
// web/src/modules.ts: shellPlan(pathname, modules, loaded) -- "view-unavailable" when an
// announced view has no component in the loaded entries; project routes go to the loaded
// projectRoutes owner, else "module-disabled"
```

**M4-U2 shell contract and loader** (core). Owns `databench:web/src/module-web.ts`,
`databench:web/src/routes.ts`, `databench:web/src/context-types.ts` (all added),
`databench:web/src/main.tsx` (reduced to the shell), `databench:web/src/modules.ts`,
`databench:web/src/module-boundaries.test.ts` (glob rule; empty allowlist),
`databench:web/src/module-web.test.ts` (added), and the import repoints in
`databench:web/src/main.test.tsx`, `comments-routing.test.tsx`, `session.test.tsx` and
`api-path-builders.test.ts` (its builder table names `routes.ts` and the explorer files).
Checks: re-adding `import { TasksView } from "./modules/tasks/tasks-view"` to `main.tsx` fails the
boundary test; changing the glob literal, or adding a second glob, fails it; `loadModuleWeb(["tasks"])`
calls only the tasks loader (revert: load all, then filter -> red); a mismatched `module` throws;
two loaded entries that both declare `projectRoutes` are rejected (revert: last one wins -> red).
The test proves which loaders are called; that a disabled chunk is never fetched is M4-U5's
browser gate.

**M4-U3 explorer view extraction** (developed beside U2 against the frozen interface; lands in the
same commit). Owns `databench:web/src/modules/explorer/web.tsx` (`{module: "explorer",
projectRoutes: ExplorerView}`), `explorer-view.tsx` (from `AppBody`), `viewer-pane.tsx`
(`ViewerPane`, `DownloadAction`, `SystemViewerAction`, the system-open and future buttons; it
asks `useFileViewers()` before its own viewers, and the Raw toggle and mode rules at
`main.tsx:1106,1571` read "a contributed viewer applies" instead of `indexPageFileKind`),
`directory-tree.tsx`, `listing.ts` (sort state, `sortEntries`, `ListingSortHeader`,
`listingDisplayName`, `withMissingChildren`, size and date formats), `explorer-api.ts` (browse,
tree, refresh URLs), and `databench:web/src/modules/explorer/listing-sort.test.tsx` (moved).
Checks: `main.test.tsx`, `comments-routing.test.tsx`, `session.test.tsx` and the listing-sort test
pass with only import changes; no explorer file imports `modules/html/` (boundary test).

**M4-U4 html and tasks entries** (same commit). Owns `databench:web/src/modules/html/web.tsx`
(`{module: "html", fileViewers: [indexPagesViewer]}`; `applies` = `indexPageFileKind(name) !==
null && viewer.index_pages !== false`), `databench:web/src/modules/html/index-pages.tsx` (takes
`rawFileUrl` from `routes.ts`), `databench:web/src/modules/html/index-pages.test.tsx` (renders
explorer's `ViewerPane` inside a `FileViewersContext` holding html's entry), and
`databench:web/src/modules/tasks/web.tsx` (`{module: "tasks", views: {tasks: TasksView}}`).
Checks: the index-pages tests pass with only that wrapper; with no html entry loaded, a marked page
opens in explorer's ordinary HTML viewer (the sandboxed iframe at `main.tsx:648`) with Raw
available, because the identifying context keys are absent (revert: keep a direct html import in
`viewer-pane.tsx` -> boundary red).

**M4-U5 gates and docs** (after U2-U4). Owns `databench:web/vite.config.ts` (enables
`build.manifest`), `databench:web/tests/browser-check.mjs` additions,
`databench:web/tests/browser-modules.mjs` (added), `databench:README.md` and
`databench:CLAUDE.md` (web map). Browser gates, desktop and Pixel 7: all modules enabled
(`browser-check.mjs` unchanged otherwise); explorer disabled (`/` redirects to `/-/tasks`, a project
URL shows "Module disabled", no explorer chunk is requested); html disabled (a marked page shows
in the ordinary HTML viewer, a sandboxed iframe, with Raw available; no html chunk is requested); tasks disabled
(`browser-tasks.mjs` case 2 and no tasks chunk). Build check: `npm run build` emits one chunk per
`modules/*/web.tsx` (read from the Vite manifest). Verify also: `npm test`, typecheck, the Python
suite, the fingerprint verifier (unchanged: no Python file moves), databench push.

### M5 Task answers

`summary-on-commit-plan.md` units U1, U2, U4, U5, U7 and U10 keep their scope, interfaces and
checks with these changes:

- **U2** also owns `task_system/export.py`, the export schema and its golden fixtures:
  `tasks[]` gains `answers` (`{qid: {state, answer, answerer, as_of, fingerprint}}`) and
  `coverage`, and `summary_source` gains `"answer"`; additive, so protocol stays 1. (The
  dashboard's `snapshot.py` is gone after M3.)
- **U3 (re-cut)** has no switch in `task.local.yaml` and no spend statement or checkbox. Phases
  are `allow` sets: phase 1 `{"free"}`, phase 2 the caller's consent. By hand, the consent is
  plugins-kit's (`llm_kit.registry.consent.load_consent()`, per q1); a host passes its own with
  `--allow CLASSES`, which wins. `answerer.tier` is the llm-kit cost class.
- **U4** adds `task-kit answers refresh --format json --protocol N [--parent DIR]... [--allow
  CLASSES] [--batch N] [--stop-file PATH] [--deadline-s N]` (kind `answers-pass`) and `task-kit
  answers status --format json --protocol N`, under the export's stdout, stderr, exit-code and
  error-code rules (M2). A stop file ends the pass between phases.
- **U6** (service) becomes M7-U6a (project-scoped answers), M7-U6 (the `task-answers` phase) and M7-U7 (the loop); **U8** (dashboard UI) becomes M5-U8.
- **M5-U8 answers in the tasks module** (databench web; after U2 is published). Owns the lens
  components in `databench:web/src/modules/tasks/`: Orient shows the orientation answer, Decide
  the decisions with their stated defaults, Trace the task-record commits and activity; each
  answer shows answerer, as-of commit and a stale marker. Read-only.
- **U9** drops the Windows service gate (M7) and every `task-kit service` text.

### M6 Shared file summaries

**Landed (databench `2a300be..b526cab`; status BUILT in `databench:docs/markup-design.md`, "V4
amendment"):** summaries of a root whose `content_markup:` block is present live in
`.databench/markup-labels.jsonl`; `_cache_root` and `_file_lock` keep their meaning, so no
fingerprinted file changed. D11 there: databench never stages, commits or pushes the file. The
amendment's "Prerequisite for M7's multi-machine default" (an overlay plus a publisher in a private
worktree) is M7-U0, U4 and U5.

### M7 Generation service (core)

Re-planned 2026-10-22 against what shipped (databench `b526cab`, task-kit 0.17.0, katsoft-llm-kit
1.0.0) and revised after an independent cross-check of the first re-plan. Nothing of M7 is built.
The user's words that bind it are in section 1 and are not restated here: "databench ships with
usage on spend off", "It all is by default" (regeneration types), task-kit "entirely functional as
it is today without the dashboard running", and no spend control in the tasks view ("we have
databench to configure spend"). Section 8 holds the questions and says which unit each one gates.

**What shipped that changes the plan.**
- Databench starts as a detached background server (`cli.py` `_start_detached`, `--stop`,
  `--status`, `--restart`, log at `server_log_path(port)`; the server names its pid in
  `/api/v1/instance`). The earlier `databench service ensure|stop|status|run` family, psutil and a
  second identity protocol are dropped: the server is the service, and M7 adds an `ensure`
  operation, the loop inside the server, and settings.
- Databench runs from its own venv on the released packages (`6baf09b`, `3975519`, `d6ccdfd`):
  `pyproject.toml` pins `katsoft-llm-kit[sdk,jobs,config]==1.0.0` and `katsoft-contracts==1.0.0`;
  `llm_gateway.resolve_llm_kit` is the `llm-kit` console script beside `sys.executable`;
  `.claude/bootstrap.json` keeps only the task-kit plugin ref. No llm-kit floor is raised.
- M6 landed (`2a300be..b526cab`): `.databench/markup-labels.jsonl` for roots whose
  `content_markup:` block is present. D11 of `databench:docs/markup-design.md`: databench never
  stages, commits or pushes it. A `content_markup:` block opts a root into tracked storage, not
  into unattended pushing, and index pages pushing their output is not a ruling for a label
  publisher. Q3 (section 8) therefore sets only the shipped default of the `publish.labels`
  setting: the publisher is built either way and pushes nothing while that setting is off.
- task-kit 0.17.0 serves `task-kit answers refresh|status|prune`; the pass commits and publishes
  its own answer files, so tasks need no publisher. Its only scoping option is `--parent`, a
  directory whose children are projects, which is wider than a `generate` selection (U6a).

**Shape.** Core gains `generation.py` (settings), `service_cli.py` (`generation`, `ensure` and
`labels` operations), `label_publisher.py` and `background.py`; the tasks module gains a generation
phase. Every phase covers the `Scope` and `targets` it is given (`GenerationPhase.run`;
`workspace_scope`). The loop runs each selected phase of each enabled module as a child process of
the server, `<this interpreter> -m databench.cli generate --repository R ... --skip <every other phase>` over
the cycle's deduplicated repositories (U7; the explicit multi-repository scope below), so
a phase is killable, its output goes to the server log, and `generate`'s own checks, scope selection
and consent filter are the only code path, by hand or by service. A regeneration type id is a phase
id (`file-summaries`, `index-pages`, `task-answers`). Fingerprint rule (section 6): no added file is
named `index_pages*.py`, and no unit edits a fingerprinted file; each databench unit's verification
runs the verifier.

**Preconditions on shared files.** The databench working tree carries uncommitted edits to `cli.py`,
`server.py` and `roots.py` (a concurrent session: the background-start tests are untracked there).
Every unit that edits `cli.py` or `server.py` (U1, U3a, U3b, U4, U7, U8) starts from that session's
committed state, never from a working tree it shares.

**Units** (owns / depends / gate). Waves: W1 U0, U1, U6a; W2 U3a, U4, U6, U8; W3 U2, U3b, U5; W4
U7; W5 U9; W6 U10. Units in one wave share no file. Files with more than one owner run in the order
of the waves: `generate_cli.py` U4, U3b, then U7; `service_cli.py` U1, U8, U5; `server.py` U1, U3a, U7;
`cli.py` U1, U4, U3b, U8; `generation.py` U1 then U5 or U10 (one default); `filesystem.py` U5 only; `workspace_scope.py`, `html_module.py` and `modules_registry.py` U7.

| Unit | Owns | Depends | Gate |
|---|---|---|---|
| U0 publisher design | `databench:docs/markup-design.md` ("V5 amendment", landed in `8cbd486`) | none | none |
| U1 settings store and CLI | `generation.py`, `service_cli.py`, settings routes in `server.py`, one dispatch line in `cli.py` | none | none |
| U6a task-kit scoped answers | `plugins/task-kit` (`answers_cli.py`, discovery, protocol doc, README, tests), task-kit 0.18.0 | none | none |
| U3a consent gateway | `llm_gateway.py`, the providers route in `server.py` | U1 | none |
| U4 label overlay, end to end | `markup.py`, `generate_cli.py` (`--label-destination`), the `markup-refresh`, `markup-accept` and `markup-adopt` operations in `cli.py`, `databench:docs/markup-design.md` (D27), README | U0 (landed) | none |
| U6 task-answers phase | `tasks_host.py`, `tasks_module.py`, `.claude/bootstrap.json` (`min_version` 0.18.0) | U1, U6a published | none |
| U8 ensure | `service_cli.py` (`ensure`), the detached-start helper of `cli.py` | U1 | none |
| U2 settings panel | `web/src/settings.tsx` (+ tests), route in `main.tsx`, `routes.ts` | U1, U3a | none |
| U3b consent call sites | `generate_cli.py` (filter), `generate_config.py`, the `markup-refresh` filter in `cli.py` | U3a, U4 | Q2 and Q4 answered |
| U5 label publisher | `label_publisher.py`, the `labels publish` operation in `service_cli.py`, `filesystem.py` (one public alias, only if the test needs it), the `publish.labels` default in `generation.py` | U0, U4, U8 (file order) | none; built for either Q3 answer |
| U7 background loop | `background.py`, `workspace_scope.py` (cycle selection, the multi-repository scope), `generate_cli.py` (`--repository`), `html_module.py` (the index-pages phase for that scope), `modules_registry.py` (the contract's docstring only), startup and shutdown wiring and the on-open gate in `server.py`, status route, "Last runs" block in `settings.tsx` | U1, U2, U3b, U4, U5, U6 | none |
| U9 hook and wording | `plugins/task-kit/hooks/`, task-kit 0.19.0, README, claude-settings `USER-PREFERENCES.md` | U8 | none |
| U10 release | none, except `generation.py` and its defaults test for the one `PUBLISH_LABELS_DEFAULT` line when Q3 is answered after U5 has run (verification and publication otherwise) | all | none |

Before the user answers anything, every unit but U3b (and the U7 and U10 that follow it) can
proceed; U3b waits for Q2 and Q4. Q3 gates no unit: it sets the shipped default of `publish.labels` (a constant in
`generation.py`, off in U1; a yes flips it in U5 when the answer exists by the time U5 starts, otherwise
in U10, which owns only that line and its defaults test for that case, in one commit). Option chosen over making U7's dependency on
U5 conditional, because a conditional dependency leaves U7 with a branch that a "no" never
exercises, while a publisher that is built and off by default has one path to test for both
answers. Q5 (the answers budget) does not gate: U6 ships the stated defaults.

Every check below names the revert that must turn it red.

**M7-U0 publisher design (landed: databench `8cbd486`).** The "V5 amendment: the label overlay and
the label publisher" in `databench:docs/markup-design.md`, decisions D14 to D26 and the interfaces
the units build against. It supersedes D11 for service runs only: hand runs keep D11, the service
writes the overlay (D14 to D17), and one component, the publisher, may commit and push under four
gates (D18 to D23): `publish.labels` on, a tracked-mode root, an `origin` with an attached HEAD, and
a checkout on origin's default branch. Its private worktree and the copied helpers follow D24 and
D25. U4 and U5 build to it; this plan restates none of its rules beyond the interfaces below.

**M7-U1 settings store and CLI.** `generation.py` owns `<config root>/databench/generation.json`
`{"schema": 1, "regeneration": {"enabled": bool, "disabled_types": [phase ids]}, "consent":
{"usage": bool, "spend": bool}, "publish": {"labels": bool}, "updated_at": ...}`; it records the
DESELECTED types, as `modules.json` schema 2 records the disabled modules, so a phase added later is
on ("It all is by default"). An absent file yields regeneration on, no disabled type, usage on,
spend off, and `publish.labels` off (`PUBLISH_LABELS_DEFAULT`, which the Q3 ruling may flip). Strict: unknown
keys, wrong types and a different schema are loud errors naming the file. Interface: `load() ->
GenerationSettings`, `save(settings)`, `consent_classes(settings) -> frozenset` (always contains
`free`), `selected_phases(settings, registered_ids) -> tuple`, and `stop_file_path()` (read from the
environment variable the loop sets, else None). `GET` and `PUT /api/v1/generation-settings` (write-
intent header; the PUT is local-only under `remote_access`; the GET lists `types: [{id, title,
module}]` from the host's registered phases). `databench generation show|set [--regeneration
on|off] [--type ID on|off]... [--usage on|off] [--spend on|off] [--publish-labels on|off]` in
`service_cli.py`. Checks: an absent file yields the shipped defaults (revert `DEFAULTS` to usage
off: red); a remote PUT is refused (revert the local-only decoration: red); a PUT without the header
is refused (revert `write=True`: red); a deselected type of a disabled module survives a save; a
type added after a save is selected.

**M7-U6a task-kit scoped answers.** `task-kit answers refresh|status|prune` gain `--project DIR`
(absolute, an existing directory, repeatable): discovery is restricted to those project directories
(their parent is the discovery parent), so a pass touches only the task folders those projects reach
and then groups them by the git repository that holds them, which for a project with a mounted
`dev/tasks` is the tasks repository, not the project checkout. Selecting by task repository (a
`--repo TOP`) is rejected: a `generate` target is a project checkout, and the repository holding its
task folders is a different one. `--project` combined with `--parent` is their intersection. The
verb `task-kit answers protocols --format json` (kind `answers-protocols`, payload `{protocols:
[1], options: [...]}`) lets a client tell a task-kit with scoped answers from one without, because
`export protocols` cannot (it answers on 0.15 and 0.16, which have no answers, and on 0.17.0, which
has answers without `--project`). The protocol document's "Answers commands" section, the README and
the tests change with it; task-kit 0.18.0, published with `publish.py --only task-kit` (the task
folder pre-approves it). Checks over a fixture tree of three projects under one parent: `--project A`
answers and commits only A's folders (revert: ignore the option: B's folders change, red); two
selected projects sharing one tasks repository produce one repository report; `status` and `prune`
are scoped the same; an absent or non-absolute path is a usage error (exit 2); `answers protocols`
lists `project` in `options`; every existing answers and export test passes unchanged. The unit
settles the hypothesis that first-checkout ownership of a shared folder (`export._owners`) still
assigns it when only some of its checkouts are selected.

**M7-U3a consent gateway.** `llm_gateway.filter_models(ids, allowed) -> Filtered(usable,
switched_off)` runs one `llm-kit models describe <ids> --caller process --allow <classes> --json`
per operation pass through the console script beside `sys.executable`. Both shapes of llm-kit 1.0.0
are read: a usable target answers on stdout with `rendered_entries` and `switched_off`; no usable
target writes `{"error": {"dispositions": [...]}}` to stderr with stdout empty and exit 1, and a
disposition of `"switched-off"` is a consent outcome while any other disposition (an unknown id) is
a loud error. `GET /api/v1/generation-providers` classifies the ids in `generate.yaml` by cost
class for U2. llm-kit is not changed and databench never reads llm-kit's consent file (q1). Checks,
with a fake `llm-kit` following the 1.0.0 shapes: the partition keeps declared order; the all-off
stderr shape yields an empty `usable` (revert: treat nonzero as failure: red); an unknown id raises
(revert: treat every disposition as switched off: red); the argv carries `--allow free,usage` for
the shipped defaults (revert: omit the flag: red); a source guard fails if `src/databench` imports
`llm_kit.registry.consent` (revert: add the import: red). One smoke test runs the real console
script on ids it declares and asserts the partition shape only, because a harness endpoint's class
comes from the machine's auth probe (on the machine that ran the probe, a `luna` entry reported
"codex on subscription auth" as usage).

**M7-U4 label overlay, end to end.** One unit owns the whole path so no half exists, to V5's
interfaces: `markup.py` gains `LABEL_DESTINATION_TRACKED_FILE` and `LABEL_DESTINATION_OVERLAY`,
`MarkupStore.label_overlay_path` (never `overlay_path`, which keeps its meaning as the read-only
tracked file shown over a cache-mode root's labels), `label_overlay_path(root_path, cache_dir=None)`,
`read_label_overlay`, `pick_label` (extracted from `parse_labels`, behavior-preserving, and the one
function the loader's duplicate rule, the read view and the fold use), `fold_labels`,
`retire_label_overlay` and `store_for(root, cache_dir=None, *, tracked=None,
destination="tracked-file")`. Accepts and run state of a tracked-mode root write the overlay under
destination `overlay`, the D10 cache import retargets to the overlay under that destination (D17),
and the combined view shows the winner per path (D16). `generate_cli.py` gains
`--label-destination {tracked-file,overlay}` (default `tracked-file`, so a hand run keeps D11),
which `FileSummaries` passes to `markup.enqueue_root` and `markup_refresh`; the `markup-refresh` and
`markup-accept` operations in `cli.py` carry the same option, so the accept contract a run emits
names its destination. U4 also owns the hand fold: `databench [DIR] markup-adopt` folds the overlay
into the working-tree label file with `fold_labels`, writes it atomically under `labels.lock`,
retires the adopted entries from the overlay, and never stages, commits or pushes (D11). It sits in
U4, not U5, because it needs only the store primitives above and no git process, and because it must
work with `publish.labels` off and a Q3 "no", where U5's publisher returns SKIPPED; the unit adds
the command to `databench:docs/markup-design.md` as D27 and to the README. Checks: a child process
`python -m databench.cli <fixture repo> generate --label-destination overlay --runner <fake
llm-kit>` leaves the tracked file's bytes and mtime unchanged and `git status --porcelain` empty,
and the overlay holds the labels (revert: stop forwarding the option in `FileSummaries`: the tracked
file is written, red); the option on a root with no `content_markup:` block writes its cache and no
overlay (revert: honor the destination everywhere: red); the D10 import under `overlay` writes the
overlay and not the tracked file (revert: import into the tracked file: red); the read view returns
the overlay label when its `generated_at` is later and the tracked one when earlier (revert:
tracked always wins: red); `mutate` under one destination never writes the other file (revert:
write both: red); `pick_label` is the function `parse_labels` uses for duplicates, shown by a direct
comparison test with a tie-break case (revert: a local copy with a different tie-break: red);
`overlay_path` keeps its meaning, shown by the existing cache-mode suites passing unmodified
(revert: reuse the name for the new file: they go red); `markup-adopt` leaves the working-tree file
holding the fold, `git status --porcelain` naming only the label file, `git diff --cached` empty and
no new commit (revert: add a stage or commit: red), drains the adopted overlay entries, and a second
run writes nothing (bytes and mtime); a conflicted tracked file is refused with exit 2 (revert:
fold into it: red); the existing `tests/test_markup*.py` and `tests/test_generate_cli.py` pass
unmodified; the fingerprint verifier reports unchanged (`_cache_root` and `_file_lock` keep their
meaning, D13).

**M7-U6 task-answers phase.** `tasks_host.py` gains `answers_capability()` (runs `task-kit answers
protocols --format json`; exit 2 with empty stdout, or a payload without `project` in `options`,
maps to `task_kit_too_old` naming `claude plugin update task-kit@plugins-kit`; this is separate from
export negotiation, which cannot tell the versions apart) and `answers_refresh(projects, allow,
stop_file, deadline_s, batch)` running `task-kit answers refresh --format json --protocol N` with one
`--project` per selected target, a client timeout of the deadline plus 120 s, and a payload check of
`kind: answers-pass`. `tasks_module.py` registers the `task-answers` phase. `run(scope, targets,
models)` consumes `targets`: one invocation with every selected target as `--project` (task-kit
groups them and answers each task repository once; with no target nothing runs). `--allow` is U1's
`consent_classes`, `--stop-file` is `stop_file_path()` when the loop set it, `--batch 4` (task-kit's
default, stated so a task-kit change cannot raise it) and `--deadline-s 1800` are constants of this
module. `check` returns None; the probe runs in `run`, so a task-kit that is absent or too old gives
the phase status 1 with a recap line and never aborts the other phases. A repository `refused`
because llm-kit is absent or too old is status 1 with the report's message; per-task failures are
recap counts. `.claude/bootstrap.json` raises the task-kit `min_version` to 0.18.0. Checks with a
fake `task-kit`: the argv carries one `--project` per target and no `--parent` (revert: pass
`development_parents()`: a sibling project appears, red); a dev root run with two targets makes one
invocation; `--allow free,usage` under the defaults and `--batch 4 --deadline-s 1800` (revert: omit
`--allow`, which would fall back to llm-kit's consent: red); a task-kit that answers export but not
`answers protocols` yields `task_kit_too_old` with zero refresh calls (revert: skip the probe: the
fake records a refresh, red); a stop file ends the fake mid-pass and the phase reports `stopped`
(revert: do not pass `--stop-file`: red); with the tasks module disabled the phase is not registered
and `generate --skip task-answers` notes it; the module-isolation and import-lint tests stay green.

**M7-U8 ensure.** `databench ensure` in `service_cli.py`: exit 0 and print nothing when a Databench
server answers on the port or regeneration is off; otherwise start the detached server with no
browser, from a working directory that is not a project (the first development root), and exit 0
once it answers. `ensure` holds a short-lived start lock (`<user cache>/databench/service/ensure.lock`,
an OS file lock) around probe and start, so a second caller waits, probes, finds the server and exits
0. The detached-start helper of `cli.py` accepts any answering Databench (`instance = _probe_instance`
without comparing pids), so this unit makes it return `(child pid, reported instance)` and check the
instance's `pid` equals the child's: a different pid with the child still alive is an error naming
both, and a child that exits on a taken port while another Databench answers is reported as "already
running", not as a start. It never opens a browser, never takes a port from another program (a
foreign listener is exit 1 with a line), and, if the helper cannot be imported without the CLI parser,
the unit first moves it to a core file. Checks with a fake `Popen` and a scripted probe: two
concurrent `ensure` calls both exit 0, exactly one child is launched and holds the lock, and the pid
`ensure` reports equals that child's (revert: drop the lock: two children, the loser exits non-zero,
red); a foreign Databench answering while the child has not bound is not credited to the child
(revert: remove the pid comparison: red); regeneration off starts nothing (revert: skip the settings
read: red); the command opens no browser.

**M7-U2 settings panel.** A core shell page (`web/src/settings.tsx`, reached from the header): the
regeneration toggle, the type drop-down (every registered phase, all selected), the usage and spend
checkboxes, each listing the model ids and providers it would let generation use (from U3a's
providers route), and the label-publishing checkbox. It writes through U1's PUT. Checks: vitest for
each control and for an unselected type round trip (revert the payload mapping: red); the browser
gate of M2-U4 (desktop and Pixel 7) over a fixture server; the tasks view still has no spend or
regeneration control (the existing module test).

**M7-U3b consent call sites.** `generate_cli.main` filters the `models` dict once, from
`generation.consent_classes`, before any phase runs, so the html module's phases receive filtered
lists and no fingerprinted file changes; the `markup-refresh` operation filters the endpoint list it
is given (the path the server's on-open refresh uses). A list that filters to empty skips its
operation with a status line naming the class to enable, exit 0, not 2. `generate_config.py` drops
the user-facing `harnesses:` switch (v2): a harness endpoint is runnable when `kit_text_only_adapters`
lists its adapter and its cost class is consented; a v1 file's `harnesses` keys are read, ignored,
and noted. Checks, with the fake kit of
U3a: a spend-only list under spend off never starts the runner (the fake records zero calls; revert
the filter in `main`: red); the `markup-refresh` operation filters too (revert: red); an all-off
list exits 0 with the status line (revert: exit 2: red); a v1 file with `harnesses: {claude: true}`
loads with a note.

**M7-U5 label publisher.** `label_publisher.py` to V5's interface: `publish(root_path, *,
settings=None) -> PublishResult(outcome, code, message, branch, base_sha, commit, added, updated,
retired, attempts)` (gates D18, fold D19, commit D20, push D21, outcomes D23, worktree D24), and
`databench labels publish [DIR]`, which prints the D23 line and exits 0 for PUBLISHED, NOTHING and
SKIPPED, 1 for REFUSED and NOT_PUSHED, 2 for a usage error. It runs only when `publish.labels` is on
(off unless the Q3 ruling flipped its default), and only from a checkout on origin's default branch.
The unit reads the tip's `content_markup:` block through `filesystem._load_configuration` pointed
at the worktree; the premise that it accepts a worktree directory as it stands is settled by a test,
and if it needs a public name U5 owns `filesystem.py` (not fingerprinted) for that one alias,
`load_root_configuration(directory)`, while the alternative, calling the private name from `label_publisher.py` (core to core, so no
import-lint edge), is taken instead when the test passes without an alias. Checks over a
bare-origin fixture, the V5 list: a checkout on a feature branch pushes nothing and reports
`not-on-base` (revert: push to the checkout's upstream: red); a block present in the checkout and
absent at the tip reports `opt-in-not-on-origin` (revert: read the checkout's config: red); a tip
label file with conflict markers is REFUSED `tip-file-invalid` and the overlay is unchanged (revert:
skip the parse error: red); an ignored label path is REFUSED (revert: skip the check: red); the
pushed commit changes exactly the label file (revert: `git add -A`: red); the checkout's working
tree, index and a dirty tracked label file are byte-identical before and after (revert: fold into
the checkout: red); the `push` argv has none of the force spellings (revert: add `--force`: red); a
competing commit for a neighbouring path pushed between fetch and push makes the second attempt
land a commit holding both (revert: rebase instead of re-fold: a conflict, red); an entry older
than the tip's record retires with no commit (revert: always overwrite: red); an entry added to the
overlay during the publish survives retirement (revert: rewrite the overlay from the snapshot:
red); no worktree remains after success, after an injected push failure and after a killed run's
leftover (revert: remove the `finally`: red); a second publisher with the same overlay makes no
commit; `publish.labels` off runs no git process (the runner fake records zero calls; revert:
ignore the setting: red); a root with no origin is SKIPPED with a line.

**M7-U7 background loop.** `background.py` owns the loop thread. Every server starts the thread when
`generation.load().regeneration.enabled`; a machine-wide lease decides which server runs phases: the
thread tries a non-blocking OS lock on `<user cache>/databench/service/loop.lock` each minute, the
holder runs the cycle and records its pid and port in the state file, and the lock frees when the
holder exits, so another server takes over within a minute. This matters because the CLI allows
several ports. The on-open refresh follows the same switches: the `startup` handler
`launch_markup_refresh` in `create_app` returns without a launch when regeneration is off or
`file-summaries` is deselected (an explicit refresh request from the page stays allowed, being the
user's action), and the launched `markup-refresh` is consent-filtered by U3b. Each cycle first
selects once for the whole machine: every existing development root's repositories through
`workspace_scope` (its config read when present, never written: `select` gains `write_config=False`,
and a root with no config contributes every unique repository, as `generate` would after writing
one; a root that holds no primary checkout, which `workspace_scope` refuses with a `ScopeError`,
is detected first with `find_repositories` and contributes zero members, writes nothing and gets a
status line, while an invalid `.databench/config.yaml` or `dev_roots.yaml` still raises and fails
the cycle, recorded in "Last runs"), then `select_union` removes a repository seen through two roots (same resolved path) and a
second checkout of one origin, by the same one-copy-per-origin rule. Then, per selected phase
whose minimum interval has elapsed (file-summaries and task-answers 1 h, index-pages 24 h;
constants in this file), it runs one child `generate --repository R ...` over that deduplicated list
(`generate` gains a repeatable `--repository`: a scope of explicit primary checkouts that reads and
writes no dev-root config). That scope is a third `Scope` mode, `MODE_REPOSITORIES`, with a
`members` field (a tuple of name and path pairs; `base` is the first member's parent and no phase
reads it for this mode), built by `workspace_scope.repositories_scope(paths)` and answered by
`select` with exactly those members. A phase receives the complete list once in `targets`: the
tasks phase passes every member as `--project` in one invocation, file summaries builds one root per
member, and `IndexPagesPhase.run` (in `html_module.py`, which is not fingerprinted) handles the new
mode by running each member that has an origin through `repository_index_argv(member)`, the argv of
repository mode that already shares a parent dev root's settings and exclusions, one
`index_pages_cli.main` call per member and the largest status as the result; the dev-root branch
(one base plus `--repo` names) stays for dev-root scope, and no `index_pages_cli.py` edit is made.
The run uses `--label-destination overlay`, the stop-file variable in its
environment, and stdout and stderr appended to the server log; so each repository, and each task
repository behind the `task-answers` pass, runs once per phase per cycle however many roots reach
it. Then it runs `databench labels publish DIR` as a child process for each tracked-mode repository of
the cycle after the file-summaries phase, whether or not that phase was due (the command returns
SKIPPED `publish-off` without a git process while the setting is off, and an empty overlay costs
none), and records the largest exit code as the publisher's status: REFUSED and NOT_PUSHED are a
failed run in "Last runs", SKIPPED is a recap line. State (last start, end, status, recap, duration per
phase) is `<user cache>/databench/service/state.json`, served by `GET /api/v1/generation-status`,
shown as a "Last runs" block in the panel. Shutdown: the app's shutdown hook creates the stop file,
waits up to 30 s for the child, then terminates it; on Windows `--stop` uses `taskkill /T /F`, so the
child dies without the grace (the answers pass commits per question and the overlay is durable).
Checks: regeneration off starts no cycle and a server startup launches no `markup-refresh` even for
a root with an endpoint (revert the startup gate: the `Popen` fake is called, red); a deselected
`file-summaries` does the same; two servers on two ports with one lock path run exactly one cycle
between them, and the survivor takes over after the owner stops (revert: no lock: both run, red); a
deselected type is not run (revert: ignore `disabled_types`: red); a disabled module's phase is
never run; the interval holds a phase back (revert: ignore the clock: red); the stop file reaches a
fake long child and the shutdown waits for it; two overlapping development roots (one nested in the
other, and a second checkout of one origin) yield each repository once, the fake `generate` is
started once per due phase, and the fake task-kit records one invocation with each `--project`
once and each task repository reported once (revert: select per root: duplicates, red); the REAL index-pages phase
runs across two roots: a fixture of two development roots holding leaf repositories with local bare
origins (a leaf needs no model call, the clean-machine run of the databench notes), one repository
reachable through both roots, and a child `python -m databench.cli generate --repository ...`
leaves exactly one index-pages run record and one push per repository (revert: have `IndexPagesPhase`
treat the multi-repository scope as one dev root: it is handed one base with another root's names and
fails, red), with a second test recording the `index_pages_cli.main` argv per member; the existing
`tests/test_workspace_scope_parity.py` and `tests/test_html_module.py` pass unmodified and the
fingerprint verifier reports unchanged; one empty development root (no
primary checkout) plus one valid root select the valid root's repositories, create no file in the
empty root and run the phases (revert: let `ScopeError` propagate: the cycle aborts, red), while an
invalid config in the valid root fails the cycle with the file named (revert: swallow the error:
red); a root with
no config is selected and no config file appears (revert: use the writing `select`: red); a tracked-mode root's
cycle leaves its tracked file untouched (revert: drop `--label-destination overlay`: red); a failing
phase does not stop the next.

**M7-U9 hook and wording.** `plugins/task-kit/hooks/hooks.json` adds a `startup` matcher and a second
command, `hooks/sessionstart/databench-ensure.sh`, to `startup`, `resume` and `clear` (async; the
existing `session-start.sh` entries for `resume` and `clear` stay). The script exits 0 on every path:
`databench` absent from PATH is silent, a failing `databench ensure` appends one line to the
task-kit data directory log, as `session-start.sh` does, and any other `source` does nothing. The
opinion is a trigger only; databench's `regeneration.enabled` is the user's switch
(`docs/reference/plugin-opinion-razor.md`: a seam by a setting). task-kit 0.19.0 and README.
claude-settings `USER-PREFERENCES.md` insight `generated_index_pages` extends to file summaries and
task answers: regeneration follows the user's setting, and agents neither refresh by hand nor report
staleness. Checks (`tests/task-kit/test_databench_ensure_hook.py`, a fake `databench` on PATH
recording argv): `ensure` is called for `startup`, `resume` and `clear` (revert the `startup`
matcher: red); not called for another source; an absent `databench` exits 0 with no log line; a
failing one exits 0 and logs once (revert: propagate the status: red); the existing hook tests pass.

**M7-U10 release.** The databench Python suite, the web tests and typecheck, and the fingerprint
verifier against the M7 base; a Windows smoke over ssh (the detached server survives the starting
session, `ensure` is idempotent, `--stop` ends the child) or release with the gap stated;
`git-kit:git-code-review` on the task-kit and plugins-kit changes and the applicable Markdown lane
on the claude-settings edit; databench push; the claude-settings edit committed by path and pushed
to its default branch (standing authorization `ordinary_git_publication`); `publish.py --only
task-kit` for 0.19.0 (0.18.0 shipped with U6a), then `bootstrap run`. Exercise on this machine:
`databench ensure`, the panel, one `labels publish` against a throwaway origin (with `publish.labels` on), and
the first `task-answers` pass. Under the shipped defaults that pass answers at most 4 tasks per task
repository (every pending question of each) within 30 minutes, with usage-class models; the 95
pending questions drain over several hourly cycles, not in the first pass.

#### Premise outcomes (M7 re-plan, 2026-10-22)

**[E]** = read or run by this unit; **[H]** = hypothesis with the check that settles it.

| Premise | Outcome | Evidence or check |
|---|---|---|
| task-kit 0.17.0 has the answers commands, kind `answers-pass`, `--allow`, `--stop-file`, `--deadline-s`, and no dashboard | [E] | `plugin.json` version 0.17.0; `task-export-protocol.md` "Answers commands"; README "task-kit has no dashboard of its own" |
| The answers pass commits and publishes its own files | [E] | protocol text: "publish when something is pending or unpublished"; so tasks need no publisher |
| `answers` accepts `--parent` only; there is no per-project or per-repository scope, and no `answers protocols` verb | [E] | `answers_cli.py` parser (`--parent`, `--allow`, `--batch`, `--stop-file`, `--deadline-s`) and its `VERBS` |
| Discovery assigns a shared task folder to the first preferred checkout; a project with a mounted `dev/tasks` keeps its tasks in another repository | [E] | `export._owners` docstring; root CLAUDE.md "dev/tasks mounts a separate tasks repository" |
| A `--project` filter keeps the first-checkout owner assignment when only some checkouts are selected | [H] | settled by U6a's shared-repository check |
| The task phase must cover the `Scope` and `targets` it is given; `--parent` over every development root is wider than a selection | [E] | `modules_registry.py` `GenerationPhase` ("`run` covers the selected targets"); `workspace_scope.py` module docstring. The first re-plan's conclusion that the phase may ignore `targets` is refuted |
| Export negotiation cannot tell a task-kit with answers from one without | [E] | `tasks_host.negotiate` runs `export protocols` only; export arrived in 0.15 and answers in 0.17.0 |
| Databench reaches llm-kit as a pinned package, not through bootstrap | [E] | `pyproject.toml` pins; `.claude/bootstrap.json` lists only `plugins-kit:task-kit`; `llm_gateway.venv_llm_kit` |
| `models describe --allow` exists in the pinned llm-kit | [E] | ran `llm-kit models describe --help` in the databench venv (1.0.0): "--allow ALLOW ... free, usage, spend" |
| No usable target answers on stderr with stdout empty, exit 1; usable answers on stdout with `switched_off` | [E] | ran both shapes against `luna` and `qwen38-5090`; commit `3975519` message |
| A harness endpoint's cost class depends on the machine's auth probe | [E] | `luna` disposition detail: "codex on subscription auth" |
| Both harness adapters have a text-only mode in 1.0.0 | [E] | `kit_text_only_adapters()` returned `['claude-cli', 'codex-cli']` |
| Detached start, `--stop`, `--status`, `--restart`, a server log and a pid in `/api/v1/instance` exist | [E] | `cli.py` `_start_detached`, `_probe_instance`, `server_log_path`, `_stop_running_instance`; first committed in `500fced` (bundled with the label-store commit) |
| `_start_detached` credits any answering Databench to its own child | [E] | the wait loop assigns `instance = _probe_instance(args.port)` and never compares `instance["pid"]` with `child.pid` |
| The databench tree carries uncommitted `cli.py`, `server.py`, `roots.py` and test edits of a concurrent session | [E], blocking for the units that edit them | `git status` and `git diff --stat` in databench; `tests/test_cli_background.py` is untracked |
| The server's on-open refresh launches a detached `markup-refresh` with `DEVNULL` output for any root that declares an endpoint, with no reference to a regeneration setting | [E] | `server.py` `launch_refresh` and the `startup` handler (`launch_markup_refresh`); the earlier plan's `server.py:676-681` is stale |
| The `generate` parser has no `--label-destination`, so a child run cannot reach the overlay without U4's end-to-end path | [E] | `generate_cli.build_parser`: `--skip`, `--no-markup-refresh`, `--no-index-pages`, `--model`, `--runner` |
| The CLI allows several ports, so several servers can run on one machine | [E] | `cli.py` `--port` and `_probe_instance(port)` per port; U7's lease covers it |
| M6 landed; D11 says databench never commits the label file; a `content_markup:` block opts into tracked storage, not into pushing | [E] | `markup-design.md` "V4 amendment" status BUILT, D1, D11, "Prerequisite for M7's multi-machine default" |
| task-kit defaults to 4 tasks per repository per answers pass | [E] | protocol document, `--batch N` "(default 4, at least 1)"; the first re-plan's "95 questions in the first pass" was wrong |
| task-kit's `hooks.json` has `resume` and `clear` only, both running `session-start.sh` | [E] | file read; U9 adds `startup` and a second command |
| The `generated_index_pages` insight exists in claude-settings `USER-PREFERENCES.md` | [E] | file read (Insights block) |
| `generate` writes a dev root's `.databench/config.yaml` when absent, and selection dedupes only inside one scope | [E] | `generate_cli` docstring; `workspace_scope.select` calls `ensure_dev_root_config` and `_split_copies(base, ...)` per base; U7's cycle selection reads without writing and dedupes across roots |
| Two concurrent `databench` starts leave one server | [H], unproved | the loser exiting non-zero is an inference, and `_start_detached` can credit the wrong server; settled by U8's two-caller check |
| A hook's working directory is a poor default root for the server | [H], rationale not evidence | U8 starts from the first development root; its check reads `/api/v1/instance` `default_root` |
| Windows keeps a detached server alive after the starting shell exits | [H], Windows-only | `_start_detached` uses `DETACHED_PROCESS`; no evidence on this Mac; settled only by U10's smoke over ssh |
| Pushing label commits to an origin's base branch is accepted by the user's remotes | [H], Q3 sets only the default | per repository; also unknown: branch protection on each opted-in remote; U5 reports a rejected push and keeps the overlay |
| A `Scope` holds one mode and one base, and `IndexPagesPhase.run` treats every non-repository scope as one dev root (`index_pages_cli` over that single base plus target names) | [E] | `workspace_scope.Scope` (`mode`, `base`); `html_module.IndexPagesPhase.run`, so a cycle-wide list needs the explicit scope of U7 |
| Running each member through `repository_index_argv` covers what a dev-root run covers (shared settings, exclusions, one copy per origin) | [H] | settled by U7's real-phase test across two roots and the parity suite |
| `workspace_scope` raises on a development root with no primary checkout | [E] | `_checkouts_inside` raises `ScopeError` when `find_repositories(base)` is empty, and `resolve_scope` does the same; U7's cycle selection tests emptiness first |
| V5 (D14 to D26) specifies the overlay, the publisher's gates, fold, push rules and interfaces | [E] | databench `8cbd486`, `docs/markup-design.md` "V5 amendment" read in full; its hand-sharing question (the toggle versus a fold command) is answered here by `markup-adopt` in U4 |
| `MarkupStore.overlay_path` already means the read-only tracked file over a cache-mode root's labels, so the new file needs `label_overlay_path` | [E] | V5 "Vocabulary" and premises |
| `filesystem._load_configuration` accepts the private worktree directory without change | [H] | settled by U5's test; a public alias in `filesystem.py` only if it fails |
| Minimum intervals bound the cost: 1 h for file summaries and task answers, 24 h for index pages | [H], incomplete | the index-pages runner's skip key covers index pages only (section 6); file summaries and task answers have no equivalent evidence, so U7 records per-phase duration and U6 states its batch and deadline |

### M8 Running work on tasks (deferred stub)

Not planned (q3). Queue, run, cancel, retry, authoring and session dismiss in databench wait
until the user needs them; after M3 they exist nowhere. Starting points for that design: the
`adced200` revision of this file, section 5, stage 6, and task-kit's dashboard code as of the
last release before M3 (git history).

## 6. The index-pages generator fingerprint

**What it is [E].** `generator_fingerprint(settings)` (`databench:src/databench/index_pages_worktree.py:845-920`)
hashes the package-relative NAME and bytes of every `index_pages*.py`, `paths.py`,
`ignore_rules.py`, `path_walk.py`, every file under `index_pages_refs/` and `assets/`, five llm-kit
modules (`pipeline.execution.{scheduler,deadlines,failure_cache}`, `storage.atomic_json`,
`pipeline.runtime.gate`), and the run settings. It is a skip key, not a page key: when it
changes, the next run of each configured repository is a real one (fetch, worktree, scan; about 24
Git processes instead of 4) and is model-free when nothing is pending (databench README, "skip
key" and "The generator fingerprint"; `index_pages_worktree.py:904-911`). Pages are replanned only
when an eligibility key, a `source_sha` or the generation spec hash moves; the spec hash is keyed
by the five generation inputs' logical names and bytes (`index_pages.py:655-670`), not their
paths. 13 repositories are configured on this Mac (`$DEVROOT/.databench/config.yaml`,
`index_pages.repositories`). The `adced200` revision of this plan said such an edit "makes every
repository's index pages stale"; that overstated it.

**How the split handles it.**
1. Nothing fingerprinted moves or changes. html membership is declared in the manifest, so
   `index_pages*.py`, `index_pages_refs/` and `assets/` stay at their paths; `paths.py`,
   `ignore_rules.py` and `path_walk.py` stay as core files at theirs.
2. Every coupling is cut on the non-fingerprinted side: `filesystem.py`, `server.py`, `cli.py`,
   `generate_cli.py`, `generate_config.py`. `index_pages_cli -> markup` needs no edit because
   markup is core. Three leftovers stay until a batch: `filesystem.index_pages_runner_settings`
   (called from `index_pages_cli.py`), `index_pages_runner.resolve_llm_kit` (a copy beside
   core's), and html's repository selection (`index_pages_cli.select_targets`,
   `select_configured`, `split_duplicates`; `index_pages_survey.find_repositories`,
   `is_primary_checkout`, `normalize_remote_url`), duplicated by core's `workspace_scope` and
   held equal by M1-U8's parity test; the batch makes html import core's.
3. No added file may be named `index_pages*.py` or sit under `index_pages_refs/` or `assets/`;
   either would join the hash. The html registration file is `html_module.py`.
4. A checked-in verifier, owned by M1-U2 and run by every databench unit:
   `databench:scripts/verify_generator_fingerprint.py --base <commit> [--kit-root DIR]` with the
   exact settings fixture `databench:tests/fixtures/generator_fingerprint_settings.json` (every
   key `_fingerprint_settings` produces, with fixed values). It extracts the base commit's
   `src/databench` with `git archive` into a scratch directory (removed in a `finally`), runs
   `index_pages_worktree.generator_fingerprint` once for the base tree and once for the working
   tree in separate isolated-mode interpreter subprocesses (`-I`) whose `sys.path` puts the same llm-kit root first
   (`--kit-root`, default the `llm_kit` the databench venv links), and prints for each side the
   member list (stable name, sha256) and the digest. It exits 0 when the digests match, 1 with
   the differing members when they do not, and 2 when the two sides resolved different llm-kit
   bytes (the comparison is then refused). A pinned test
   `databench:tests/test_generator_fingerprint_members.py` asserts the member list equals the
   html manifest's hashed files plus `paths.py`, `ignore_rules.py`, `path_walk.py` and the five
   llm-kit modules, and that `html_module.py`, `module_manifest.py` and `modules_registry.py` are
   not members (revert: name the registration file `index_pages_module.py` -> red). A plugins-kit
   llm-kit release that edits one of the five hashed modules moves the digest too, so M5 and M7
   touch none of them.
5. If a later change must touch fingerprinted files (the two leftovers, or moving html files
   into a directory), it goes in ONE databench commit, so the cost is paid once: one model-free
   local run per configured repository. Editing the bytes of the five generation inputs is
   outside that batch, because it moves the spec hash and restamps records in every repository.

## 7. Changes to `summary-on-commit-plan.md`

- **D2.** Three llm-kit classes (free, usage, spend); `luna` and `haiku` are usage. Phases are
  `allow` sets. Consent is plugins-kit's when the pass runs by hand and databench's when
  databench runs it (q1). The spend checkbox and its statement leave task-kit (ruling: "there
  shouldn't be a spend control there").
- **D4.** The host is databench's generation service (M7). Deleted: `task-kit service ...`,
  `<data dir>/answers/service.{json,lock,stop,log}`, `hooks/sessionstart/service-ensure.sh`,
  `answers.service`, `answers.spend`, `answers.poll_s`. The pass algorithm stays task-kit code,
  run as `task-kit answers refresh --format json --allow ... --stop-file`. The row "inside
  the dashboard server: Rejected" reverses for databench's detached service.
- **D7.** "service off" reads "regeneration off"; "spend off" reads "class not consented".
- **Units.** U3 re-cut; U6 becomes M7-U6a, M7-U6 and M7-U7; U8 becomes M5-U8; U9 loses
  the Windows service gate.

## 8. Open questions (recommended default in brackets)

- **Q1. task-kit's dashboard data at the M3 cutover.** `<task-kit data dir>/dashboard/` holds
  the runtime database, run directories and `server.json` (2.1 MB on this Mac). Delete it?
  [No: leave it in place and name its path in the release notes; delete only on your word.]

- **Q2. Does the consent apply to a hand-run `databench generate` and `markup-refresh`?** Gates
  M7-U3b. The service and a hand run take the same path, so one rule holds. [Yes: the same filter,
  and a refused operation prints which class to enable in the settings; a second rule for hand runs
  would need a second switch.]
- **Q3. May databench push label commits to an opted-in repository's base branch unattended?** Gates
  no unit. A `content_markup:` block opts a root into tracked storage, D11 says databench never
  pushes, and index pages pushing their output is not a ruling for this. The publisher pushes only from a checkout that is on origin's default branch, not to the current
  branch's upstream as index pages does; a checkout on another branch keeps its overlay and prints a
  line. The answer sets only the shipped default of the `publish.labels` checkbox; the publisher is
  built either way. [Yes, default on: origin's default branch, fast-forward only, one commit
  touching only the label file; a rejected push keeps the overlay and shows in "Last runs". On a
  no, labels stay in the overlay, visible on this machine, and `databench markup-adopt` folds them
  into the working-tree file for the owner's own commit, as in M6.]
- **Q4. Does consent replace the `harnesses:` switch in `generate.yaml`?** Gates M7-U3b. With
  llm-kit 1.0.0 both harness adapters have a text-only mode, and the usage class already gates them.
  [Yes: drop the switch (v2 file); a v1 file is read with a note.]
- **Q5. How much task-answer work may the service start?** Does not gate; M7-U6 ships the defaults.
  Each hourly cycle answers at most 4 tasks per task repository (every pending question of each),
  stops after 30 minutes, and uses only the consented classes. [Those defaults; the 95 pending
  questions drain over several cycles.]

## 9. Decisions this plan makes, and what it dropped

Decided on the user's behalf:
- html membership by manifest rather than a directory move (section 6).
- File summaries, comments, raw read, store, system open and the workspace files are core;
  browse, tree, raw write and entities are explorer.
- databench applies its consent by pre-filtering through `llm-kit models describe --allow`, so
  llm-kit does not change; the pinned katsoft-llm-kit 1.0.0 carries `--allow`, so databench's
  llm-kit floor does not move.
- Regeneration and consent share one machine-written `generation.json`; `generate.yaml` stays
  hand-edited model lists.
- All three modules are enabled by default; the Python lint allowlist is emptied in M1 and
  the web one in M4.
- The export is task-kit domain code (`task_system/export.py`), reached by `task-kit export`
  without importing dashboard code; databench derives every manager view from it.
- Too old versus failed is decided by a fixed probe argv (section 5, M2-U2).
- M3 keeps `task-kit stop` for one task-kit minor so a server an older version started can be
  stopped.
- The tasks module reaches task-kit through its JSON CLI (process boundary, no `task_system`
  import), as in the previous revision.
- Entity declarations stay explorer's and load through a root loader (M1-U6); with explorer
  disabled a malformed declaration blocks nothing.
- The repositories `generate` covers are core (`workspace_scope`, M1-U7); each module adds a
  generation phase, which `databench generate` and M7's service both run.
- `modules.json` records the disabled set (schema 2, M2-U2), so a module added later is on.
- Every phase covers the `Scope` and `targets` it is given; the task phase passes one `--project` per
  selected target to a project-scoped task-kit answers pass (M7-U6a), not every development root.
- One machine-wide lease picks which server runs the loop, and the on-open refresh follows the
  regeneration and type settings (M7-U7).
- The loop runs each phase as a child `generate` process with a minimum interval per phase (file
  summaries and task answers 1 h, index pages 24 h); the index-pages runner's own skip key keeps an
  unchanged repository cheap (section 6).
- The server is the service: M7 reuses databench's detached start, `--stop` and `--status` and adds
  `ensure`; the earlier `service` verb family and psutil are dropped.
- The publisher pushes only from a checkout on origin's default branch (V5 D18), not to the current
  branch's upstream; a hand fold, `markup-adopt`, serves an owner who keeps `publish.labels` off.
- Regeneration settings record the deselected types, so a phase added later is on.
- Unattended file summaries of a tracked-mode root write a per-user overlay and a publisher folds
  it into the origin tip in a private worktree (M7-U0); hand runs keep D11.

Dropped from `adced200`: stage SC (llm-kit default flip and migration of task-kit, workflow-kit
and other plugins-kit callers; q1 scopes consent to databench); the remote-access question (q5);
stage S5's run host, engine supervision and UI parity (M8, deferred; task-kit's dashboard
retires in M3 without them).
