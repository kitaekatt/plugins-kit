"""Classify every output vs. policy + brief + ground truth, runtime-shared classifiers.

The audit reuses the exact classifier functions the pipeline runtime uses
during generation and validation (see ``validate.contract``), rather than a
separate audit-only rule set -- so an audit finding can never simply be the
audit and the runtime disagreeing about the same rule.
"""


def audit_entity(entity_id: str, output, policy, brief, ground_truth=None) -> dict:
    """Classify one output against policy/brief/ground-truth using runtime classifiers."""
    raise NotImplementedError
