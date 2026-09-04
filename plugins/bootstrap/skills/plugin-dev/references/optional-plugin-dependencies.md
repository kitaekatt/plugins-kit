# Optional plugin dependencies: plugin A uses plugin B if it is installed

How a plugin in this marketplace consumes another plugin's shared library when
that plugin may be absent, older than expected, or left behind after an
uninstall -- and what the consumer owes the user in each state.

The one-line rule and the submit gate live in `plugins/CLAUDE.md`. This
document is the detail: the decision rule, the mechanics each branch requires,
the states a probe must distinguish, and what a reviewer checks.

Audience: an author adding a cross-plugin import to a shipped plugin, and a
reviewer of that change. It does not restate the bootstrap mechanics it builds
on; those are shipped with the bootstrap skill and are cited where they apply:

- `plugins/bootstrap/skills/bootstrap/references/deferred-requirements.md`
  -- the escalate-vs-defer rule (does a developer who never invokes the
  capability notice it is unmet?).
- `plugins/bootstrap/skills/bootstrap/references/action-triggered-install.md`
  -- `install: "manual"`, preflight-then-ask, the mid-session install.
- `plugins/bootstrap/skills/bootstrap/references/library-consumption.md`
  -- the `.pth` linking model, and why no consumer can pin a version.
- `plugins/CLAUDE.md`, "Why shared libs rather than published packages" and
  the shared-lib version-probing paragraph.

## Vocabulary

**Owner** -- the plugin that declares `shared_libs` and ships the library
(`llm-scripting-kit` ships `llm_scripting_kit`).

**Consumer** -- the plugin that declares `shared_lib_imports` and imports it.

**Artifact** -- the thing the consumer's action hands the user: a rendered
review, a rendered policy, a JSON envelope, a file. The artifact is where
truthfulness is judged, because it is the only thing the user reads.

**Frontier symbol** -- of the symbols a consumer uses from the library, the
one with the latest owner ship date. It is the symbol that decides whether a
linked copy is "current enough", so it is the one to probe.

## The three runtime states, and why `import` cannot tell them apart

A consumer venv is linked to the owner's source by a `.pth` that names a
version-independent directory (`_shared_libs/<name>/`). Consequences that are
true by construction (`plugins/bootstrap/bootstrap_lib/shared_lib.py`,
`link_shared_lib`; `engine.py`, `_phase_shared_libs`):

1. **Absent.** The owner was never installed, so the directory does not
   exist. The engine records the consumer link as `skipped` and routes it to
   `ctx.ok`, which is verbose-only. **Nothing is shown at session start.** The
   point of need is the first and only place the user learns anything.
2. **Too old.** The owner is installed at a version that predates a symbol
   the consumer uses. `import <lib>` succeeds; the symbol is missing. The
   failure is `ImportError: cannot import name ...` on a `from` import, or an
   `AttributeError` deep in a call path.
3. **Stale after uninstall.** The owner was installed, then removed. Nothing
   prunes `_shared_libs/<name>/` or the consumer's `.pth`, so `import <lib>`
   still succeeds against whatever source was last synced. To the consumer
   this is state 2 wearing state 1's clothes.

`ModuleNotFoundError` identifies state 1 only. States 2 and 3 are identified
only by probing for the frontier symbol. A message that says "not installed"
for state 2 or 3 tells the user to install a plugin they already have.

## The decision rule

Answer two questions, in order.

**Q1. Can the plugin do its job at all without B?**

No -> **REQUIRED.** B is a dependency, not an option. Declare it so it is
installed for everyone (below) and import it as you would any dependency. The
rest of this document does not apply.

Yes -> B is optional. Go to Q2. The test is deferred-requirements.md's: a
developer who never invokes the B-backed capability must not notice B is
unmet.

**Q2. When B is unavailable, can the action still hand the user an artifact
that is TRUE as read?**

Ask it concretely: a reader with no other information reads the artifact. Do
they conclude that B participated, or that the world is B-less, when neither
is the case? If so, the artifact is a false claim.

- The artifact would be read as if B had participated, or it asserts a fact
  about the machine that B's absence falsifies, and the consumer cannot state
  the gap INSIDE that artifact -> **REFUSE.** The unit that needs B fails
  with a diagnosis. It produces nothing that could be mistaken for the real
  thing.
- The consumer can state the gap inside the artifact the user reads -> **DEGRADE
  WITH DISCLOSURE.** The unit that needs B is omitted, and the artifact
  carries one line saying what is missing and why.

Both branches share an absolute: **never substitute.** Not a cheaper model
for the configured one, not a config-literal for a rendered value, not an
empty result for a failed one, unless the substitution is named in the
artifact. A silent substitute is the one outcome this rule exists to prevent,
because the artifact then looks identical whether B ran or not.

**"Did the user ask for it" is a proxy, not the axis.** A B-need that exists
because the user configured it (a review profile naming an endpoint, a
workflow node naming a transport, a job naming endpoints) nearly always lands
in REFUSE, because the user will read the artifact against their own
configuration. But a B-need the user never asked for can still produce a false
claim -- a rendered policy that says "the models listed are the only ones that
exist here" while omitting a harness the user installed is false whether or
not the user asked for the row. The question to answer is always Q2.

**Refuse at the unit, disclose at the artifact.** In practice both branches
have the same two-level shape. The unit that needs B (one review lane, one
harness-model row) refuses or is omitted; the enclosing artifact (the review,
the policy) is still produced, and it is the enclosing artifact that carries
the disclosure. A failed lane exits non-zero; the review renders a `## Lane
failures` section. The rule is that the disclosure must reach the artifact the
user reads. A note that appears only under a `--verbose` or `--explain` flag is
diagnostics, and diagnostics are not disclosure.

## Mechanics: REQUIRED

- `bootstrap.json`: `plugins[]` entry for the owner with `install: "auto"`,
  and `shared_lib_imports` naming the library. content-pipeline-kit's
  `bootstrap.json` is the worked example, and its `$comment` states the
  distinction from the action-triggered pattern.
- `plugin.json` `dependencies`: only `bootstrap` is universal
  (`plugins/CLAUDE.md`, "Plugin dependencies on bootstrap"). Adding the owner
  there is permitted for a REQUIRED dependency and makes Claude Code install
  it at plugin-install time rather than at the next bootstrap pass. Do not add
  it for an optional dependency: it would install B for every consumer of A.
- `pyproject.toml`: every third-party package the imported paths need, as
  ordinary `dependencies`. `tests/bootstrap/test_dependency_completeness.py`
  enforces this.
- Code: import at module top if the whole module needs it; otherwise lazily.
  Guard for the installed-but-not-yet-provisioned window with
  `bootstrap_guard` as every plugin does.

## Mechanics: optional (REFUSE or DEGRADE)

### Manifest side

- `shared_lib_imports` **must** name the library. The engine soft-skips when
  the owner is absent, so the declaration costs nothing on a machine without
  B, and without it a machine WITH B is still not linked. An optional import
  with no `shared_lib_imports` entry never works anywhere.
- `plugin.json` `dependencies` must NOT name the owner.
- `plugins[]` with `install: "manual"` is optional discoverability, per
  action-triggered-install.md. Include it when a developer browsing the
  manifest would otherwise not know which plugin provides the capability.
- `pyproject.toml` third-party packages. The completeness test follows a
  first-party import into the library REGARDLESS of a `try/except ImportError`
  around it, and exempts a guarded third-party leaf only in the consumer's own
  files. So a consumer of `llm_scripting_kit` must declare `openai` even when
  it never reaches a transport. Two shapes satisfy the test:
  - a hard dependency (awesome-kit, content-pipeline-kit, job-kit,
    workflow-kit): provisioned for everyone; costs an SDK install on machines
    that never use it.
  - an optional extra (git-kit, p4-kit: `endpoint-dispatch = ["openai"]`):
    provisioned only when the plugin's OWN `bootstrap.json` lists it under
    `venv.extras`. That file ships in the plugin cache. A layered
    `~/.claude/bootstrap.json` or `<project>/.claude/bootstrap.json` is merged
    with the other layered files, not into a plugin's manifest, so a consumer
    has no supported way to enable another plugin's extra.

  Rule: **an optional extra that no supported configuration enables is a
  declaration that provisions nothing.** Either the plugin enables it in its
  own `venv.extras` (at which point it is a dependency with a group name), or
  the code path that needs it is unsupported and the error must say so
  rather than direct the user to a file they cannot edit.

### Code side

1. **Lazy import at the call site.** Never at module top; a top-level import
   turns absence into a load failure of the code that was supposed to
   diagnose it.
2. **Distinguish the three states.** Let `ModuleNotFoundError` mean absent.
   For a module that imports, probe the frontier symbol (`getattr` /
   `hasattr`, or a `from` import whose `ImportError` is caught separately
   from the module import). A missing frontier symbol is "too old or stale",
   never "not installed". job-kit's `lib/job_kit/select.py`
   (`SharedLibTooOldError`) is the worked example: absent propagates as the
   plain `ModuleNotFoundError`; present-but-missing-a-symbol is diagnosed by
   name with the owner version the symbol first shipped in.
3. **Probe the frontier, not a convenient symbol.** A probe on a symbol older
   than a hard import elsewhere in the same file protects against a skew that
   cannot occur. When a hard import of a newer symbol sits on the same path,
   the probe is decoration. List the symbols you use, find the one the owner
   shipped last (`git log -S<symbol>` on the owner's tree), and probe that one.
4. **The message names the owner plugin and the remedy command.** Absent:
   `claude plugin install <owner>@<marketplace>`. Too old or stale:
   `claude plugin update <owner>@<marketplace>`, with the owner version the
   frontier symbol first shipped in. Name the no-B alternative in the same
   sentence when one exists (an Agent model, a harness endpoint). Never name
   the consumer's own `bootstrap.json` or `pyproject.toml` as the remedy: a
   consumer of a published plugin cannot edit either.
5. **Disclosure reaches the artifact.** In DEGRADE, the omission is stated in
   the rendered output the user reads, one line, naming the capability and
   the state (absent / too old). In REFUSE, the enclosing artifact carries the
   failed unit by name and states what it would have covered. A note that
   reaches only a diagnostic mode does not satisfy this.
6. **No silent substitution.** A fallback value may be used only when the
   artifact says a fallback was used and why.
7. **Wrap the import in `except ImportError`, and the CALL in a narrower
   handler.** An `except Exception` around the import hides a syntax error in
   a half-synced copy; an `except ImportError` around a call misses the
   `AttributeError` a skewed copy raises. Keep the two guards at their own
   boundaries.

### Tests

Every optional import ships with tests for BOTH failure states, driven by
`sys.modules` fakes:

- absent: `monkeypatch.setitem(sys.modules, "<lib>", None)` -> the absent
  message class.
- too old: a `types.ModuleType("<lib>")` lacking the frontier symbol -> the
  too-old message class, not the absent one.

And for DEGRADE, one test that the disclosure line appears in the RENDERED
artifact, not only under a diagnostic flag.

## Applying the rule to the shipped cases

| Consumer | Branch | Where the rule holds | Where it does not |
|---|---|---|---|
| git-kit / p4-kit `run_review_lane.py` (endpoint lane) | REFUSE | Exit 2 with a reason; SKILL.md forbids substitution; `## Lane failures` carries the gap into the review; `openai` pre-flighted by name. `ModuleNotFoundError` is caught separately from a `hasattr` probe over `BackendOptions`, `HaltError` and `create_backend`, so absent and too-old raise different messages and both name the owner plugin and a `claude plugin install` / `update` command. | The `AgentTimeoutError` probe is not the frontier: `BackendSelection.kind` and `.effort` remain hard uses outside the probed set. The `endpoint-dispatch` extra is not enableable by a consumer, so a transport endpoint is refused rather than provisioned. |
| git-kit / p4-kit (md-domain claim) | DEGRADE | Skill probes for the skill by name, degrades, and notes the degradation in one line of the review. | -- |
| awesome-kit `orchestration_guidance.py` | DEGRADE | Feature-detected by frontier symbols (`discover_model_entries`, `HARNESS_KIND`, `resolve_harness_adapter`); config-literal fallback is disclosed as a note; states 2 and 3 are handled by the same probe. Absent and too-old carry distinct wordings, each naming the owner plugin and a `claude plugin install` / `update` command, and the notes are rendered into the policy under a `Degraded render.` heading rather than only under `--explain`. | -- |
| workflow-kit `openrouter_run.py` | REFUSE | Names the owner plugin. | Does not distinguish state 2; no frontier probe. |
| job-kit `select.py` | REFUSE | Distinguishes absent from too old, names version and command. | -- |

## Reviewer checklist

For every cross-plugin import a change adds or edits:

- [ ] Q1 answered in the change description: REQUIRED, or optional. If
      REQUIRED, the owner is `install: "auto"` (or a `plugin.json` dependency)
      and third-party packages are hard dependencies.
- [ ] Q2 answered: REFUSE or DEGRADE, with one sentence on why the artifact
      stays true. "The user asked for it" is not the sentence; the sentence
      says what a reader would wrongly conclude without disclosure.
- [ ] `shared_lib_imports` names the library; `plugin.json` does not name the
      owner.
- [ ] Third-party packages are declared, and if declared as an extra, the
      extra is enabled somewhere a consumer can reach, or the path is
      reported as unsupported.
- [ ] The import is lazy and guarded with `except ImportError`; calls are
      guarded separately.
- [ ] The probe targets the frontier symbol; the message for a present
      module lacking it says "too old", names the owner version, and gives
      `claude plugin update`; the message for `ModuleNotFoundError` gives
      `claude plugin install`.
- [ ] No message tells a consumer to edit the plugin's own manifest.
- [ ] In DEGRADE, the disclosure line is in the rendered artifact; a test
      asserts it there.
- [ ] In REFUSE, the enclosing artifact names the failed unit and what it
      would have covered; nothing substitutes silently.
- [ ] Tests cover absent AND too-old, as distinct assertions.

## Relationship to the shipped bootstrap references

This document adds three things those references do not state: the Q2 split
between refusing and degrading, the three runtime states and the requirement
to diagnose them apart, and the rule that disclosure must reach the artifact.
Everything about declaring, linking, preflighting, asking, and installing is
theirs and is not repeated here. Two of them would carry a sentence each of
this document's content well, and should be extended in place rather than
duplicated: library-consumption.md's "version declaration is unsupported
everywhere" section (name the three states and the two message forms) and
action-triggered-install.md's preflight section (an import that succeeds does
not prove the library is current).

The skill-embedded, silence-on-absence sub-case of DEGRADE is defined in
[skill-embedded enabling](enabling.md). It applies only when the artifact stays
true without the owner and a consuming skill provides the consented probe host.
