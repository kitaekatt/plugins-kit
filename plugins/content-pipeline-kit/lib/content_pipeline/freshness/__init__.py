"""freshness -- two-tier content-hash staleness engine.

The most duplicated subsystem across the two systems this plugin unifies,
and the cleanest (pure, no LLM, no VCS, no I/O side effects to mock) --
which is why it is built and ported first. Submodules:

- ``hashing`` -- stable, deterministic content hashing over already-prepared
  values; the shared-snapshot + per-item split; the corpus cross-reference
  digest.
- ``classify`` -- the single ``FreshnessState`` predicate
  (``HUMAN > EXCLUDED > MISSING > STALE > FRESH``) plus ``needs_generation``
  and ``bucket_counts``, so every "needs regen" check and every
  coverage-bucket site delegates to one place.
- ``tier`` -- the two-tier model (source tier / generation tier) and the
  cross-reference staleness predicate, as small frozen dataclasses.
- ``ensure`` -- the ensure-chain: always regenerate cheaply in memory,
  compare content hashes, write only on a real change, with an optional
  version-control pre-write hook supplied as a plain callable.
- ``seed`` -- deterministic seeding of stochastic gating decisions from
  stable identity, so a flag flip never perpetually invalidates a hash.

The package re-exports nothing eagerly (import a submodule directly); nothing
here imports ``content_pipeline.vcs`` or the LLM stack.

Deviations from the two source systems' semantics
--------------------------------------------------

The generic design preserves every *behavioral* case the two source suites
pin, but three details differ deliberately. Each is recorded here so a
port of an originating system knows where equivalence is behavioral rather
than byte-for-byte.

1. **Uniform string canonicalization (no legacy raw-UTF-8 path).** The
   first-pass system hashes a legacy bare-string field as raw UTF-8 bytes
   while hashing the newer dict shape via sorted-key JSON -- a migration
   concession so its ~111 existing character files did not mass-restale when
   the field shape changed. The generic library has no pre-existing corpus
   to stay byte-compatible with, so ``hashing.canonical_bytes`` routes every
   non-``bytes`` value (strings included) through ``stable_json`` uniformly.
   The behavioral property the source test pins -- a bare string and a dict
   wrapping it must hash *differently* -- still holds (``"foo"`` -> ``b'"foo"'``
   vs ``{"v": "foo"}`` -> ``b'{"v":"foo"}'``). Only byte-for-byte identity with
   the pre-migration encoding is dropped, and that has no meaning outside the
   first-pass corpus.

2. **Empty recorded hash on a present machine value classifies as STALE, not
   MISSING.** The first-pass classifier buckets a present machine value whose
   ``inputs_hash`` is empty as ``missing``; the localization classifier
   buckets the same situation as ``stale`` (its ``stored != expected`` branch
   fires on an empty stored hash). The generic ``classify`` follows the
   localization reading and this package's own state definitions -- ``MISSING``
   means *no machine value*, ``STALE`` means *the recorded hash does not match*
   -- so an empty recorded hash on a present value is ``STALE``. This is not a
   behavioral divergence for regeneration: ``needs_generation`` returns True
   for both ``MISSING`` and ``STALE`` (default sweep), exactly as both source
   systems regenerate this case; only the coverage-bucket label differs.

3. **``EXCLUDED`` is checked after ``HUMAN``.** Neither source classifier had
   both tiers at one site: first-pass checked its excluded lines (player /
   hold) first and had no human tier there; localization checked its human
   override first and had no excluded tier. The union orders
   ``HUMAN > EXCLUDED``, so a human-authored value wins even on an otherwise
   non-applicable item -- consistent with the do-no-harm rule that a human
   value is never discarded, and a superset of both source behaviors (neither
   could ever observe a human value on an excluded item).
"""
