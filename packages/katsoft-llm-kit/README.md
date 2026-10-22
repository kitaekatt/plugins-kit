# katsoft-llm-kit

LLM access, durable agent jobs and batch content pipelines in one library.
The import package is `llm_kit`; the console script is `llm-kit`.

```
pip install katsoft-llm-kit
pip install "katsoft-llm-kit[sdk,jobs,config]"
llm-kit --help
```

The base install depends on `katsoft-contracts` (execution-event envelope,
interrupt contract, model declaration parser) and nothing else. Extras:
`config`, `jobs` and `pipeline` bring PyYAML; `sdk` brings the OpenAI client
(the OpenRouter HTTP path); `local` brings psutil; `frontdoor` brings
FastAPI, uvicorn and httpx.

## Runtime mode

`llm_kit._runtime.mode()` reports how the library is deployed. This
distribution is `"package"`: the contracts come from `katsoft-contracts`, and
the features that need the plugin marketplace's `bootstrap_lib` (the Codex
adapter, claudx propagation, skill context, the code-review lane) raise
`UnsupportedInPackageMode`. The model registry reads the shipped defaults;
point `MODEL_ENDPOINTS_REGISTRY` at a registry file to change them.

Do not install this distribution into an environment that also holds the
plugin's linked `llm_kit` copy.

## Source

The source lives in the plugins-kit repository (`plugins/llm-kit/lib/llm_kit`).
Releases are tagged `katsoft-llm-kit-v<version>` in the repository named by the
`Source` project URL.
