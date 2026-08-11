# Plugin improvements -- candidate work

Deficiencies found in `llm-scripting-kit` and `content-pipeline-kit` while
writing an architecture document set over them. Documenting a system is a way
to find its problems: a passage that resists being written accurately is
usually evidence of a real defect, not a writing difficulty.

Each entry states the observed behavior, why it is a problem, and a candidate
fix. Nothing here is scheduled; this is a staging area for triage.

## 1. Unsupported `BackendOptions` fields are silently ignored

`BackendOptions` is a union of transport-specific knobs, and a backend that
does not understand a field simply drops it. `temperature` and `max_tokens`
are accepted and then ignored by both CLI backends
(`completion/backends.py`, `completion/codex_backend.py`); `user_cache_prefix`
is OpenRouter-only; `allowed_tools` is claude-cli-only.

A caller who sets `temperature=0` and routes to `claude-cli` gets no error, no
warning, and no effect -- the run completes and the setting was never applied.
The signal does not match the cause.

**Candidate fix.** Warn once when a backend receives a populated option it does
not support, naming the field and the backend. A warning rather than an error:
routing is process-global, so a caller may legitimately construct one options
object and send it to several transports. Rejecting at construction would break
that pattern; staying silent hides a real misconfiguration.

## 2. Model ids are not portable, and nothing in the library translates them

An OpenRouter slug is meaningless to `claude -p`; codex requires fully-qualified
ids. Choosing a backend is therefore really choosing a backend AND a model id,
which undercuts the "write the prompt pair once, choose the executor separately"
seam the package otherwise provides.

`content_pipeline.llm.backends.routed_model()` exists to compensate. That a
consumer had to build this is the evidence: a genuinely uniform seam would not
need it.

**Candidate fix.** Let the alias registry own per-transport resolution, so an
alias resolves to the right concrete id for whichever backend is active.

## 3. Two distinct `HaltError` classes share one name

`content_pipeline/llm/platform.py` defines its own `HaltError` and
`classify_halt_text` with no import from `llm_scripting_kit`, so
`llm_scripting_kit.HaltError` and `content_pipeline.llm.platform.HaltError` are
different classes with the same name, in a dependency chain where
content-pipeline-kit depends on llm-scripting-kit.

Latent today: llm-scripting-kit never raises its own, so the two never meet. But
a consumer who writes `from llm_scripting_kit import HaltError` and wraps a
`content_pipeline` call in `except HaltError` gets a handler that silently never
fires. Nothing errors; the halt is simply not caught.

**Candidate fix.** content-pipeline-kit re-exports llm-scripting-kit's taxonomy
rather than redefining it.

## 4. Codex rate-limit halt markers are unverified

`completion/halt.py` self-documents the 429 markers as guessed -- "the 429 forms
mirror the 401 shapes and are UNVERIFIED ... Replace them with observed text
when one is seen". The 401 markers WERE verified live, by pointing `CODEX_HOME`
at an empty directory and reading the emitted strings.

So auth halts on the codex transport are trustworthy and rate-limit halts are
not. A bulk run could keep spending against a persistent 429 that never
classifies.

**Candidate fix.** Provoke a real 429 and replace the guessed strings, the same
way the 401 markers were confirmed.

## 5. `LLMResponse.from_cache` is never set

All three backends hardcode `from_cache=False` (`completion/types.py`). The
field is reserved for a caching layer above, which is a reasonable design, but
it reads as implemented when nothing populates it.

**Candidate fix.** Document it as caller-populated at the definition site, or
drop it and let the caching layer carry its own flag.

## 6. `README.md` understates the completion seam

`README.md` describes TWO transports and omits `CodexCliBackend` from the
completion-seam section entirely. It also presents the credential layer as
universal, when key resolution, the `openrouter-account` skill, and the CLI are
OpenRouter/HTTP-only -- the two CLI transports authenticate through their own
logins and never call `get_api_key`.

**Candidate fix.** Correct the count, and scope the credential prose to the HTTP
transport.

## 7. Two path helpers are not re-exported from the package root

`legacy_project_env_file` and `project_env_files` are defined in
`llm_scripting_kit/constants.py` and importable from there, but
`llm_scripting_kit/__init__.py` imports and lists only `project_env_file`
(singular) in `__all__`.

A caller following the package's own convention -- import from the root --
finds one of the three and has to discover that the other two live one module
down. Minor, but it is an inconsistency in the public surface.

**Candidate fix.** Re-export both from the package root, or document that
`constants` is the intended import site for the path helpers.

## 8. `stale_editable_self_install` remains open

Recorded in the root `CLAUDE.md` knowledge base and marked unfixed: a plugin's
own editable-install `.pth` never re-points on a version bump, so a plugin can
silently run old code from its own package. The shared-lib variant of this was
fixed; the self-install variant was not.

Listed here because it can make any behavioral claim about these plugins
unreproducible on a machine in that state -- including the claims in the
architecture document set.
