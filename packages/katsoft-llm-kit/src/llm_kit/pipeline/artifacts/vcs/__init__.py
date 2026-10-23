"""vcs -- the version-control seam.

``seam`` declares the VcsBackend protocol every delivery mode drives.
``null_vcs`` is the no-op backend (CI, tests, non-VCS consumers). ``git_vcs``
is the shipped default -- git is the implied default VCS for this plugin.
Perforce support ships in p4-kit instead of here: p4-kit's role is making
things built git-first work under p4, by substituting the p4 implementation
of this same seam. The two-phase generate/apply changeset choreography lives
in ``deliver``, driving whichever backend is configured -- it is delivery-
mode logic, not backend logic, so it is not duplicated per backend.

This package imports NOTHING from the rest of ``content_pipeline`` -- a backend
is a leaf the delivery modes drive, never a consumer of the library.

Deviations from the source systems
----------------------------------

**The git changeset mapping (git has no pending-changelist).** The ``VcsBackend``
protocol is shaped around Perforce's pending changelist; ``git_vcs`` maps it as:
a changeset == a staged set finalized as a commit (``make_changeset`` creates no
git object, only an in-memory staged-path record; ``finalize_description``
commits the staged subset with the rebuilt message); ``open_for_edit`` is a
no-op (git needs no per-file checkout); ``move_into`` == ``git add`` of the
exact paths (never a wildcard, so unrelated working-tree changes are never swept
in); ``revert`` == ``git checkout HEAD -- <path>`` for exactly one path;
``delete_if_empty`` is a no-op (an empty changeset was never committed). The full
mapping table lives in ``git_vcs``'s module docstring.
"""
