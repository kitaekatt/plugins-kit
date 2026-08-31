# Codex endpoint compatibility

Use this document when deciding whether Codex can drive a custom
OpenAI-compatible endpoint. It covers the wire protocol, tool schema, and
authentication behavior. For launch flags, command shape, and dispatch-time
failure traps, see [Codex dispatch mechanics](../../plugins/awesome-kit/skills/orchestrate/references/codex-dispatch.md).

## The compatibility predicate

Codex can drive endpoint X only when a live `codex exec` run against X passes
both checks:

- X serves `/v1/responses`.
- X accepts the tool schema Codex sends.

"OpenAI-compatible" is too broad, and `/v1/responses` alone is necessary but
not sufficient. Authentication and provider selection can succeed while the
first tool-bearing request fails. A live run is the deciding test.

## Responses wire compatibility

Codex speaks the Responses API only. `wire_api = "chat"` was removed from
Codex on 2026-02-05 (upstream discussion #7782); its enum is Responses-only
and passing `"chat"` is a hard configuration error, not a fallback. The
built-in Ollama and LM Studio providers also moved to Responses.

Do not infer that Codex cannot drive a Chat Completions server from that
removal. A server-side conversion layer can translate Responses requests into
Chat Completions. llama.cpp ships such a layer; a live run produced Responses
SSE events including `response.created`, `response.output_item.added`, and
`response.reasoning_text.delta`, with `function_call` items carrying
`call_id`. Test the endpoint instead of reasoning from the client enum.

## Tool-schema compatibility

A vendor endpoint can serve `/v1/responses` and return 200 for a real
Responses completion, yet reject Codex's request with 422. In the observed xAI
case, the server accepted a real `grok-4.6` Responses request but rejected
Codex's core `type: "namespace"` tool as an unknown variant. Authentication
and provider selection had already succeeded. Disabling MCP servers and eleven
feature flags only changed the tool index; it did not remove the core tool.
No consumer-side setting fixes a server that rejects that schema.

Therefore the predicate is "serves `/v1/responses` AND accepts Codex's tool
schema", not "is OpenAI-compatible" and not "serves `/v1/responses`". Only a
live `codex exec` run answers the second half. llama.cpp passed the full
predicate, including `function_call` round-trips; the observed xAI endpoint
did not.

## Keyless endpoints

Keyless is native. `ModelProviderInfo.env_key` is `Option<String>` and
`requires_openai_auth` defaults to false. Omitting `env_key` sends no
Authorization header, matching how built-in OSS providers are configured. Do
not fabricate a dummy key for a keyless server.
