"""Budget guard / hard-stop on 429/401, auth-expiry preflight.

A bulk CLI run checks its budget before starting (auth-expiry preflight, so
a run does not burn partial progress before discovering an expired key) and
during (a hard-stop on 429 rate-limit or 401 auth-failure responses, so a
misconfigured run halts instead of retry-looping against a dead credential).
"""


def preflight_check(backend) -> None:
    """Raise if auth is expired or the budget is already exhausted, before a run starts."""
    raise NotImplementedError


def check_response(response) -> None:
    """Raise a hard-stop if response indicates 429/401; otherwise update budget accounting."""
    raise NotImplementedError
