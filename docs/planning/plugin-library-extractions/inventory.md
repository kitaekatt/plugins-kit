# Library extraction inventory (2026-10-23)

This records a read-only survey of the plugins-kit source tree on 2026-10-23.
Counts and coupling estimates describe that survey; recheck them before extraction.
The follow-on task is `dev/tasks/plugin-library-extractions`.

Read-only survey (sonnet, Explore). Line counts are non-test .py/.sh; coupling is grep-based.
Direction: thin plugins over general katsoft-* libraries; Claude-specific integration stays in plugins.

## Stays in plugins (Claude-plugin-specific)
bootstrap_lib: engine (10.3k), harvest, proc_lock, marketplace_lifecycle, registry_repair,
plugins_snapshot, plugin_resolve, plugin_tiers, self_register, session_env, claudx, codex_hook,
shell_hook, settings_writable, display_relay, fix_queue/fix_runner, queue_records, stamps,
messages, skill_material, agent_skills_check, instruction_files, config*/profiles, env_manifest,
manifest_lint, shared_lib (deprecation target once libs ship from PyPI).
Plugins: presence-kit, awesome-kit (orchestrate, poster), relaunch-kit, claude-ui-kit,
bootstrap-stuck-fix, llm-kit plugin (already thin), cache-kit (transcript data), skills-kit (SKILL.md/CLAUDE.md auditing).

## Candidates (general functionality)
| Source | Lines | Target | Effort |
|---|---|---|---|
| bootstrap_lib/code_review/ + git/p4 prepare_review glue (duplicated helpers) | 5.9k + 3.7k | katsoft-review (VCS-agnostic engine; git/p4 adapters) | medium-high; highest value |
| task-kit task_system/ (only harness.py and parts of sessions are harness-coupled) | ~14k | katsoft-tasks | medium; high thinning value |
| secrets-kit lib/secrets_kit | ~5k | katsoft-secrets | medium; security review before public |
| workflow-kit workflow_kit_lib (+ runners -> llm-kit cli) | ~1.8k | katsoft-workflow | medium |
| bootstrap_lib tool/pkg-manager/PATH/venv checks + utilities | ~6.7k | katsoft-envkit (leaf modules first) | medium-high (manifest coupling) |
| hue-kit scripts | ~2.7k | katsoft-hue | low-medium; niche |
| unreal-kit ue_* + p4cli; p4-kit p4kit_vcs | ~4k + 0.5k | katsoft-unreal, katsoft-vcs | medium; niche |
| pdf-kit html_to_pdf | 0.3k | katsoft-pdf or a shared tools package | low |
| bootstrap_lib/codex.py | 0.35k | katsoft-llm-kit (owned by the initial packaging task) | low |
| bootstrap_guard.py (about 8 vendored copies) | 0.24k each | single source | low |

## Proposed order
1. Dedupe: bootstrap_guard copies; consumers import katsoft_contracts directly (then retire the aliases).
2. katsoft-review. 3. katsoft-tasks. 4. katsoft-secrets (after security review). 5. katsoft-workflow.
6. katsoft-envkit, leaf modules first. 7. Niche: hue, unreal, pdf, vcs.

## Open questions

The follow-on task plan owns the live work states for these decisions.
- Q1 code_review instruction-file names pluggable (CLAUDE.md / AGENTS.md / none)? Decided: yes (no-harness ruling).
- Q2 one package per small utility vs a shared katsoft-tools? USER.
- Q3 skills-kit audit general or harness-specific? Decided: stays (it audits Claude artifacts).
- Q4 managed_state/: not opened; decide at envkit time.
- Q5 delete shared_lib.py once libs ship? USER (deletion).
- Q6 profiles/manifest layering general? Decide at envkit time.
