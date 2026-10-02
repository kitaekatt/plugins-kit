# Library Consumption Modes

How a Python process reaches a bootstrap-published shared library (`shared_libs`
in `bootstrap.json`), across every kind of consumer -- not only another plugin.
`manifest-reference.md`'s `shared_libs` / `shared_lib_imports` section covers the
plugin-to-plugin case in full; this document is the map of every OTHER way a
process can import the same source, and states which of those ways are
supported.

Audience: a developer or agent deciding how a consumer -- a plugin, a script on
the bootstrap-managed standalone Python, a project venv that bootstrap
provisions, or a project with a foreign interpreter -- should reach a shared
library.

## The modes

| # | Consumer | Supported? | Update mechanism | Version story |
|---|----------|------------|-------------------|----------------|
| 1 | Plugin (`shared_lib_imports`) | Yes, canonical | Engine re-syncs on owner publish | Lockstep with the installed owner plugin; no pinning |
| 2 | Standalone Python (bootstrap-managed interpreter) | Yes | Same as mode 1 | Same as mode 1 |
| 3a | Project venv that bootstrap owns (`project_venv`, with `shared_lib_imports` inside it) | Yes, declared | Same as mode 1; the engine re-asserts the link every pass | Same as mode 1 |
| 3b | Foreign-interpreter project (engine-bundled Python, a venv bootstrap does not own) | Yes, consumer-assembled | Same as mode 1 | Same as mode 1, plus the caveats below |
| 4 | Off-fleet pip install (git URL, vendored copy) | No | None -- a human re-runs pip | A hand-looked-up commit SHA, not a version |

Every supported mode (1-3b) ultimately points at the same on-disk source,
`~/.claude/plugins/data/<marketplace>/_shared_libs/<name>/<name>/`, and inherits
the same refresh trigger: an owner plugin publish re-syncs that one directory,
and every consumer's path entry -- a `.pth` file in modes 1-3a, whatever the
project supplies in mode 3b -- keeps resolving without a rewrite. The modes
differ only in HOW a given interpreter is told to look there.

Each publish also lands an immutable copy of the same source at
`_shared_libs/<name>/.generations/<id>/<name>/` and writes `<id>` to
`_shared_libs/<name>/.current`. The `.pth` of modes 1-3a reads that pointer
when an interpreter starts, so a process keeps the version it started with
for as long as that generation is retained (7 days after it is superseded; see
mode 3b, "Update"); the next process gets the new one. A mode-3b shim gets the same
property only if it reads the pointer too (see mode 3b, "Update").

## Mode 1 -- Plugin consumer (`shared_lib_imports`)

The canonical, fully-supported case. A plugin declares
`"shared_lib_imports": ["<pkg>"]` in its own `bootstrap.json`; the engine writes
a `<pkg>.pth` into that plugin's own venv and verifies the import. Full field
reference, ordering, and the source-only guarantee (third-party dependencies
are the importing plugin's own concern):
[manifest-reference.md](manifest-reference.md#shared_libs--shared_lib_imports--cross-plugin-first-party-libraries).

**Update.** The engine re-syncs the shared, version-independent location on
every owner publish; the consumer's `.pth` never needs to change. A process
already running keeps the generation it started with (including submodules it
has not imported yet) for as long as that generation is retained (7 days after
it is superseded; see mode 3b, "Update"); a new process gets the new version. **Version.**
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

## Mode 3a -- Project venv that bootstrap owns

The shape: a project declares `project_venv` in a layered manifest
(`~/.claude/bootstrap.json` or `<project>/.claude/bootstrap.json`), so bootstrap
creates and syncs the project's own `.venv` from the project's `pyproject.toml`.
Because bootstrap owns that venv, it can wire shared libraries into it. The
project lists them under `project_venv.shared_lib_imports`:

```json
{
  "project_venv": {
    "shared_lib_imports": ["content_pipeline"]
  }
}
```

Each entry is a library name, or a `{"name": ..., "marketplace": ...}` object
that names the marketplace publishing it. The field's schema and its merge
across layers:
[manifest-reference.md](manifest-reference.md#project_venv--projects-own-python-environment).

**What the engine does.** After every owner plugin has published in the pass,
the engine finds the marketplace that publishes each declared library by
scanning `~/.claude/plugins/data/<marketplace>/_shared_libs/<name>/<name>/`. It
then writes `<name>.pth` into the project venv's site-packages (the same
executable `.pth` modes 1-2 use) and verifies that `import <name>` works under
the venv's own interpreter. The step runs only when the layered manifest declares
`project_venv`, the engine has a project directory, and the venv's target
directory holds a `pyproject.toml`; otherwise it reports one verbose-only
"skipped - no project venv to link into" entry and links nothing.

**Outcomes.** Every declared library produces exactly one outcome per pass.

| Outcome | Meaning | Reported as |
|---|---|---|
| `linked` | `.pth` written and the import verified | Folded into the pass's single aggregated shared-libs display line |
| `cached` | `.pth` already correct; nothing written | Verbose-only ok entry |
| `skipped` | No marketplace has published the library yet, or the interpreter or its site-packages could not be resolved | Ok entry, no failure; a later pass supplies the link |
| `failed` | `.pth` written but the import still fails, or the write failed | Action entry plus a `shared_lib` failure attributed to `config` |
| `ambiguous` | An unqualified entry, and two or more marketplaces publish that name | Routed like `failed`; the message names each marketplace. Qualify the entry as `{"name": ..., "marketplace": ...}` |

A failed link rolls back to the prior `.pth` (or removes the new one), so the
next pass attempts it again. A `skipped` library leaves nothing on `sys.path`,
and the project's own `import` then fails as an ordinary `ModuleNotFoundError`.

**A shared library outranks a same-named package in the venv.** The `.pth` line
ends in `sys.path.insert(0, _bsl_p)`, so it PREPENDS the library's current
generation to `sys.path`. If the project venv already holds a package with that
name -- for example one the project's own dependencies installed -- the shared
copy is the one that imports. There is no setting to change this order. To use
the venv's own copy instead, do not list that library in `shared_lib_imports`.
When a pass links or finds linked a library whose name the venv already holds
(a package or module in its site-packages, or a matching `.dist-info`), the engine
reports an action entry naming the shadowed copy (with its version when a
`.dist-info` gives one); the pass still
succeeds and the venv's own copy is left untouched.
If a `sitecustomize.py` exists in the venv, bootstrap reports it, because that
file plus the `.pth` would prepend the same generation twice.

**Malformed entries.** A `shared_lib_imports` value that is not a list, or an
entry that is not a name or a `{name, marketplace}` object, is reported as a
`project_venv` failure naming the entry. Valid entries in the same list are
still linked. The report alone does not withhold the `BOOTSTRAP_PROJECT_PYTHON`
export; a venv-level failure (a bad `subdir`, a failed sync or import check) does.

**Update.** Same as mode 1: an owner publish re-syncs the shared source, the
`.pth` names a stable path, and a process that starts afterward gets the new
generation. The engine re-checks the link on every pass that runs, so a venv
recreated outside bootstrap, or a link lost for any other reason, is restored by
the next pass; the per-project cooldown can delay that pass. **Version** is
lockstep with the installed owner plugin, with no pinning.

**Third-party dependencies are not shared.** Only first-party source is linked.
The project's own `pyproject.toml` must declare whatever the library imports
from outside the standard library, exactly as a plugin does in mode 1.

**The declaration is inert on an older engine.** `shared_lib_imports` sits in a
layered manifest, and an older bootstrap does not know the key, so it ignores it
and links nothing, with no message. `requires_bootstrap`
does not gate this: that field lives in a plugin's own `bootstrap.json`, not in a
layered manifest. A project that depends on the link should probe for the library
at import time and report the missing link at the call site.

## Mode 3b -- Foreign-interpreter project consumer

The shape: a project that cannot use any interpreter above, because it must run
under its own -- an engine-bundled Python (a game engine or application that
ships its own interpreter build), a venv that bootstrap does not own (no
`project_venv` declaration covers it), or any other interpreter bootstrap does
not manage.

What bootstrap supports here is the published location itself: `_shared_libs/`
is a stable, documented path (see `manifest-reference.md`'s `shared_libs`
section), so a project may read it. Bootstrap ships no machinery for this mode
-- no shim, no manifest field, no import verification. The consumer assembles
the recipe below itself, and owns it. A project whose venv bootstrap can own
should use mode 3a instead.

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
runtime never has to invoke Claude Code to see the change -- the next process
started on the project's interpreter gets the fresh source. Only the machine's
session-start bootstrap pass has to run at some point to perform that re-sync;
the consuming process does not participate in it.

A process that is already running does NOT pick the change up on its next
`import` statement: CPython caches every imported module in `sys.modules`, so
a module the process already imported stays the old one until the process
exits. What does change under it is the directory itself. With the shim above,
the process resolves `_shared_libs/<name>/<name>/`, which every publish
replaces in place, so a submodule the process imports for the FIRST time after
a publish comes from the new version while the modules it already holds are
the old one -- two versions of one package in one process.

To keep a long-running process on one version, have the shim insert the
current generation instead of the entry directory, falling back to the entry
directory when there is no usable pointer -- the same rule the mode 1-2 `.pth`
applies:

```python
# For each <entry> = ~/.claude/plugins/data/<marketplace>/_shared_libs/<name>
path = entry
try:
    gen = open(os.path.join(entry, ".current"), encoding="utf-8").read().strip()
    candidate = os.path.join(entry, ".generations", gen)
    if gen.isalnum() and os.path.isdir(os.path.join(candidate, name)):
        path = candidate
except OSError:
    pass
sys.path.insert(0, path)
```

A superseded generation is deleted 7 days after it stops being current, so a
process running longer than that can still fail to import a submodule it had
not imported yet.

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
  `shared_lib_imports` schema (mode 1), the `project_venv.shared_lib_imports`
  schema (mode 3a), and the `plugins[]` `min_version` /
  `install` fields referenced above.
- [action-triggered-install.md](action-triggered-install.md) -- the
  point-of-need preflight-and-ask pattern a foreign-interpreter consumer (mode
  3b) should imitate when the owner plugin is missing.
- The bootstrap plugin's `bootstrap_lib/shared_lib.py` -- the engine module that
  publishes a shared library and registers the standalone-Python `.pth` (mode
  2).
