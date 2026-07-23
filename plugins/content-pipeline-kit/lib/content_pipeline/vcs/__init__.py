"""vcs -- the version-control seam.

``seam`` declares the VcsBackend protocol every delivery mode drives.
``null_vcs`` is the no-op backend (CI, tests, non-VCS consumers). ``git_vcs``
is the shipped default -- git is the implied default VCS for this plugin.
Perforce support ships in p4-kit instead of here: p4-kit's role is making
things built git-first work under p4, by substituting the p4 implementation
of this same seam. The two-phase generate/apply changeset choreography lives
in ``deliver``, driving whichever backend is configured -- it is delivery-
mode logic, not backend logic, so it is not duplicated per backend.
"""
