# Orchestrate / Codex boundaries

Standing rationale behind two operative one-line rules in the root
`CLAUDE.md` insights `orchestrate_yaml_capabilities_are_not_a_duplicate` and
`codex_dispatch_is_silent_on_failure`. Read it before re-proposing either
design; the observation dates live in those insights' `origin` fields.

## orchestration.yaml capabilities vs the llm-scripting-kit advertisement

The two look like one fact stated twice and are not. orchestration.yaml
describes a `codex exec` command the ORCHESTRATOR types and runs itself,
rendered through CodexAdapter.build_argv; CODEX_CAPABILITIES describes what
CodexCliBackend emits through the COMPLETION SEAM. They are siblings
converging only at bootstrap_lib.codex.build_codex_exec_argv, which is the
single source of the argv CONSTRUCTION CODE. Scope that precisely: the
construction is deduplicated, the argv SPELLINGS are not. Fourteen flags are
restated as string literals across up to five files (`codex exec`,
`-s workspace-write`, `windows.sandbox="unelevated"`,
`sandbox_workspace_write.network_access=true`, `-m`,
`model_reasoning_effort=`, `-C`, `--add-dir`, `-o`, `--output-schema`,
`--skip-git-repo-check`, `--color never`, `--json`, trailing `-`), and the
effort menu is stated at FOUR sites -- orchestration.yaml,
CODEX_EFFORT_MENU in harness_adapters.py, a prose note inside
CODEX_CAPABILITIES, and codex-dispatch.md. An earlier revision of this
insight said the genuine duplication was already deduplicated; that is too
strong and misled a follow-up task into expecting to find nothing. Most of
the YAML block is still not capability fact at all (unrestricted reads
outside -C, silent HTTP-000 egress, the TUI dying when backgrounded, where
concurrent writers land, judging by the -o file); references/codex-dispatch.md
owns those.

THE CONCRETE DAMAGE, which is why this is an insight and not a preference:
the YAML asserts the effort menu low|medium|high|xhigh|max and the
advertisement deliberately carries NO `values` for codex effort. Not a
contradiction -- CodexAdapter owns and VALIDATES that menu
(CODEX_EFFORT_MENU, harness_adapters.py, whose comment says the accepted menu
is a runtime contract rather than a transcription of one version's help
text), while CodexCliBackend BYPASSES that validator, which is exactly why
its advertisement advertises no menu. Deriving the rendered menu from the
advertisement deletes xhigh, a value verified against a live run and
carrying an explicit do-not-remove warning in two places.

Also load-bearing: backend capabilities in orchestration.yaml are
USER-OVERRIDABLE CONFIGURATION, so removing the seam must pass the
plugin-opinion razor, and plugins/CLAUDE.md bars relocating ownership across
a plugin boundary to remove apparent duplication. Separately, there is NO
capability fallback -- deleting the block and having discovery fail would
silently drop the safety summary.

SETTLED, SECOND TIME: a follow-up task investigated the harness-owned
display/dispatch contract on its own merits and returned DO-NOT-BUILD. The
deciding evidence: the ONE drift defect in this history (commit 7e4b18ab --
CodexAdapter dropped `--add-dir`, so a dispatched unit exited 0 having
written nothing) was orchestration.yaml <-> CodexAdapter, the DIRECT-DISPATCH
axis, NOT <-> CODEX_CAPABILITIES. A contract consuming the completion-seam
advertisement would not have prevented it. Its fix was already a test. The
plugin-opinion razor also fails for removing the config seam -- no serious,
and no two distinct, power-user scenarios could be named. A drift TEST
across the boundary remains the cheap alternative if the maintenance ever
bites; it was scoped and deliberately not built.

If the idea is ever revisited, it is a NEW design (a harness-owned
display/dispatch contract distinct from CODEX_CAPABILITIES, with explicit
fallback and precedence rules), not a retirement of a duplicate. A drift TEST
across the boundary is the cheap alternative worth evaluating first.

## Two paths to codex, and why a dispatch fails silently

A codex dispatch can fail silently at exit 0 -- judge it by its `-o` file,
never `$?`. Dispatch reference:
plugins/awesome-kit/skills/orchestrate/references/codex-dispatch.md.

ORCHESTRATE'S RELATIONSHIP TO llm-scripting-kit is easy to get backwards
because it differs by AXIS rather than being all-or-nothing. DISCOVERY:
orchestrate DOES consume llm-scripting-kit -- orchestration_guidance.py
imports it and calls discover_model_entries, and orchestration.yaml states
that every routing name without the `agent:` prefix resolves against it.
EXECUTION: orchestrate does NOT go through the completion seam
(CodexCliBackend), but it is NOT independent of bootstrap_lib.codex --
orchestration_guidance.py's adapter_command_text_provider resolves a harness
adapter via llm_scripting_kit.resolve_harness_adapter and calls
CodexAdapter.build_argv, which calls bootstrap_lib.codex.build_codex_exec_argv.
The literal `command:` string in orchestration.yaml's backends record is the
DISCLOSED FALLBACK (_command_fallback), reached only when llm_scripting_kit
is unavailable, version-skewed, or the adapter raises, and it appends a note
saying so. Do not mistake that degradation path for the mechanism.

So "orchestrate does not use llm-scripting-kit" is FALSE on both axes. What
stays true is narrower: a codex dispatch from orchestrate does not exercise
the COMPLETION SEAM, so a failure there implicates the argv, the sandbox, or
the caller's process handling -- not CodexCliBackend.

orchestration.yaml is hand-written config.
