"""llm_scripting_kit -- shared OpenRouter API key resolution and account validation.

Consumers (loc-ops, future tooling) import from here:

    from llm_scripting_kit import get_api_key, make_openai_client, check_account
    from llm_scripting_kit import BASE_URL, API_KEY_ENV

Stdlib-only by default. ``make_openai_client`` lazy-imports the ``openai`` SDK
so callers that only need the raw key (or use a different HTTP client) do not
pay the SDK install cost.

The completion seam (``llm_scripting_kit.completion``) adds one ``complete()``
protocol over two transports -- an OpenAI-compatible HTTP endpoint
(:class:`OpenRouterBackend`) and the local ``claude -p`` CLI, subscription
billed (:class:`ClaudeCliBackend`) -- so a pipeline can run the same task on
either purely by configuration. Its seam types and the ``claude -p`` runner are
stdlib-only; only the OpenRouter transport reaches for ``openai``, lazily.
"""

from .constants import API_KEY_ENV, BASE_URL, USER_ENV_FILE, project_env_file
from .api_key import get_api_key, KeyLookupResult
from .account import (
    check_account,
    check_models_probe,
    validate_endpoint,
    AccountStatus,
    AccountCheckError,
)
from .client import make_openai_client
from .env_file import read_env_file, write_env_file
from .models import (
    DEFAULT_ENDPOINT_NAME,
    DEFAULT_MODEL_CONFIG,
    EndpointResolveError,
    ModelResolveError,
    default_endpoint_name,
    load_model_config,
    resolve_endpoint,
    resolve_model,
)
from .completion import (
    AgentTimeoutError,
    BackendOptions,
    ClaudeCliBackend,
    HALT_AUTH,
    HALT_INSUFFICIENT_CREDIT,
    HALT_RATE_LIMIT,
    HaltError,
    LLMBackend,
    LLMResponse,
    OpenRouterBackend,
    classify_claude_exception,
    classify_halt_text,
    classify_openai_exception,
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
    "validate_endpoint",
    "AccountStatus",
    "AccountCheckError",
    "make_openai_client",
    "read_env_file",
    "write_env_file",
    "DEFAULT_ENDPOINT_NAME",
    "DEFAULT_MODEL_CONFIG",
    "EndpointResolveError",
    "ModelResolveError",
    "default_endpoint_name",
    "load_model_config",
    "resolve_endpoint",
    "resolve_model",
    # completion seam
    "LLMResponse",
    "BackendOptions",
    "LLMBackend",
    "OpenRouterBackend",
    "ClaudeCliBackend",
    "HaltError",
    "HALT_AUTH",
    "HALT_RATE_LIMIT",
    "HALT_INSUFFICIENT_CREDIT",
    "classify_halt_text",
    "classify_openai_exception",
    "classify_claude_exception",
    "AgentTimeoutError",
]
