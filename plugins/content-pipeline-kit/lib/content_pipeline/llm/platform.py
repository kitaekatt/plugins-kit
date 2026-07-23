"""Thin platform shim: transport / retry / content-addressed cache / cost / budget.

Wraps the raw client openrouter-kit provides (``openrouter_kit.make_openai_client``)
with the pipeline-shaped concerns every batch LLM call site needs: retry on
transient failure, a content-addressed cache keyed on the request payload (so
an identical request never pays for a second completion), running cost
accounting, and a budget hard-stop. Does not itself decide which backend or
model to call -- that is ``backends``'s job; this module is the shared
plumbing every backend runs its calls through.
"""


def cached_complete(request: dict, cache_dir) -> dict:
    """Run a completion through the content-addressed cache, transport, and retry layer."""
    raise NotImplementedError
