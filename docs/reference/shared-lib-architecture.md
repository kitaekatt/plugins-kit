# Shared-lib architecture: review findings

Six findings about bootstrap's `shared_libs` mechanism (declared in a plugin's
`bootstrap.json`, implemented in `plugins/bootstrap/bootstrap_lib/shared_lib.py`
and driven by `_phase_shared_libs` in `plugins/bootstrap/bootstrap_lib/engine.py`).
Each was reviewed against the code at `dev` HEAD on 2026-09-20 and is recorded
here so it survives outside the session transcript that found it. Five are open
at HEAD; Finding 2 was fixed in bootstrap 0.123.0 on 2026-09-20 and is kept
here for the record. Being named in this document fixes nothing -- read each
finding's own status line.

Each finding's claim is a verbatim copy of a task plan's record, apart from the
fix write-up added under Finding 2 when it was closed; the original
reviewer notes were written to a session scratchpad that no longer exists, so
this document -- and the source citations added while writing it -- is the only
surviving record of the underlying claims.

This belongs in `docs/reference/`, not in
`plugins/bootstrap/skills/bootstrap/references/`, because it is maintainer
material: nobody who is not developing this repository reads it. Root
`CLAUDE.md` states the boundary ("That rule applies to documents a SKILL's
readers need. It stops at the plugin boundary").

## Scope: what 0.121.0 already fixed, and what this document is not

Commit `b23ff3c8` (bootstrap 0.121.0, 2026-09-20) fixed a separate, already-
closed defect in the same file: a pass run under `CLAUDE_BOOTSTRAP_DATA_ROOT`
(the `claudx` / `claude_plugin_test.py` test harness) was writing the owner-
phase standalone broadcast into the real, machine-wide standalone interpreter's
site-packages -- pointing a durable file at a disposable test data root. That
fix added the `CLAUDE_BOOTSTRAP_DATA_ROOT` gate around the broadcast, made the
`.pth` write atomic (`write_atomic`), and made `claude_plugin_test.py --print`
side-effect-free. All three are in place at HEAD.

The six findings below are unrelated defects in the same mechanism. Five are
open at HEAD; Finding 2 is closed, and its section records what the fix does.
Do not re-open or re-fix the `CLAUDE_BOOTSTRAP_DATA_ROOT` gate, the atomic
write, `--print`, or Finding 2 on the strength of anything in this document --
those are closed. The other five are open.

## Finding 1: two marketplaces publishing one package name collide in the standalone broadcast

**Confirmed.**

`_phase_shared_libs` (`engine.py`) runs an owner phase and a consumer phase.
The owner phase's `shared_root` is already scoped per marketplace:

```
shared_root = os.path.join(os.path.dirname(ctx.data_dir), "_shared_libs")
```

`ctx.data_dir` is `<data_root>/<marketplace>/<plugin>`, so `shared_root` is
`<data_root>/<marketplace>/_shared_libs` -- distinct per marketplace. But the
broadcast target is not:

```
link_shared_lib(lib_name, find_standalone_python(), shared_root)
```

`find_standalone_python()` (`shared_lib.py`) always returns the same single,
machine-wide interpreter path regardless of marketplace. `link_shared_lib`
resolves that interpreter's site-packages (`purelib_of`) and writes
`os.path.join(site, f"{name}.pth")` -- keyed only by package name, in the one
site dir every standalone-Python consumer shares. If marketplace A and
marketplace B each declare a `shared_libs` entry named `foo`, both write
`foo.pth` into the same site dir, each pointing at its own marketplace's
`_shared_libs/foo/`. Whichever plugin's manifest is processed last -- within a
pass, or across passes over time -- silently overwrites the other's link; the
standalone interpreter can resolve only one marketplace's `foo` at a time, with
no diagnostic naming the collision.

The consumer side does not have this problem, and the code shows the fix was
deliberate there. `_shared_lib_convergence_sweep` (`engine.py`) derives its
`shared_root` per plugin via `_plugin_data_dir`, whose docstring explains that
an earlier version of this function keyed by the *engine's* marketplace alone
and "collides when two marketplaces ship same-named plugins... each engine
iterates ALL installed plugins and writes the foreign-marketplace plugin into
its own tree, so the per-plugin data dir AND the derived `_shared_libs` sync
target last-writer-win between the two copies." The sweep's own dedup key is
explicit about the same class of bug: `key = (getattr(plugin_info,
"marketplace", ""), plugin_info.name, lib_name)`, with the comment "Include
marketplace in the dedup key: same-named plugins from two marketplaces link
into their own trees and must not skip each other."

So the exact failure mode named in this finding was identified and fixed for
the owner's sync target and for every consumer venv's link target. The
standalone broadcast was not brought under that fix: `find_standalone_python()`
takes no marketplace argument, and there is exactly one standalone interpreter
machine-wide for it to target.

## Finding 2: a failed import verification leaves the new link installed

**Confirmed at review time (2026-09-20). Fixed in bootstrap 0.123.0
(2026-09-20).**

`link_shared_lib` (`shared_lib.py`) wrote the `.pth` file before checking that
the import it promises actually works:

```
try:
    write_atomic(pth, desired + "\n")
except OSError as e:
    return SharedLibResult(name, "failed", f"failed to write {pth}: {e}")

if not _verify_import(python, name):
    return SharedLibResult(name, "failed", f"wrote {pth} but `import {name}` still fails")
return SharedLibResult(name, "linked", f"linked -> {pth}")
```

On a failed `_verify_import`, the function returned `status="failed"`, but the
`.pth` it had just written was already on disk -- nothing removed it. A
consumer venv, or the standalone interpreter, was left with a working path
entry pointing at a package that did not actually import cleanly under that
interpreter.

This compounded because the function's own cache check runs before any of the
above:

```
pth = os.path.join(site, f"{name}.pth")
desired = 'import sys; sys.path.insert(0, r"%s")' % entry_dir
if _read_text(pth) == desired:
    return SharedLibResult(name, "cached", f"linked (cached, {pth})")
```

Once a failed attempt had written `desired` to `pth`, every later pass read
that content back unchanged and returned `"cached"` -- without calling
`_verify_import` again. A link that never actually verified was therefore
treated as settled after its first failure, not retried.

Contrast with `sync_shared_lib` (the owner-side publish, same file), whose
`verify_python` check runs against the *staged* copy (`stage_root`) before
`_swap_directory` installs it as `dest_pkg`:

```
proc = subprocess.run([verify_python, "-c",
    f"import sys; sys.path.insert(0, {stage_root!r}); import {name}"], ...)
if proc.returncode != 0:
    return SharedLibResult(name, "failed", ...)
...
_swap_directory(stage_pkg, dest_pkg)
```

A failed owner verification leaves the previously-published (working) copy in
place, because the swap never happens. `link_shared_lib` had no staging step to
verify against before committing -- it wrote directly to the final path -- so
it lacked the safety property `sync_shared_lib` has. Pre-write verification was
never an option for `link_shared_lib` the way it is for `sync_shared_lib`: the
`.pth` is what makes `import <name>` resolve in the first place, so a check
run before the write would fail by construction.

**The fix is a rollback, not a reorder.** `link_shared_lib` now reads the
`.pth`'s exact prior content (`_read_raw`, unstripped, distinct from the
`_read_text` the cache check uses) immediately before the write, so it has a
true "prior state" to return to -- the file's previous bytes, or `None` when
there was no prior `.pth` at all. When `_verify_import` still fails after the
write, `_rollback_pth` either restores that prior content byte-for-byte
(`write_atomic`, so the restore is itself torn-write-safe) or removes the file
when there was nothing to restore. Either way, the next pass's cache check at
the top of `link_shared_lib` misses -- it will not find `desired` sitting in
`pth` -- so a never-verified link is retried instead of being trusted forever.
A rollback that itself fails (e.g. the file cannot be removed or rewritten) is
never swallowed: `_rollback_pth` reports it back through the `SharedLibResult`
message so a reader can tell a cleanly-retried failure from one that left a
broken `.pth` in place needing manual attention. Tests:
`TestLinkRollback` in `tests/bootstrap/test_shared_lib.py`, including two
control tests (a successful link still writes; a genuinely current link still
hits the cache path unchanged) so the rollback tests cannot pass vacuously.

## Finding 3: uninstall revokes nothing

**Confirmed.**

`shared_lib.py` has no function that deletes a published package tree, a
`.src.sha256` hash file, or a `.pth` link. The only `shutil.rmtree` calls in
the module clean up a publish's own staging directory or swap backup
(`stage_root`, `backup` in `sync_shared_lib` / `_swap_directory`), not the
installed artifacts. `engine.py` has no phase, hook, or code path that fires on
a plugin being uninstalled or disabled -- bootstrap only runs at SessionStart to
provision; nothing in this codebase participates in Claude Code's own plugin
uninstall.

Consequence: after the owning plugin is uninstalled or disabled,
`_shared_libs/<name>/<name>/` and its `.src.sha256` stay on disk exactly as
published, and every `.pth` that was ever written for it -- the standalone
interpreter's site-packages, and every consuming plugin's own venv -- keeps
resolving, because nothing removes them. A consumer that still declares
`shared_lib_imports: ["<name>"]` keeps importing successfully from a package
whose owner is gone, with no signal anywhere that this happened. The only way
this state changes is a later publish overwriting the same name (from the same
or a different owner) or a human deleting the directory by hand -- and manual
deletion of bootstrap-managed state is exactly the hand-repair anti-pattern
root `CLAUDE.md`'s "Anti-pattern: repairing a wedged machine by hand" section
warns against.

## Finding 4: the published copy carries no version marker

**Confirmed, with one nuance.**

`sync_shared_lib` (`shared_lib.py`) writes exactly two things under
`entry_dir = <shared_root>/<name>/`: the copied package tree at
`<entry_dir>/<name>/`, and a hash file:

```
hash_file = os.path.join(entry_dir, ".src.sha256")
...
with open(hash_file, "w", encoding="utf-8") as f:
    f.write(current + "\n")
```

`current` is `_hash_tree(src_pkg)`'s sha256 over every file's relative path and
bytes -- a change-detection fingerprint used only to decide whether to skip a
re-sync (`if os.path.isdir(dest_pkg) and _read_text(hash_file) == current:
return cached`). It carries no ordering and no relationship to the owning
plugin's semantic version; it cannot tell a consumer "this is newer than that."
Nothing else is written -- no `.version` file, no copy of the owner's
`plugin.json` version.

`library-consumption.md`'s cross-cutting section tells a consumer needing a
minimum capability to "probe for that capability at runtime... check for the
symbol, function, or behavior it actually needs (`hasattr`, a version constant
the library itself exports and documents, a try/except around the specific
call)." The nuance: that advice does not strictly require a version file
bootstrap itself writes -- if the *owner package's own source* defines something
like `__version__` in its `__init__.py`, `sync_shared_lib`'s `shutil.copytree`
carries it into the published copy unchanged, and a consumer probing
`pkg.__version__` would see it. But bootstrap's publish path supplies no such
marker itself, does not require one, and does not keep one in sync with the
owning plugin's `plugin.json` version if one happens to exist. A consumer with
no reason to expect the owner package to self-version has nothing on disk to
probe, exactly as the finding states.

## Finding 5: "picks up fresh source on its next import" is misleading

**Confirmed.**

`library-consumption.md`, Mode 3 ("Foreign-interpreter project consumer"),
"Update" section, states:

> ...a scheduled job or a long-running process using the project's interpreter
> picks up fresh source on its next import.

This is misleading for exactly the audience the sentence addresses (a
long-running process). CPython caches every imported module object in
`sys.modules`, keyed by fully-qualified module name. Once `import pkg` has run
once in a process, every later `import pkg` statement in that process is a
cache hit: it returns the existing module object without re-reading
`__init__.py` or re-executing any of the package's top-level code.
`sync_shared_lib`'s re-sync replaces what is on disk (`_swap_directory` does an
`os.replace` of the whole package directory), but that changes only the
filesystem -- it does nothing to a process's `sys.modules` entry. A process
that already holds `pkg` keeps running the module object it imported before
the swap until it explicitly calls `importlib.reload(pkg)`, deletes the
`sys.modules` entry, or restarts. "Next import" reads as "the next `import`
statement," which is false for that process; the accurate claim is closer to
"next process start."

The finding's second half is also real and sharper: because the resync is a
directory-level swap and Python resolves each submodule independently the
first time something imports it, a process can end up running two generations
of the same package at once. If a process already imported `pkg` (pinned to
the pre-swap generation in `sys.modules`) and later does `import
pkg.newmodule` for a submodule that did not exist, or was simply never
imported, before the resync, that submodule loads fresh off the post-swap
tree -- coexisting in the same process with the pre-swap `pkg` package object.
If the two generations' internal contract changed together (a renamed helper,
a changed internal signature, a moved constant), the mix can misbehave in a
way that has no "stale code" signature to search for, because part of the
package genuinely is current.

## Finding 6: `find_standalone_python` duplicates `interpreter_env.standalone_python` with a different POSIX path

**Confirmed.**

`find_standalone_python` (`shared_lib.py`):

```
base = os.path.join(os.path.expanduser("~"), ".local", "share",
                     "python-standalone", "python")
candidate = (os.path.join(base, "python.exe") if sys.platform == "win32"
             else os.path.join(base, "bin", "python3"))
return candidate if os.path.exists(candidate) else None
```

`standalone_python` (`interpreter_env.py`):

```
if windows:
    path = os.path.join(home, ".local", "share", "python-standalone",
                        "python", "python.exe")
else:
    path = os.path.join(home, ".local", "bin", "python3")
```

The Windows candidates agree byte-for-byte:
`<home>/.local/share/python-standalone/python/python.exe`. The POSIX
candidates do not:

- `find_standalone_python()`: `<home>/.local/share/python-standalone/python/bin/python3`
  -- checked directly against the filesystem with `os.path.exists`.
- `standalone_python()`: `<home>/.local/bin/python3` -- per its own docstring,
  "the symlink the hook maintains" (i.e. a separate path the SessionStart hook
  is responsible for keeping current, not the interpreter's own install
  layout).

These encode two different beliefs about where the canonical interpreter lives
on POSIX. `interpreter_env.py`'s module docstring names
`STANDALONE_DIR_REL = ".local/share/python-standalone"` as a literal "MIRRORED
(not linked) in session-bootstrap.sh, bootstrap.sh, bootstrap-display.sh,
shell/project-python.sh, and defaults/config.json good_python_dir; a drift test
asserts the spellings agree." `shared_lib.py` is not in that mirrored list and
is not covered by that drift test. Every existing test that touches
`find_standalone_python` (`tests/bootstrap/test_shared_lib.py`) stubs it out
with `monkeypatch.setattr(shared_lib, "find_standalone_python", lambda: ...)`
rather than checking it against `standalone_python()`'s answer, so the two are
free to diverge further with nothing noticing.

Practical consequence: if the symlink `standalone_python()` relies on and the
interpreter's own `bin/python3` layout `find_standalone_python()` checks for
ever disagree -- the symlink missing, stale, or pointing elsewhere, or the
standalone Python distribution not laying out `bin/python3` where expected --
the two functions can resolve to different files, or one can resolve and the
other return nothing. `_phase_shared_libs`'s owner broadcast uses
`find_standalone_python()` exclusively; every other engine-side reference to
"the standalone interpreter" (`begin_pass`, persistent PATH writes, and the
rest of `interpreter_env.py`) uses `standalone_python()`. `link_shared_lib`
treats a `None` interpreter as a silent soft-skip (`not python or not
os.path.exists(python)` -> `status="skipped"`), so a `find_standalone_python()`
miss produces no error, only a broadcast that quietly does not happen even
though `standalone_python()` would have resolved a real, working interpreter.

## Sources read

- `plugins/bootstrap/bootstrap_lib/shared_lib.py` -- `link_shared_lib`,
  `sync_shared_lib`, `find_standalone_python`, `_swap_directory`, `_hash_tree`.
- `plugins/bootstrap/bootstrap_lib/engine.py` -- `_phase_shared_libs`,
  `_shared_lib_convergence_sweep`, `_plugin_data_dir`, `_SharedLibLinkLog`
  (read-only; owned by another unit for edits).
- `plugins/bootstrap/bootstrap_lib/interpreter_env.py` -- `standalone_python`,
  `STANDALONE_DIR_REL`.
- `plugins/bootstrap/skills/bootstrap/references/library-consumption.md`
  (read-only).
- `tests/bootstrap/test_shared_lib.py` -- confirms no test exercises
  `find_standalone_python()` against real output.
- `git show b23ff3c8` -- the 0.121.0 fix this document's scope boundary
  excludes.
