"""In-place mutation delivery: do-no-harm marker + first-class revert.

Writes generated content directly into the authored source, tagging every
machine-written region with a marker so a later pass (or a human) can tell
authored from generated at a glance. Revert is first-class: any marked
region can be reverted to its pre-generation state without touching
unmarked, human-authored content around it. Drives the ``vcs`` seam for the
two-phase generate/apply changeset choreography (open-for-edit before
writing, describe-from-moved-subset after).
"""


def apply_inplace(target_path, generated: dict, vcs_backend) -> None:
    """Write generated content into target_path with do-no-harm markers, via the VCS seam."""
    raise NotImplementedError


def revert_marked(target_path, marker: str) -> None:
    """Revert every do-no-harm-marked region in target_path to its pre-generation state."""
    raise NotImplementedError
