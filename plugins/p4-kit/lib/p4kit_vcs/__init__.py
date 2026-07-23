"""p4kit_vcs -- p4-kit's Perforce implementation of content-pipeline-kit's VcsBackend.

Ships the Perforce backend for the ``VcsBackend`` seam defined in
content-pipeline-kit (``content_pipeline.vcs.seam``). Kept here, in p4-kit,
rather than in content-pipeline-kit so that plugin never depends on p4 tooling:
p4-kit's role is "make git-first things work under p4 by substituting the p4
implementation of the same seam."

This package imports NOTHING from content_pipeline -- protocol conformance is
structural (duck typing), so P4Vcs satisfies ``content_pipeline.vcs.seam.VcsBackend``
without importing it, and p4-kit works standalone.
"""

from .p4_vcs import P4Changeset, P4Runner, P4Vcs, P4VcsError

__all__ = ["P4Vcs", "P4Changeset", "P4VcsError", "P4Runner"]
