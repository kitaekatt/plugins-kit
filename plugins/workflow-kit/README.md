# workflow-kit

**Experimental.** Published and listed in the marketplace, per `plugin.json` /
`marketplace.json`.

A kit of incremental, native-preserving improvements on top of Claude Code's
native Workflow tool. A human authors a durable workflow as a declarative
`*.workflow.yaml`; workflow-kit validates it and compiles it to a native
Workflow script. It never reimplements execution -- everything compiles to,
and runs on, the native tool.

It also ships node strategies (a script executor and an OpenRouter model-call
executor, run via a generic `workflow-kit-agent`) that fulfil a file-passing
contract: node outputs travel on disk via `$OUT`/`$STATUS` with shell
redirection, so payloads bypass the model context instead of flowing through
it. The OpenRouter node reuses llm-scripting-kit's client and model registry;
workflow-kit's own only Python dependency is pyyaml.

Tests live in `tests/workflow-kit/`. Try it locally with
`claude --plugin-dir plugins/workflow-kit`.
