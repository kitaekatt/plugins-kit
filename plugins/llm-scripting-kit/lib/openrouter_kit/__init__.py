"""openrouter_kit -- shared OpenRouter API key resolution and account validation.

Consumers (loc-ops, future tooling) import from here:

    from openrouter_kit import get_api_key, make_openai_client, check_account
    from openrouter_kit import BASE_URL, API_KEY_ENV

Stdlib-only by default. ``make_openai_client`` lazy-imports the ``openai`` SDK
so callers that only need the raw key (or use a different HTTP client) do not
pay the SDK install cost.
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
]
