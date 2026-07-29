# Action-Triggered Plugin Install

How a skill makes a specialty plugin available without installing it for a whole
team: declare it `install: "manual"`, then let the ACTION that needs it ask the
user at the moment of need.

Audience: skill authors wiring an optional plugin dependency, and anyone reading
a project `bootstrap.json` that lists a plugin nobody seems to install.

## Motivation

The default way to express "this project needs plugin X" is a `plugins[]` entry
with `install: "auto"`, which installs X for every developer who opens the
project. That is correct for a genuine dependency -- something the project
cannot function without, or a shared lib an always-loaded import needs (see the
`shared_lib_imports` pairing rule in
[manifest-reference.md](manifest-reference.md)).

It is the wrong shape for a SPECIALTY plugin: one that only some developers
ever exercise, that carries its own credentials or paid API access, or that is
needed by a single skill nobody on the team invokes daily. Auto-installing that
for everyone provisions venvs, config prompts, and fix-all items on machines
that will never run the code -- noise indistinguishable from breakage.

The reverse failure is just as bad: omitting the plugin entirely. Then the
skill fails at the moment of need with a raw `ModuleNotFoundError` or a missing
shared-lib path, the user has no idea which plugin owns the missing piece, and
nothing keeps an opted-in install up to date afterwards.

Action-triggered install is the middle position: **declared but not installed,
installed at the moment of need, and kept fresh once opted into.** The decision
belongs to the developer who actually hits the requirement, taken at the one
moment they have the context to make it.

## The three-part flow

The pattern has three parts, each owned by a different file. All three are
required -- any two without the third reproduce one of the failure modes above.

1. **Declare, do not install.** The project (or plugin) `bootstrap.json` lists
   the plugin in `plugins[]` with `install: "manual"`. The plugin is then a
   known, named member of the marketplace -- discoverable, version-managed, and
   opt-in per developer. The engine never installs, enables, disables, or
   re-scopes it; it only keeps an ALREADY-installed copy up to date via
   `claude plugin update`. Install state is the user's; version freshness is
   bootstrap's.

2. **Preflight at the point of need.** The skill or action that requires the
   plugin checks for it BEFORE doing the work -- by testing the published
   shared-lib path, or by attempting a guarded import. On a miss, the skill
   does not fail and does not install silently: it tells the user which
   capability is unavailable, names the plugin that provides it, and ASKS
   whether to install it. Offer the no-install path in the same breath when one
   exists (a mock/offline backend, a dry run, a reduced mode).

3. **Install on consent, then retry.** On yes, run
   `claude plugin install <plugin>@<marketplace>`. Bootstrap's mid-session
   install relaunch provisions the new plugin WITHOUT a restart, so the action
   can simply be retried a prompt or two later.

## Mechanics

### Preflight check

Two forms, in order of preference:

- **Shared-lib path** -- cheapest and side-effect free. A published shared lib
  lands at
  `~/.claude/plugins/data/<marketplace>/_shared_libs/<lib_name>/`, so the
  check is a directory-exists test. Prefer this when the requirement is a
  shared lib rather than the plugin's commands or skills.
- **Guarded import** -- `try: import <lib>` / `except ImportError:` inside the
  code path that needs it. Use a LAZY import at the call site, never a
  top-level one: a top-level import turns a missing optional dependency into an
  unconditional module-load failure, which breaks the very code path that was
  supposed to detect and report it.

Whichever form, the failure message must name the plugin that provides the
capability -- "install llm-scripting-kit" is actionable; "No module named
llm_scripting_kit" is not.

### Asking

The install is a user decision, so ask explicitly rather than inferring
consent. Present three things: the capability that is unavailable, the exact
command that would provide it, and the fallback if there is one. Do not install
without an answer, and do not treat a previously-declined install as settled
for the rest of the session's unrelated work -- re-asking on the next genuine
need is correct.

### Installing

```
claude plugin install <plugin>@<marketplace>
```

For a plugin declared in this marketplace, that is e.g.
`claude plugin install llm-scripting-kit@plugins-kit`.

### No restart needed

A plugin installed mid-session is provisioned in the SAME session. The
UserPromptSubmit hook compares a content hash of the registry plugins map plus
settings `enabledPlugins` (`plugins_state_hash`) and relaunches
`session-bootstrap.sh` once per change; the pass creates the new plugin's venv
and publishes its shared libs. So the correct advice after installing is
"retry in a moment", NOT "restart Claude Code".

The one caveat is a race, not a restart requirement: an action fired
immediately after the install can beat the detached pass. Retry a prompt or two
later. Full mechanics: `update_lifecycle` in SKILL.md and
[plugin-reload-lifecycle.md](plugin-reload-lifecycle.md).

### What `install: "manual"` costs you

`enabled` and `scope` are IGNORED for a manual entry -- the user owns those
decisions, so a manifest cannot force the plugin on at a particular scope.
`min_version` is honored only for `install: "auto"` entries, so a manual entry
cannot pin a floor; communicate a version requirement out-of-band or preflight
for the specific capability rather than the version. Field-level detail:
[manifest-reference.md](manifest-reference.md) (`plugins` entry fields).

## Wiring it as a skill author

1. Add the `plugins[]` entry with `install: "manual"` to the project's
   `.claude/bootstrap.json` (or the plugin's own `bootstrap.json` when the
   requirement belongs to a plugin rather than a project). Include a `$comment`
   saying which skill needs it and why it is opt-in.
2. Add a short **Requirements** note to the SKILL.md (or the reference the
   action lives in) stating: the capability that needs the plugin, the
   preflight check, the install command, and the no-plugin fallback. Keep it to
   a few lines -- it is a precondition, not a subject.
3. Make the code's own failure message agree with the skill text. When a
   library already raises a "needs plugin X, or use Y instead" error, the skill
   note must not contradict it; a mismatch between the two is how a user ends
   up installing the wrong thing.
4. Do not gate unrelated parts of the skill on the requirement. Preflight in
   the action that needs it, so every other action keeps working uninstalled.

The pattern is extensible by construction: any number of skills can each
declare their own plugin requirements this way, and a plugin required by
several skills is simply preflighted by each of them.

## Choosing between this and `install: "auto"`

| Situation | Declare |
|-----------|---------|
| A top-level import / always-on code path needs the lib | `auto` |
| The plugin is required for the project to function at all | `auto` |
| One skill's optional backend needs it; a mock/offline path exists | `manual` + preflight |
| The plugin needs credentials or paid access not everyone has | `manual` + preflight |
| Only some developers ever run the action | `manual` + preflight |

The test is whether a developer who never invokes the action would notice the
plugin's absence. If no, it is action-triggered.
