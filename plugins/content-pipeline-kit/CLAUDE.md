# CLAUDE.md -- content-pipeline-kit plugin

Guidance for an AI agent working in this plugin. content-pipeline-kit ships the
`content_pipeline` library that sits behind LLM-in-the-loop batch content
pipelines. `skills/content-pipeline-domain/SKILL.md` owns the vocabulary and the
abstraction map; `references/design-discipline.md` beside it carries the
philosophy behind the opt-in guardrails.

## The dependency on llm-scripting-kit is one-way by design

`content_pipeline` depends on `llm_scripting_kit`; the reverse edge does not
exist and should not be added. llm-scripting-kit owns making ONE call correctly
-- endpoint, model, key, transport, halt taxonomy. This plugin owns what only
exists across a RUN of calls: the content-addressed response cache, run-level
retry, cost accounting, the token/cost budget guard, and the validate-until-valid
submission loop. Each of those is a policy a pipeline can answer and a transport
cannot. They live in `lib/content_pipeline/llm/platform.py`, whose module
docstring states the same split; `llm/backends.py` holds the thin adapters over
the shared lib and imports it lazily, so `MockBackend` needs neither the shared
lib nor an SDK.

So the lower layer not implementing these is the boundary working, not a gap a
consumer is left to fill. A consumer wanting pipeline-grade behaviour depends on
this plugin and gets them; a consumer wanting one call depends on
llm-scripting-kit alone and carries none of the weight. When adding a concern,
keep it on this side of the line unless it is a fact about making a call rather
than about running many.

`openai` is declared in this plugin's own `pyproject.toml` even though the SDK is
reached through the shared lib -- shared libs share source, not dependencies. See
`plugins/CLAUDE.md`, "Why shared libs rather than published packages".
