"""store -- the canonical, attributed, freshness-anchored artifact.

Holds the four pieces of the canonical-store abstraction: field-level
attribution with human-always-wins precedence (``attributed``), the
single-intermediary hashing anchor that makes regeneration intelligent by
construction (``intermediary``), the candidate cell for the many-candidates
case -- active/shadow/retired lists plus cached grades and deterministic fact
riders (``candidate``), and the canonical-store-to-consumer-visible
projection (``projection``). Depends on nothing outside this package (REP:
``store`` is usable without ``llm``, without ``vcs``). The only cross-package
reach is ``candidate`` and ``projection`` into ``freshness.hashing`` (rider
cache keys) and ``store.attributed`` respectively -- both inside the library.

Deviations from the two source systems' semantics
--------------------------------------------------

The generic design preserves every behavioral case the two source suites
pin, but several details are deliberate semantic unions. Each is recorded
here so a port of an originating system knows where equivalence is behavioral
rather than byte-for-byte.

1. **Presence precedence unifies the scalar-wins and block-wins shapes.**
   ``attributed.effective_value`` resolves ``human > machine > sourced`` by a
   configurable ``present`` predicate (default: truthiness). Plain truthiness
   handles the scalar case; a block-aware predicate handles designer ownership,
   where a human ``{body, face}`` block wins *wholesale* when any sub-field is set
   -- even if the value it yields is an empty sub-field. The union expresses
   both: the caller passes a block-aware ``present`` (``any sub-field truthy``)
   for the designer-ownership shape, and the default truthiness predicate for
   the scalar shape. No sub-field name is hardcoded.

2. **``merge_preserved_fields`` is declarative, not hand-wired.** Field names
   and machine-field categories are caller-supplied through ``MergePolicy``
   data: ``carry_fields`` (verbatim-when-present -- machine blocks whose
   downstream freshness check
   decides validity, plus hashes) versus ``conditional_fields``
   (carried only when a ``unchanged`` predicate holds). ``human_fields`` and
   ``carry_fields`` share the carry-when-present mechanism and are split only
   to document intent. Keyed sub-collections (the ``lines`` / ``questions``
   lists) merge under ``CollectionMerge``, including the "retain an orphaned
   existing item that still carries authored work" rule (a question with an
   answer). The domain-specific ``sanitize``/phantom-text-drift behavior stays
   project-side; the neutral tests pin the preservation semantics only.

3. **The intermediary full path always writes.** ``intermediary.ensure_intermediary``
   mirrors ``ensure_character_file``: on hash drift it rebuilds and always
   writes, re-stamping the current hash even when the synthesized content is
   byte-identical, so the cheap path reclaims the entity next run instead of
   taking the full path forever. ``changed`` therefore means "written this
   call". An optional ``content_equal`` enriches the result with a
   ``content_changed`` flag (content differed vs. hash-only re-stamp) without
   gating the write.

4. **``promote_candidate`` unifies retire-previous and keep-as-shadow.** The
   simple promote-and-supersede shape retires the prior active; the
   many-candidate loop keeps it as a shadow (still selectable). The union is
   one function with ``retire_previous`` (default ``False`` == keep-as-shadow,
   the many-candidate behavior). ``candidate`` also keeps serialization
   pluggable -- ``load_store`` / ``dump_store`` take an injected YAML engine
   rather than binding a comment-preserving parser, per the source system's
   finding that binding one to a bulk generated store was pathologically slow.

5. **``store.projection`` computes the view; it does not write.** The
   projection seam is split: ``store/projection.py`` reduces a canonical
   record to its projected value shape (effective value / active candidate),
   while the actual materialization (in-place mutation, append-only file)
   lives in ``deliver`` -- matching the canonical-store-plus-projection
   pattern where consumers read only the projected result.
"""
