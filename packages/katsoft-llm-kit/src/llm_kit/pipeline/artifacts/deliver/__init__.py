"""deliver -- two delivery modes.

``inplace`` mutates authored content in place, marking every machine-written
region with a do-no-harm tag and offering first-class revert. ``projection``
instead emits append-only projection artifacts alongside the source,
rolling back via a ``.bak`` file and never overwriting. A pipeline picks
exactly one mode; both drive the ``vcs`` seam for the changeset choreography
around the write.

Dependency contract: ``deliver`` may import ``store`` / ``freshness`` and the
``vcs`` seam TYPE only via injection -- it takes a ``VcsBackend`` instance, it
never constructs one (so it never imports ``vcs.git_vcs`` or ``vcs.null_vcs``).

Deviations from the source systems
----------------------------------

1. **The changeset choreography lives in ``deliver``, backend-agnostic.**
   ``inplace.deliver_changeset`` implements the "placeholder changeset up front
   -> per-item inline moves -> description rebuilt from the successfully-moved
   subset -> delete-if-empty" sequence ONCE, driving whichever injected
   ``VcsBackend`` is configured -- it is delivery-mode logic, not backend logic,
   so it is not duplicated per backend (the source ``revert_and_bundle`` +
   ``cl_creation`` generalized).
2. **Apply is a pure function of the store.** ``inplace.apply_inplace`` rebuilds
   only marked (machine-owned) rows from the store's projected value, skips
   human-owned (populated-but-unmarked) rows, and skips rows the store has no
   value for -- the source ``_apply_assignments`` do-no-harm rule, generalized
   to a caller-supplied ``InplaceSpec`` with zero hardcoded field names.
3. **The ownership marker is a schema, not a fixed tag.** ``inplace.Marker`` /
   ``classify_ownership`` use a configurable marker and HUMAN/MACHINE/EMPTY
   classification. The append-only
   ``projection`` writers keep the ``.bak`` rollback + reload-validation and add
   the xliff-aggregation SHAPE (``aggregate_projections``: many ``(artifact,
   unit)`` pairs -> one artifact) with the on-disk format left to the caller.
"""
