"""Provider registry: claude-cli loop / openrouter completion / mock, by process-level routing.

Selects, once per process (not per call), which backend a pipeline run talks
to: the openrouter completion path (via openrouter-kit's client), a
claude-cli agent-loop transport, or the mock seam used by tests. The
claude-cli adapter here is deliberately thin -- see the plugin's proposal
decision on gen-ops: it is expected to migrate down into openrouter-kit
(as a value-add "use Claude locally instead of an endpoint" backend) without
this module's call-site interface changing.
"""


def select_backend(backend_name: str):
    """Return the backend callable for 'openrouter' | 'claude-cli' | 'mock'."""
    raise NotImplementedError
