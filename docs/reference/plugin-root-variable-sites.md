# CLAUDE_PLUGIN_ROOT variable sites

Record of the migration of agent-typed `${CLAUDE_PLUGIN_ROOT}` command sites
under `plugins/`: the mechanism that made them broken, the classification the
migration worked from, and the reasons for the replacement contract.
Maintainer material: it lives in `docs/`, not in a skill, because nobody reads
it on a machine that is not ours.

The migration landed in the change that added this file. The guard
`tests/repo-scripts/test_claude_plugin_root_expansion.py` holds the property at
zero offenders (7 tests pass, verified 2026-10-01); a regression is a guard
failure, not an entry to add here.

Line numbers in this file are a snapshot of the tree on the survey date
(2026-10-01) and drift as files change. They were NOT re-derived after the
migration: the taxonomy and the reasons matter, and a reader re-finds any site
with `grep -rn 'CLAUDE_PLUGIN_ROOT' plugins/<name>/` before editing. The
migrated-site lists below are pre-migration locations and serve as a map of
what was touched, not as current coordinates.

## Mechanism

- `CLAUDE_PLUGIN_ROOT` is expanded by Claude Code only in values the harness
  reads before executing them: a `hooks/hooks.json` `command:` field, and a
  skill `!` preload line. The preload case is established in
  `plugins/bootstrap/skills/bootstrap/references/python-interpreter.md`
  (section "Skill preload commands"): Claude Code refuses a preload containing
  any shell expansion, and only the names it substitutes itself, such as
  `${CLAUDE_PLUGIN_ROOT}` and `${CLAUDE_SESSION_ID}`, may appear. The bootstrap
  `SKILL.md` restates it.
- Proof for hooks.json: `plugins/bootstrap/hooks/hooks.json` line 8 launches
  `hooks/sessionstart/session-bootstrap.sh`, the only writer of
  `BOOTSTRAP_PYTHON`, and that variable is set in sessions.
- The variable is UNSET in the Bash tool environment. An agent-typed
  `${CLAUDE_PLUGIN_ROOT}/scripts/x.py` therefore runs as `/scripts/x.py`.

### Replacement

`"${<PLUGIN>_ROOT:?<msg>}/scripts/x.py"`, where the variable name comes from
`plugin_root_env_var_name` in `plugins/bootstrap/bootstrap_lib/env_var_check.py`.
The engine exports it at `plugins/bootstrap/bootstrap_lib/engine.py` line 1942
via `export_env_var`, which calls `session_env.record` and appends to
`$CLAUDE_ENV_FILE`.

That export sits BELOW both SessionStart skip gates. The interpreter names are
written ABOVE them (`plugins/bootstrap/hooks/sessionstart/session-bootstrap.sh`
lines 134-137). A gate-skipped session therefore has the interpreter names but
lacks the root variable, and the `:?` guard fails loudly. That loud failure is
the intended behaviour; the alternative is a silent run of `/scripts/x.py`.

### Rejected anchors

- `CLAUDE_PLUGIN_DATA`: set, but it held ANOTHER plugin's data dir
  (`codex-openai-codex`) when measured, so it cannot locate this plugin.
- Data-dir sync: the data dir is not a general anchor for a shipped script.
  `sync_to_data` is null for git-kit, skills-kit, awesome-kit, hue-kit and
  cache-kit, and unreal-kit syncs only `lib`. claude-ui-kit is the one plugin
  that syncs `scripts` (`plugins/claude-ui-kit/bootstrap.json` declares
  `sync_to_data` with src `scripts`, dst `scripts`), so its data dir does hold
  `install_statusline.py` and `statusline.sh`; the same path under a plugin
  with no such entry does not exist. A repo-wide
  `grep -rn '"sync_to_data"' plugins/*/bootstrap.json` returns exactly those
  two plugins. What the data dir anchors for every plugin is a venv
  interpreter, not a script. Same wording as the gotcha in the root
  `CLAUDE.md` insight `claude_plugin_root_not_in_bash`.
- `bin/` on PATH: only hue-kit, job-kit, llm-scripting-kit and secrets-kit
  ship a `bin/` dir whose versioned path is on the session PATH.

## Classes

### WORKING (harness-expanded, no change needed)

| File | Lines |
|------|-------|
| `plugins/bootstrap/hooks/hooks.json` | 8, 19 |
| `plugins/bootstrap-stuck-fix/hooks/hooks.json` | 8 |
| `plugins/unreal-kit/hooks/hooks.json` | 8, 19 |

### WORKING-BY-DEFENCE (do not change)

`plugins/bootstrap-stuck-fix/hooks/sessionstart/repair-registry.sh` line 26 is
the one site that handles the unset case:

    SCRIPTS="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}/scripts"

It is the pattern to copy inside a script that knows its own location. It does
NOT generalise to prose a reader types, which has no `$0` or `BASH_SOURCE`.

### DOC (about 45 hits, correct as written, do not change)

- `plugins/bootstrap/bootstrap_lib/`: `env_var_check.py:61`, `fix_queue.py:441`,
  `harvest.py:222`, `tool_paths.py:90`.
- `plugins/bootstrap/skills/bootstrap/`: `references/manifest-reference.md:205`,
  `references/plugin-reload-lifecycle.md:16,68,84,152`,
  `references/python-interpreter.md:66`, `SKILL.md:519,632`.
- `plugins/CLAUDE.md:560,587`.
- skills-kit code that STRIPS the prefix: `skills_kit_lib/audit.py:686,835`,
  `skills_kit_lib/CLAUDE.md:168`.
- md-domain: `references/provenance/*.md` (6 hits, used as a doc coordinate),
  `references/audit-framework.md:199`, `references/lanes/coverage-lane.md:97`,
  `skill-domain/framework.md:11,396`. (`references/lanes/audit-lane.md:17` was
  a DOC hit in the survey; it is a `SKILLS_KIT_ROOT` site and belongs to the
  migrated class.)
- git-kit and p4-kit `references/md-domain-review.md:74` (says explicitly it is
  NOT the right path).
- `plugins/workflow-kit/examples/node-strategies.example.js:11`,
  `plugins/llm-scripting-kit/README.md:326`.
- unreal-kit `skills/ue-python-api/references/architecture.md:48`,
  `project-setup.md:19`.
- The generator `scripts/gen_code_review_skills.py:1138` emits a DOC-class
  mention; it is fine.

### MIGRATED -- GENERATED (29 lines from 4 constants)

Source: `scripts/gen_code_review_skills.py`, constants `PREPARE_LAUNCHER` (:64),
`LANE_LAUNCHER` (:77), `PARSE_LAUNCHER` (:78), `RENDER_LAUNCHER` (:79). They
interpolated `${CLAUDE_PLUGIN_ROOT}` into templates at lines 1085, 1092, 1131,
1141, 1217, 1509, 1541, 1567, 1568, 1581, 2080, 2090, 2441, 2442, 2447, 2448.

Emitted sites (survey locations):

- `plugins/git-kit/skills/git-code-review/SKILL.md`: 87, 106, 114, 198, 353,
  360, 479, 623
- `plugins/p4-kit/skills/p4-code-review/SKILL.md`: 73, 96, 104, 188, 343, 350,
  482, 504, 657
- `plugins/git-kit/skills/git-code-review/references/configuration.md` and the
  p4-kit twin: 11, 182, 208, 320
- `plugins/git-kit/skills/git-code-review/references/declined-ledger.md` and the
  p4-kit twin: 60

Every file carries an `@generated by scripts/gen_code_review_skills.py` banner
and is pinned byte-for-byte by `tests/bootstrap/code_review/test_skill_drift.py`.
The fix was made in the generator and the files regenerated; an edit to an
artifact is overwritten. This is the durable lesson of the class: one generator
constant was 29 emitted lines, so the migration of git-kit and p4-kit was one
generator edit plus regeneration.

### MIGRATED -- HAND-WRITTEN (about 100 lines)

Survey locations:

- awesome-kit: `skills/orchestrate/SKILL.md:74`, `references/codex-dispatch.md:298`
  (under orchestrate), `skills/task/SKILL.md:55`, `skills/plugin-ecosystem/SKILL.md:74`
- cache-kit `skills/cache-report/SKILL.md`: 46, 49, 55, 66, 68, 70, 72. Line 55
  is a gotcha that explains the problem and line 72 tells the reader to
  substitute the install path; both are descriptive. The `!` preload line that
  was at 64 was deleted.
- content-pipeline-kit: `README.md:122`, `scripts/check_consumer_contract.py:6`
- hue-kit: `skills/hue-domain/SKILL.md:388` (`scene-layers.py`, migrated to
  `"${HUE_KIT_ROOT:?...}/scripts/scene-layers.py"` because the `hue-kit` shim
  fronts only `hue_kit_cli.py`). The sites in `CLAUDE.md` and at the former
  SKILL.md 383 were migrated to the bare `hue-kit` shim.
- llm-scripting-kit: `CLAUDE.md:13`, `skills/openrouter-account/SKILL.md:84`
- pdf-kit: `skills/html-pdf/SKILL.md:29,74,81`
- skills-kit: `CLAUDE.md:230,234`, `scripts/skills_kit_tool.py:7`,
  `scripts/print_version.py` (derives from `Path(__file__)` and fails loudly;
  remaining hits are comments)
- skills-kit `skills/knowledge-encoding/SKILL.md:70,74` (Read-tool paths, not exec)
- skills-kit `skills/md-domain/SKILL.md:757,764,771,778,784,787,790,793`
- skills-kit md-domain references: `lanes/audit-lane.md:17,117,142,190,374,507`
  (survey numbers; the sites sit up to 2 lines lower in the tree and read
  `SKILLS_KIT_ROOT`), `lanes/generation-lane.md:469`, `lanes/render-lane.md:42`,
  `skill-domain/report-usage.md:23,30`, `skill-domain/scripts.md:101`,
  `skill-domain/example-verification.md:16,17`
- skills-kit `skills/update-documentation/SKILL.md:42,151,152`
- unreal-kit: `README.md:107`, `custom_bootstrap.py:192`,
  `scripts/search_unreal_stub.py:67`, `lib/ue_runner_config.py:31`,
  `skills/ue-python-api/scripts/ue_runner.py:9,10,11,12,52`
- unreal-kit `skills/fix-up-redirectors/SKILL.md`: 187, 205, 206, 221, 287, 308,
  319, 320, 328, 329, 337, 338, and 431 (`os.environ['CLAUDE_PLUGIN_ROOT']`
  raised KeyError)
- unreal-kit `skills/ue-python-api/`: `SKILL.md:58,68`,
  `references/bootstrapped-setup.md:42,55,60`, `references/project-setup.md:51`,
  `references/script-bootstrap.md:58`, `references/script-execution.md:10`
- workflow-kit `skills/workflow-kit/references/workflow-yaml.md`: 15, 32, 36,
  45, 70, 80, 85, 136

## Sites migrated per plugin (survey counts, approximate)

| Plugin | Lines |
|--------|-------|
| skills-kit | 27 |
| unreal-kit | 26 |
| p4-kit | 14 |
| git-kit | 13 |
| workflow-kit | 8 |
| cache-kit | 7 |
| awesome-kit | 4 |
| hue-kit | 4 |
| pdf-kit | 3 |
| content-pipeline-kit | 2 |
| llm-scripting-kit | 2 |

## Latency evidence

All 44 recorded review bundles under the git-kit reviews data dir show only
native Agent lanes. The `run_review_lane.py` path has therefore never executed
from a skill launcher, and it fails loudly (exit 2) with the variable unset.

## Verification status of the line numbers

Verified against the tree on the survey date, before migration: the hooks.json
lines, the `repair-registry.sh` line, the four generator constants, the engine
export line and the hook-script interpreter block, and every site in cache-kit,
hue-kit, print_version.py, the git-kit SKILL.md and configuration.md, plus
per-file line lists for pdf-kit, awesome-kit (task, orchestrate,
plugin-ecosystem), llm-scripting-kit, skills-kit CLAUDE.md, md-domain SKILL.md,
update-documentation, fix-up-redirectors, ue-python-api SKILL.md,
workflow-yaml.md, and the p4-kit SKILL.md and declined-ledger.md.

Taken on trust from the survey, not re-grepped: the DOC class, the remaining
md-domain reference files, the unreal-kit references and scripts, the
content-pipeline-kit sites, and the awesome-kit `codex-dispatch.md:298`.

Discrepancy found on re-check: p4-kit `SKILL.md` also carries hits at 80, 558
and 561 that the survey did not list; they appear to be prose mentions rather
than launcher lines.
