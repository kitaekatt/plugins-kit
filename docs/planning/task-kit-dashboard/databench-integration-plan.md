# databench integration plan: a modular databench with a tasks module (2026-10-22)

Status: plan only (units `revise-databench-plan-modules` and `fix-databench-plan-xcheck`,
2026-10-22; the second applies a codex/sol cross-check of `b7e92e26`). No code changed. This
revision replaces the stage plan committed as `adced200` (stages S2 to S6 and a consent-default
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
| core: host | `cli.py` (front door), `server.py` (app shell), `modules_registry.py` (added), `module_manifest.py` (added), `path_sources.py`, `generation.py`, `llm_gateway.py`, `service.py`, `background.py` (added in M1 or M7) | runs the process and loads enabled modules |
| core: workspace | `projects.py`, `dev_roots.py` (+ `dev_roots.yaml`), `roots.py`, `filesystem.py` (root resolution, exact spelling, reads), `paths.py`, `ignore_rules.py`, `path_walk.py`, `store.py`, `system_viewer.py`, `workspace_scope.py` (added in M1: which repositories `generate` covers) | every module resolves projects, paths and repositories the same way |
| core: access | `remote_access.py` | one access policy |
| core: comments | `comments.py`, `comment_locations.py` | ruling (2) |
| core: file summaries | `markup.py` (label store, work list, generation through `llm-kit jobs`), `generate_config.py`, `generate_cli.py` (the `generate` front door over core's and the modules' generation phases) | ruling (1); the html module's run state already shares markup's cache root and lock |
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
| 7 | M7 Generation service | databench, plugins-kit (task-kit hook), claude-settings | M1, M5, M6 | an always-on service; settings panel with regeneration (on by default, type drop-down, all types by default), usage (on by default) and spend (off by default); summaries, index pages and task answers stay fresh | (end of planned work) |
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
- **U6** (service) becomes M7's `task-answers` operation; **U8** (dashboard UI) becomes M5-U8.
- **M5-U8 answers in the tasks module** (databench web; after U2 is published). Owns the lens
  components in `databench:web/src/modules/tasks/`: Orient shows the orientation answer, Decide
  the decisions with their stated defaults, Trace the task-record commits and activity; each
  answer shows answerer, as-of commit and a stale marker. Read-only.
- **U9** drops the Windows service gate (M7) and every `task-kit service` text.

### M6 Shared file summaries

The databench task `shared-generated-content`, items `design-amendment` and `tracked-label-store`:
summaries move from `~/.cache/databench/markup/<key>/labels.json` into `.databench/`. Owns
`databench:src/databench/markup.py` (core) and `databench:docs/markup-design.md`. It keeps
`_cache_root` and `_file_lock` and their meaning, because the fingerprinted `index_pages_cli.py`
imports them. M7's file-summary operation runs by default on more than one machine only after
this lands, otherwise every machine pays for the same summaries.

### M7 Generation service (core)

**M7-U1 settings and consent.** Owns `databench:src/databench/generation.py` (added):
`<config root>/databench/generation.json` `{"schema": 1, "regeneration": {"enabled": bool,
"types": [operation ids]}, "consent": {"usage": bool, "spend": bool}, "updated_at": ...}`. Absent
file: regeneration on with every registered type (q6, "It all is by default"); usage on and
spend off ("databench ships with usage on spend off").
`GET` and `PUT /api/v1/generation-settings` (write header; local clients only). A core settings
panel holds the regeneration toggle, the type drop-down (operations of enabled modules) and the
usage and spend checkboxes with the providers they send text to. `generate.yaml` v2 drops
`harnesses`; a v1 file's harness keys are ignored with a note. Checks: absent file yields the
shipped defaults; a remote `PUT` is refused.

**M7-U2 consent applied through llm-kit.** Owns `databench:src/databench/llm_gateway.py`
(`filter_models(ids, allowed) -> Filtered(usable, switched_off)`, one `llm-kit models describe
<ids> --caller process --allow <classes> --json` per operation pass) and its call sites in
`generate_cli.py` and `markup.py`; `databench:.claude/bootstrap.json` raises llm-kit's
`min_version` to 0.4.0 (the first with `--allow`), with distinct absent and too-old messages.
The html operation receives already-filtered phase lists from `generate_cli`, so no fingerprinted
file changes. An operation whose list filters to empty is skipped with a status line, not exit 2.
llm-kit is not changed.

**M7-U3 service lifecycle.** Owns `databench:src/databench/service.py` (added), `cli.py`
(`databench service ensure|stop|status|run [--json]`; bare `databench` ensures the service and
opens the browser; `--dev` stays foreground), `databench:pyproject.toml` (psutil),
`databench:tests/test_service.py`. Identity, stop protocol and checks as in the previous
revision's S3-U1 (adapted from task-kit's `procspawn` and `procstamp` design).

**M7-U4 background loop.** Owns `databench:src/databench/background.py` (added) and the startup
wiring in `server.py` (markup launch stderr to the service log, replacing DEVNULL at
`server.py:676-681`). Its operations are M1's generation phases, and a regeneration type id is a
phase id. It runs each selected phase of each enabled module: file summaries
(core), index pages (html; per repository after its origin HEAD moves, at most once per 24 h),
task answers (tasks; `task-kit answers refresh --format json --protocol N --allow <databench
consent> --stop-file <service dir>/stop`). Checks: regeneration off runs nothing; a deselected type does
not run; a disabled module's operation is never registered.

**M7-U5 triggers and wording.** Owns `plugins/task-kit/hooks/hooks.json` and
`plugins/task-kit/hooks/sessionstart/databench-ensure.sh` (async `startup`, `resume`, `clear`;
runs `databench service ensure` when on PATH; absent or failing databench logs one line and exits
0), `tests/task-kit/test_databench_ensure_hook.py`; claude-settings `USER-PREFERENCES.md`
insight `generated_index_pages` (the regeneration switch and its types are the user's discretion;
agents do not regenerate by hand).

**M7-U6 release.** Windows smoke over ssh (detached service survives the starting session; stop
works) or ship with the gap stated; git-code-review; databench push; `publish.py --only task-kit`.

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
- **Units.** U3 re-cut; U6 becomes M7-U4's `task-answers` operation; U8 becomes M5-U8; U9 loses
  the Windows service gate.

## 8. Open questions (recommended default in brackets)

- **Q1. task-kit's dashboard data at the M3 cutover.** `<task-kit data dir>/dashboard/` holds
  the runtime database, run directories and `server.json` (2.1 MB on this Mac). Delete it?
  [No: leave it in place and name its path in the release notes; delete only on your word.]

## 9. Decisions this plan makes, and what it dropped

Decided on the user's behalf:
- html membership by manifest rather than a directory move (section 6).
- File summaries, comments, raw read, store, system open and the workspace files are core;
  browse, tree, raw write and entities are explorer.
- databench applies its consent by pre-filtering through `llm-kit models describe --allow`, so
  llm-kit does not change; databench's llm-kit floor becomes 0.4.0.
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
- Index pages in the background run per repository after its origin HEAD moves, at most once per
  24 h.

Dropped from `adced200`: stage SC (llm-kit default flip and migration of task-kit, workflow-kit
and other plugins-kit callers; q1 scopes consent to databench); the remote-access question (q5);
stage S5's run host, engine supervision and UI parity (M8, deferred; task-kit's dashboard
retires in M3 without them).
