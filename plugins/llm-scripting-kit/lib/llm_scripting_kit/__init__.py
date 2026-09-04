"""llm_scripting_kit -- shared OpenRouter API key resolution and account validation.

Consumers (loc-ops, future tooling) import from here:

    from llm_scripting_kit import get_api_key, make_openai_client, check_account
    from llm_scripting_kit import BASE_URL, API_KEY_ENV

Stdlib-only by default. ``make_openai_client`` lazy-imports the ``openai`` SDK
so callers that only need the raw key (or use a different HTTP client) do not
pay the SDK install cost.

The completion seam (``llm_scripting_kit.completion``) adds one ``complete()``
protocol over four transports -- an OpenAI-compatible HTTP endpoint
(:class:`OpenRouterBackend`) and three local CLIs, ``claude -p``
(:class:`ClaudeCliBackend`), ``codex exec`` (:class:`CodexCliBackend`), and
``opencode run`` (:class:`OpencodeCliBackend`) -- so a pipeline can run the
same task on any of them purely by configuration. Its seam types and the CLI
runner are stdlib-only; only the OpenRouter transport reaches for ``openai``,
lazily, and only the codex transport reaches for ``bootstrap_lib``, also
lazily.

Named endpoints come from the layered ``config.yaml`` and from the
model-endpoints registry (``llm_scripting_kit.model_endpoints``) -- a file the
user owns at ``~/.claude/config/model-endpoints.yaml``, whose entry ids resolve
as endpoint names. Endpoints may be keyless (``key_env: null``), which is the
norm for a locally hosted OpenAI-compatible server.
"""

from .constants import API_KEY_ENV, BASE_URL, USER_ENV_FILE, project_env_file
from .api_key import get_api_key, KeyLookupResult
from .account import (
    check_account,
    check_models_probe,
    probe_endpoint,
    validate_endpoint,
    AccountStatus,
    AccountCheckError,
    EndpointProbe,
)
from .client import KEYLESS_API_KEY, make_openai_client
from .env_file import read_env_file, write_env_file
from .model_endpoints import (
    EndpointEntry,
    EndpointMetadataError,
    EndpointRegistry,
    EndpointRegistryError,
    HARNESS_KIND,
    REGISTRY_ENV,
    TRANSPORT_KIND,
    default_registry_path,
    load_endpoint_registry,
    resolve_registry_entry,
)
from .seats import (
    Seat,
    SeatResolutionError,
    SeatSelf,
    SeatsResult,
    UnclassifiedEntry,
    discover_seats,
)
from .reachability import (
    DEFAULT_VERIFY_TIMEOUT_S,
    STATUS_REACHABLE,
    STATUS_UNKNOWN,
    STATUS_UNREACHABLE,
    Reachability,
    check_entry,
    check_harness,
    check_many,
    check_transport,
)
from .harness_adapters import (
    CODEX_EFFORT_MENU,
    CodexAdapter,
    HarnessAdapter,
    HarnessAdapterError,
    HarnessInvocation,
    OpencodeAdapter,
    resolve_harness_adapter,
)
from .models import (
    DEFAULT_ENDPOINT_NAME,
    DEFAULT_MODEL_CONFIG,
    EndpointResolveError,
    ModelDiscovery,
    ModelResolveError,
    default_endpoint_name,
    discover_model_entries,
    load_model_config,
    resolve_endpoint,
    resolve_model,
)
from .completion import (
    AgentTimeoutError,
    BackendOptions,
    ClaudeCliBackend,
    CodexCliBackend,
    CodexRunError,
    EmptyCompletionError,
    OpencodeCliBackend,
    OpencodeRunError,
    HALT_AUTH,
    HALT_INSUFFICIENT_CREDIT,
    HALT_RATE_LIMIT,
    HaltError,
    LLMBackend,
    LLMResponse,
    OpenRouterBackend,
    classify_claude_exception,
    classify_codex_exception,
    classify_codex_text,
    classify_halt_text,
    classify_opencode_exception,
    classify_openai_exception,
    match_capabilities,
)

__all__ = [
    "API_KEY_ENV",
    "BASE_URL",
    "USER_ENV_FILE",
    "project_env_file",
    "get_api_key",
    "KeyLookupResult",
    "check_account",
    "check_models_probe",
    "probe_endpoint",
    "validate_endpoint",
    "AccountStatus",
    "AccountCheckError",
    "EndpointProbe",
    # reachability (verification, distinct from configuration -- never an LLM call)
    "DEFAULT_VERIFY_TIMEOUT_S",
    "STATUS_REACHABLE",
    "STATUS_UNREACHABLE",
    "STATUS_UNKNOWN",
    "Reachability",
    "check_entry",
    "check_harness",
    "check_many",
    "check_transport",
    "make_openai_client",
    "KEYLESS_API_KEY",
    "read_env_file",
    "write_env_file",
    "DEFAULT_ENDPOINT_NAME",
    "DEFAULT_MODEL_CONFIG",
    "EndpointResolveError",
    "ModelDiscovery",
    "ModelResolveError",
    "default_endpoint_name",
    "discover_model_entries",
    "load_model_config",
    "resolve_endpoint",
    "resolve_model",
    # model-endpoints registry
    "REGISTRY_ENV",
    "TRANSPORT_KIND",
    "HARNESS_KIND",
    "EndpointEntry",
    "EndpointMetadataError",
    "EndpointRegistry",
    "EndpointRegistryError",
    "default_registry_path",
    "load_endpoint_registry",
    "resolve_registry_entry",
    # frontier seat discovery
    "Seat",
    "SeatResolutionError",
    "SeatSelf",
    "SeatsResult",
    "UnclassifiedEntry",
    "discover_seats",
    # harness adapters
    "CODEX_EFFORT_MENU",
    "HarnessAdapter",
    "HarnessAdapterError",
    "HarnessInvocation",
    "CodexAdapter",
    "OpencodeAdapter",
    "resolve_harness_adapter",
    # completion seam
    "LLMResponse",
    "EmptyCompletionError",
    "BackendOptions",
    "LLMBackend",
    "OpenRouterBackend",
    "ClaudeCliBackend",
    "CodexCliBackend",
    "CodexRunError",
    "OpencodeCliBackend",
    "OpencodeRunError",
    "HaltError",
    "HALT_AUTH",
    "HALT_RATE_LIMIT",
    "HALT_INSUFFICIENT_CREDIT",
    "classify_halt_text",
    "classify_openai_exception",
    "classify_claude_exception",
    "classify_codex_text",
    "classify_codex_exception",
    "classify_opencode_exception",
    "AgentTimeoutError",
    "match_capabilities",
]
