"""deliver -- two delivery modes.

``inplace`` mutates authored content in place, marking every machine-written
region with a do-no-harm tag and offering first-class revert. ``projection``
instead emits append-only projection artifacts alongside the source,
rolling back via a ``.bak`` file and never overwriting. A pipeline picks
exactly one mode; both drive the ``vcs`` seam for the changeset choreography
around the write.
"""
