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

### Why shared libs rather than published packages

Sharing SOURCE at a stable path and linking it with a `.pth` is deliberate, not
a workaround for lacking an index. Every consumer is in-fleet, so one publish of
the OWNING plugin updates the source every consumer resolves -- no version bump,
no dependency constraint, no reinstall anywhere. The cost is the other side of
that same coin: consumers cannot pin, so a breaking change to a shared lib
reaches all of them at once.

The venv-scoping above is the ordinary consequence of a per-venv install rather
than fragility -- a `.pth` written into one environment no more appears in
another than a `pip install` does. The re-exec rule below is how a script
satisfies that precondition itself instead of pushing it onto its caller.

**The source is shared; third-party dependencies are not.** A plugin that
imports a shared lib declares that lib's third-party requirements in its OWN
`pyproject.toml` -- a consumer driving `llm_scripting_kit`'s OpenRouter path
declares `openai` itself. One shared lib is linked into several independently
provisioned venvs, so shipping its pins with it would impose a single resolution
on every consumer, and a consumer using only the paths that need no SDK would
install one anyway. `tests/bootstrap/test_dependency_completeness.py` catches an
omission.

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

**Test gotcha: this same re-exec silently short-circuits pytest.** Importing
`prepare_review.py` triggers `reexec_under_plugin_venv`, which on a machine with
the plugin's venv provisioned calls `os.execv` and abandons the pytest process
ITSELF, not just the import -- so the run stops at collection with **exit 0 and
no output at all: a false green**. Setting `_BOOTSTRAP_GUARD_VENV_REEXEC=1`
makes the re-exec a no-op (see `_REEXEC_GUARD_ENV` in `bootstrap_guard.py`),
matching how the real script is invoked once the guard has already fired.

**Set it in the test package's `conftest.py`, not at the invocation.** Every
affected test dir does this at import time, so a bare `pytest tests/<dir>` is
safe with nothing to remember:

```python
os.environ.setdefault("_BOOTSTRAP_GUARD_VENV_REEXEC", "1")
```

Current setters: `tests/awesome-kit`, `tests/git-kit`, `tests/p4-kit`,
`tests/unreal-kit`. A dir whose tests import a re-execing script and which does
NOT set this is a latent false green, and the failure hides itself: in a
full-suite run an earlier conftest (alphabetically, `tests/awesome-kit`) sets
the var first, so the dir looks healthy and only breaks when run ALONE -- i.e.
in exactly the targeted TDD loop, never in CI. `tests/p4-kit` sat in that state
and silently ran 0 of its 188 tests (fixed 2026-08-09). When adding a
module-level `reexec_under_plugin_venv` to a script, check its test dir.

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
