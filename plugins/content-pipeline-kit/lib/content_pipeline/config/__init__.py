"""config -- config-in-charge loader surface.

Locates a project's config root by walking up from a starting path to a
marker file, then loads and caches config content with mtime-based
invalidation. Hashing used for change detection strips doc-block comments
first, so editing a comment does not perpetually invalidate downstream
caches that key off the config's content hash. No dependency on any other
``content_pipeline`` subpackage (REP: usable standalone by any consumer that
just wants config-in-charge loading).
"""
