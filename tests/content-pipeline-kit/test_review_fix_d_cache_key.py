"""Response-cache keys are pinned to literal digests.

Every cached response on disk is addressed by ``build_cache_key``. A comparison
of the function with itself passes whatever the function does, so it cannot
notice a key change that makes an entire corpus miss and re-spend. These
literals were computed from build_cache_key as of content-pipeline-kit 0.27.0; a deliberate key
change must update them and say so.
"""

from content_pipeline.llm.platform import BackendOptions, build_cache_key

MINIMAL_DIGEST = "963f763125a571547223cde37c92acaf745f0becfec00fd2912f8800574540b5"
FULL_DIGEST = "02898b111e18cf7fa3d2267b867dc3f8dfed755314058232eabf91be0249e179"


def test_minimal_key_matches_the_pinned_digest():
    key = build_cache_key(backend="mock", model="m", system="s", user="u")
    assert key == MINIMAL_DIGEST


def test_key_with_every_participating_field_matches_the_pinned_digest():
    options = BackendOptions(
        temperature=0.2,
        max_tokens=100,
        effort="high",
        allowed_tools=["Read"],
        extras={"b": 1, "a": 2},
        cwd="/w",
        cache_salt="salt",
        user_cache_prefix="pre",
    )
    key = build_cache_key(
        backend="claude-cli", model="opus", system="sys\n", user="hi", options=options
    )
    assert key == FULL_DIGEST


def test_deadline_and_client_id_do_not_move_the_pinned_digest():
    options = BackendOptions(timeout_s=5, client_id="run-1")
    key = build_cache_key(
        backend="mock", model="m", system="s", user="u", options=options
    )
    assert key == MINIMAL_DIGEST
