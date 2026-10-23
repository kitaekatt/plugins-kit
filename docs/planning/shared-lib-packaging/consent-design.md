# Consent design for katsoft-llm-kit

Status: design record, 2026-10-23. Advisor ruling: Sol, "APPROVE WITH AMENDMENTS"
(cited below as "Sol amendment N"). Supersedes `docs/planning/llm-kit/cost-class-consent-plan.md`.

## Durable principle

Consent governs caller-selected execution policy. The library does not make LLM
calls; the user of the library makes them. Writing code that invokes models needs
no consent. Codex and Claude models are never blocked.

## 1. User rulings (verbatim, 2026-10-23)

- "the library does not make any llm calls, the user of the library makes the llm calls, we don't need the user to consent to writing code that makes llm calls" / "this principle should be durable"
- "i think we want to provide a default consent experience, plus the flags we think people would like to turn on, but that consent experience itself is optional. You can build your own consent layer, or use ours and work within the config variables we provide. That is the library policy. With the orchestrate skill, I think it would set its own policy. Databench has a globally configured policy."
- "i think we want to try and do some unification here versus accepting everything's individuality as the best design"
- "(5) off by default makes sense, but I want to be cognizent of how do users discover a config layer exists."
- "we should use local as the term instead of free or maybe endpoints" / "might as well name it well"
- "(8) you should be able to override it if you want to like say \"for this usage is disabled\". Essentially you can restrict at the thing as well." / "we should provide how to do that"
- 2026-10-23: "codex and claude models shouldn't be blocked." Library goal: "conceptually we want the libraries to be usable by someone who isn't using an AI harness. ... claude usage isn't required to use the libraries."

## 2. Design (amended form)

1. **Classification is information, always reported.** Every catalog row, describe
   entry and protocol 2 response carries the cost class plus the reason for it,
   whether or not the consent layer is active. Consent activation is reported
   separately, as structured JSON fields. (Sol amendment 9)
2. **The consent layer is optional and off by default.** No call path refuses
   unless the caller opts in: protocol 2 request field `consent`, CLI
   `--consent [--consent-scope X]`, API `consent.policy(scope=...)`. The
   unconditional refusal in `dispatch/invoke.py` step 6 and `cost.allowed` in
   `registry/catalog.py` are removed. An explicit per-call `--allow` is
   caller-owned policy and replaces the layer ("build your own"). Protocol 2
   shape: omitted or `false` = disabled; `true` = global policy;
   `{"scope": "<name>"}` = scoped policy; scope names are validated nonempty,
   malformed objects are rejected, and supplying both `consent` and an explicit
   allow is rejected. With consent disabled, configuration problems never become
   refusals. (Sol amendment 5)
3. **Class names: `local` / `subscription` / `metered`.** Renamed from
   free/usage/spend, with no aliases (the user is the only user). `metered` is the
   conservative category for paid or unverified billing; it does not imply a charge
   occurred. `local` needs explicit registry ownership metadata, because
   `billing.mode: unmetered` states marginal cost, not ownership; a URL, LAN
   address or model name cannot establish ownership. An unmetered remote endpoint
   (a provider free tier) classifies `metered`. Classification considers every
   front-door tier of a route. (Sol amendment 1)
4. **Default policy when opted in:** local on, subscription on, metered off. With no
   consent file the caller receives that default with provenance `default`; nothing
   is created. A missing file is distinct from a malformed or unreadable one, which
   raises a clear configuration error when opted in. (Sol amendment 4)
5. **One file, `~/.config/llm-kit/consent.json`:** global classes plus
   `"scopes": {name: {class: bool}}`. Global is a ceiling; scopes are restrict-only;
   an absent scope key means inherit: `effective = global AND scope.get(class, true)`.
   Global changes preserve scope entries. A scoped `on` blocked globally is displayed
   as blocked and names the global command needed; it never widens global policy.
   CLI: `llm-kit consent set [--scope X] --local|--subscription|--metered on|off`
   and `consent show [--scope X]` (effective policy and source of each value).
   (Sol amendment 2)
6. **Unification:** orchestrate applies scope `orchestrate`; the task-kit answer
   chain applies scope `task-kit` and no longer unions in `free`/`local`
   unconditionally (a disabled `local` class is respected, including in phase one);
   databench reads and writes scope `databench` through the library API.
   (Sol amendment 8 for task-kit)
7. **Discovery:** the cost class is always shown; catalog, describe and
   `consent show` print one line on whether the layer is in use and how to enable
   it; a model dropped by the layer names its class and the exact flag; the package
   README has a "Consent" section; `llm-kit --help` lists consent.
8. **No implicit OpenRouter selection.** Fix the source: `default_declaration()`
   (reached from `plugins/workflow-kit/scripts/openrouter_run.py`) supplies a shipped
   OpenRouter default, and the pipeline runtime falls back to a bare
   `OpenRouterBackend()` when no model declaration exists. Both fail loudly,
   naming how to declare a model. Explicitly supplied backends and deliberately
   configured default declarations are preserved. (Sol amendment 3)
9. **A harness takes the class of the endpoint it runs on.** OpenCode registry
   entries hold opaque model strings, so each needs an explicit harness-to-endpoint
   reference before classification can inherit. (Sol amendment 1)

Further amendments: orchestrate's renderer reconstructs entries without billing,
front-door or routing metadata and synthesizes Claude core entries, so it must
preserve classification fields, supply authentication context, define disclosed
behavior when the library is unavailable, and classify native Agent-tool execution
by its actual authentication context (Sol amendment 7). Databench migration maps
`usage` to `subscription` and `spend` to `metered`, preserves regeneration and
publishing settings, lets existing destination scope values win on rerun, and
reports (never silently drops) a legacy `spend: true` that conflicts with global
`metered: false` (Sol amendment 6). Databench has no `generation.json` on this
machine, so no conflict arises here.

## 3. Implementation units (dependency order)

| Unit | Scope | Owns |
|---|---|---|
| L1 | Library: rename classes; always-reported classification with reason; optional layer; scopes; protocol 2 `consent` field; CLI; missing-file default; remove the unconditional refusal and `cost.allowed` | `packages/katsoft-llm-kit/src/llm_kit/registry/{cost,consent,catalog}.py`, `cli/consent.py`, `dispatch/invoke.py`, tests/llm-kit goldens |
| L2 | Harness-to-endpoint classification: registry metadata for OpenCode harnesses; ownership metadata for `local` | registry data and `registry/cost.py` callers |
| L3 | Remove implicit OpenRouter default (shipped default in `default_config.yaml` / llm_kit equivalent; pipeline bare `OpenRouterBackend()` fallback) | `registry/declaration`, `pipeline/runtime/backends.py`, workflow-kit script |
| L4 | Orchestrate renderer applies scope `orchestrate`, preserves classification fields | `plugins/awesome-kit/skills/orchestrate/scripts/orchestration_guidance.py` |
| L5 | Task-kit answer chain on scope `task-kit`; drop unconditional free/local union | `plugins/task-kit/skills/task/scripts/task_system/answer_chain.py` |
| L6 | Databench migration to scope `databench`; conflict reporting | databench repo (`generation.py`, `llm_gateway.py`, settings page) |
| L7 | Docs: plugins/llm-kit CLAUDE.md, README, `references/llm-access.md`, package README "Consent" section; mark `docs/planning/llm-kit/cost-class-consent-plan.md` superseded | the named docs |

Order: L1 first; L2 and L3 after L1 (independent of each other); L4, L5, L6 after L1
and L2; L7 last.

**L1 gate:** done (user approval 2026-10-23). The user approved removing the unconditional refusal; only opted-in requests are checked.
