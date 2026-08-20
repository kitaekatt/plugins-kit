# Library Consumption Modes

How a Python process reaches a bootstrap-published shared library (`shared_libs`
in `bootstrap.json`), across every kind of consumer -- not only another plugin.
`manifest-reference.md`'s `shared_libs` / `shared_lib_imports` section covers the
plugin-to-plugin case in full; this document is the map of every OTHER way a
process can import the same source, and states which of those ways are
supported.

Audience: a developer or agent deciding how a consumer -- a plugin, a script on
the bootstrap-managed standalone Python, or a project with its own interpreter
-- should reach a shared library.

## The four modes

| # | Consumer | Supported? | Update mechanism | Version story |
|---|----------|------------|-------------------|----------------|
| 1 | Plugin (`shared_lib_imports`) | Yes, canonical | Engine re-syncs on owner publish | Lockstep with the installed owner plugin; no pinning |
| 2 | Standalone Python (bootstrap-managed interpreter) | Yes | Same as mode 1 | Same as mode 1 |
| 3 | Foreign-interpreter project (engine-bundled Python, project venv) | Yes, consumer-assembled | Same as mode 1 | Same as mode 1, plus the caveats below |
| 4 | Off-fleet pip install (git URL, vendored copy) | No | None -- a human re-runs pip | A hand-looked-up commit SHA, not a version |

Every supported mode (1-3) ultimately points at the same on-disk source,
`~/.claude/plugins/data/<marketplace>/_shared_libs/<name>/<name>/`, and inherits
the same refresh trigger: an owner plugin publish re-syncs that one directory,
and every consumer's path entry -- a `.pth` file in modes 1-2, whatever the
project supplies in mode 3 -- keeps resolving without a rewrite. The modes
differ only in HOW a given interpreter is told to look there.

## Mode 1 -- Plugin consumer (`shared_lib_imports`)

The canonical, fully-supported case. A plugin declares
`"shared_lib_imports": ["<pkg>"]` in its own `bootstrap.json`; the engine writes
a `<pkg>.pth` into that plugin's own venv and verifies the import. Full field
reference, ordering, and the source-only guarantee (third-party dependencies
are the importing plugin's own concern):
[manifest-reference.md](manifest-reference.md#shared_libs--shared_lib_imports--cross-plugin-first-party-libraries).

**Update.** The engine re-syncs the shared, version-independent location on
every owner publish; the consumer's `.pth` never needs to change. **Version.**
Lockstep with whatever version of the owner plugin the marketplace has
installed. There is no pinning -- a consumer cannot ask for an older revision of
the library while staying on the marketplace's current owner-plugin version.

## Mode 2 -- Standalone-Python consumer

Any script running under the bootstrap-managed standalone Python can `import
<pkg>` directly, with no plugin venv and no `shared_lib_imports` declaration.
The engine registers the same `<pkg>.pth` in that interpreter's site-packages
the moment the owner publishes the library (`find_standalone_python` /
`purelib_of` in `bootstrap_lib/shared_lib.py`), so any process launched with
that interpreter sees the import resolve.

**Update and version.** Identical to mode 1 -- the `.pth` targets the same
stable `_shared_libs/<name>/` path, so an owner publish reaches this consumer
the same way it reaches a plugin's venv.

No manifest field expresses this mode: a standalone-Python script declares
nothing, and needs to declare nothing. The owner plugin's `shared_libs` entry
is what puts the `.pth` there.

## Mode 3 -- Foreign-interpreter project consumer

The shape: a project that cannot use either interpreter above, because it must
run under its own -- an engine-bundled Python (a game engine or application that
ships its own interpreter build), a project-local venv with its own dependency
set, or any other interpreter bootstrap does not manage.

What bootstrap supports here is the published location itself: `_shared_libs/`
is a stable, documented path (see `manifest-reference.md`'s `shared_libs`
section), so a project may read it. Bootstrap ships no machinery for this mode
-- no shim, no manifest field, no import verification. The consumer assembles
the recipe below itself, and owns it.

### The recipe

1. **The owner plugin is installed on the machine.** This mode reads the same
   published location every other mode reads; it does not work if the owning
   plugin has never run a bootstrap pass on that machine.
2. **A fail-soft `sitecustomize.py`** (or an equivalent `PYTHONPATH` shim) is
   added to the project's own interpreter. At import time it lists the
   immediate subdirectories of
   `~/.claude/plugins/data/<marketplace>/_shared_libs/` and inserts each one
   onto `sys.path`. "Fail-soft" is load-bearing: if the directory does not
   exist (owner plugin not installed on this machine, or a machine where
   bootstrap itself is absent), the shim must do nothing and let imports fail
   normally at the call site -- never raise from `sitecustomize.py` itself,
   which would break every Python invocation on that interpreter, not just the
   ones that need the shared library.
3. **The project mirrors the library's third-party dependencies** in its own
   requirements file. A shared lib shares first-party SOURCE only (see mode
   1's cross-reference); this project is not a bootstrap-provisioned venv, so
   nothing installs those dependencies for it automatically.

### Update

Automatic, and this is the point of the recipe: an owner publish re-syncs
`_shared_libs/<name>/` the same way it does for modes 1-2, and the project's own
runtime never has to invoke Claude Code to see the change -- a scheduled job or
a long-running process using the project's interpreter picks up fresh source on
its next import. Only the machine's session-start bootstrap pass has to run at
some point to perform that re-sync; the consuming process does not participate
in it.

### Caveats

State these plainly to anyone adopting this mode:

- **No pinning**, same as every other mode -- a project consumer resolves
  whatever version of the owner plugin is installed, with no mechanism to
  hold an older revision.
- **Dependency mirroring can drift.** The project's requirements file is a
  hand-maintained copy of the library's third-party dependency list. If the
  library adds a dependency and the project's copy is not updated in step, the
  import succeeds (first-party source resolves fine) but a call into a
  code path needing the new dependency fails with an ordinary
  `ModuleNotFoundError` -- indistinguishable, from the project's side, from any
  other missing package.
- **A machine where the owner plugin is not installed breaks the import
  outright.** The shim finds nothing under `_shared_libs/` and inserts nothing,
  so the subsequent `import <pkg>` in the project's own code raises
  `ModuleNotFoundError` with no mention of a plugin. The project's own code must
  catch that and emit a point-of-need message naming the plugin to install,
  the same discipline
  [action-triggered-install.md](action-triggered-install.md) describes for a
  skill's own preflight -- a foreign-interpreter consumer gets no help from
  bootstrap's own guarded-import machinery, because that machinery runs inside
  a plugin's own venv, not inside an arbitrary project interpreter.

## Mode 4 -- Off-fleet pip install

Not supported. Do not point a project's `requirements.txt` or `pyproject.toml`
at a shared library via a git URL, and do not vendor a standalone copy of one
into a project's own tree. Three concrete reasons, not a general aversion to
packaging:

- **No real version to pin.** A git URL pins a ref, not a version, and a
  marketplace that publishes no `{plugin}--v{version}` release tags gives you
  no ref that corresponds to a plugin version. "Install version X" then means
  "install this hand-looked-up commit SHA," which nobody else is asked to
  reproduce and which drifts the instant the owner plugin publishes again.
- **No update channel.** Every supported mode above refreshes itself when the
  owner plugin publishes. A pip install refreshes only when a human re-runs
  pip against a newly chosen ref -- there is no mechanism watching for a new
  publish and nothing analogous to the standalone-Python `.pth` re-sync.
- **The dependency closure is absent from `pyproject.toml`.** A shared
  library's edges to other first-party libraries are expressed as
  `shared_lib_imports` in its owner plugin's `bootstrap.json`, a bootstrap-only
  construct pip cannot read. A `pip install` of the library alone silently omits any other
  first-party package it imports, producing an `ImportError` pip's own
  dependency resolution gave no warning about.

If a genuinely detached consumer -- one that must function with no bootstrap
pass ever having run on its machine -- becomes a real requirement, that is a
deliberate release-process decision (its own package index entry, its own
version tags, its own dependency declarations) to be made on its own merits. It
is not something to improvise by pointing pip at a marketplace's source tree.

## Cross-cutting: version declaration is unsupported everywhere

Every mode above delivers whatever version of the owner plugin is installed, and
nothing else. There is no mode in which a consumer can ask bootstrap for "at
least version X" of a shared library the way `plugins[]` entries can ask for
`min_version` of a PLUGIN -- and even that plugin-level `min_version`
constraint is honored only for `install: "auto"` entries, not `"manual"` ones
(stated in the `install` bullet list of `manifest-reference.md`'s `plugins`
Entry Fields, and visible in the engine's plugin phase, where the `min_version`
branch sits on the auto path only). No comparable field exists for a shared
library at all.

A consumer that needs a minimum capability from a shared library should
**probe for that capability at runtime** -- check for the symbol, function, or
behavior it actually needs (`hasattr`, a version constant the library itself
exports and documents, a try/except around the specific call) -- rather than
assume any particular revision is present. Treat the library as always being
"whatever is currently published" and code defensively against the oldest
capability set you are willing to support, not against a version number you
cannot ask bootstrap to enforce.

## See also

- [manifest-reference.md](manifest-reference.md) -- the `shared_libs` /
  `shared_lib_imports` schema (mode 1) and the `plugins[]` `min_version` /
  `install` fields referenced above.
- [action-triggered-install.md](action-triggered-install.md) -- the
  point-of-need preflight-and-ask pattern a foreign-interpreter consumer (mode
  3) should imitate when the owner plugin is missing.
- The bootstrap plugin's `bootstrap_lib/shared_lib.py` -- the engine module that
  publishes a shared library and registers the standalone-Python `.pth` (mode
  2).
