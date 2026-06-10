# plugins/ -- plugin implementation conventions

Implementation-level conventions for the plugin code under this directory. Repo
orientation, the publish flow, and the bootstrap engine overview live in the
root `CLAUDE.md`; this file is the home for "how to write the plugin code
itself" details that only matter when you are editing a plugin.

## The bootstrap-provisioned venv and shared libs

The bootstrap plugin provisions a dedicated venv per plugin at a stable path
that does not change across versions:

```
Windows:     ~/.claude/plugins/data/<marketplace>/<plugin>/.venv/Scripts/python.exe
macOS/Linux: ~/.claude/plugins/data/<marketplace>/<plugin>/.venv/bin/python
```

A plugin can share a library with other plugins by declaring it in
`bootstrap.json`:

```json
"shared_lib_imports": ["bootstrap_lib"]
```

Bootstrap links the shared lib onto that plugin's venv via a `.pth` file. The
shared lib is therefore importable ONLY under the provisioned venv -- a
uv-managed venv (`uv run`) or a bare `python` builds a different environment
that has no such `.pth`, so the import fails there.

### Shared-lib scripts must re-exec under the plugin venv

**Rule:** a standalone script that hard-imports a bootstrap shared lib (e.g.
`from bootstrap_lib... import ...`) MUST call
`bootstrap_guard.reexec_under_plugin_venv("<plugin>")` at module top, BEFORE the
shared-lib import:

```python
from bootstrap_guard import reexec_under_plugin_venv   # vendored, stdlib-only
reexec_under_plugin_venv("p4-kit")

from bootstrap_lib.code_review.chunking import ...      # now resolvable
```

**Why:** a script must not trust the interpreter that launched it. Skills name a
script as `tool: ${CLAUDE_PLUGIN_ROOT}/scripts/foo.py` with no interpreter, so an
agent runs it under `python` / `uv run python` -- neither carries the shared-lib
`.pth`. Without the re-exec the import fails and the except-handler emits a
MISLEADING "bootstrap has not provisioned ... (missing: bootstrap_lib)" message
*even though provisioning succeeded* -- the venv just was not the one running.
`reexec_under_plugin_venv` re-execs into the provisioned venv (a no-op when
already there), making the script invocation-method-agnostic. This was the
actual `p4-kit` / `git-kit` `prepare_review.py` failure mode (fixed 2026-06-02).

`bootstrap_guard` is stdlib-only, so importing it can never itself trip the
missing-shared-lib failure (the vendoring discipline that keeps it that way is
the next section).

The SKILL.md-side companion (write the explicit venv path in skill examples
rather than `uv run python`) is documented in the root CLAUDE.md insight
`host_python_via_plugin_venv`. With the script-side re-exec in place, the
SKILL.md guidance is a nicety, not a load-bearing requirement.

## bootstrap_guard.py is vendored byte-for-byte

`bootstrap_guard.py` is a stdlib-only guard that must run when `bootstrap_lib`
itself may be absent, so each consuming plugin ships its own copy rather than
importing the canonical. The canonical lives at
`plugins/bootstrap/bootstrap_lib/bootstrap_guard.py`; vendored copies live next
to the script that imports them (e.g. `plugins/p4-kit/scripts/bootstrap_guard.py`).

**Rule:** edit the canonical, then copy it byte-for-byte into every vendored
location. `tests/bootstrap/test_bootstrap_guard.py` asserts every copy matches
the canonical, and the guard must never `import bootstrap_lib`. Current vendored
copies: `git-kit/scripts`, `p4-kit/scripts`, `skills-kit/scripts`,
`unreal-kit/lib`, `awesome-kit/skills/task/scripts`.

`path_repair.py` follows the same vendoring discipline.
