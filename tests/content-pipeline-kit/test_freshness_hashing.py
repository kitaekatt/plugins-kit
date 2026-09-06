"""Tests for content_pipeline.freshness.hashing.

These cases mirror the freshness hashing behavior pinned by BOTH source
systems' suites (a single-hash animation-assignment pipeline and a two-tier
translation pipeline), translated to the plugin's neutral vocabulary as the
port-equivalence baseline. No game/domain concepts appear in the test bodies:
"units" hold "items"; a source-tier hash covers a unit's source content and a
generation-tier hash covers a per-item generation input.
"""

from content_pipeline.freshness.hashing import (
    DEFAULT_DIGEST_LENGTH,
    FULL_DIGEST_LENGTH,
    canonical_bytes,
    combined_hash,
    content_hash,
    corpus_hash,
    digest,
    shared_snapshot,
    stable_json,
)


# -- stable_json primitive ----------------------------------------------------

def test_stable_json_sorts_keys():
    a = {"x": 1, "y": 2, "z": 3}
    b = {"z": 3, "y": 2, "x": 1}
    assert stable_json(a) == stable_json(b)


def test_stable_json_ascii_only():
    out = stable_json({"k": chr(0x2603)})  # a non-ASCII code point
    assert b"\\u2603" in out
    assert all(byte < 0x80 for byte in out)


# -- digest primitive ---------------------------------------------------------

def test_digest_separator_collision_safe():
    assert digest(b"ab", b"c") != digest(b"a", b"bc")


def test_digest_default_length_is_16():
    h = digest(b"payload")
    assert len(h) == DEFAULT_DIGEST_LENGTH == 16
    int(h, 16)  # hex only


def test_digest_full_length_is_64():
    h = digest(b"payload", length=FULL_DIGEST_LENGTH)
    assert len(h) == 64
    int(h, 16)


def test_digest_truncation_is_prefix_of_full():
    full = digest(b"payload", length=FULL_DIGEST_LENGTH)
    short = digest(b"payload", length=16)
    assert full.startswith(short)


# -- canonical_bytes ----------------------------------------------------------

def test_canonical_bytes_passthrough_for_bytes():
    assert canonical_bytes(b"already") == b"already"


def test_bare_string_and_wrapping_dict_hash_differently():
    # Deviation #1 behavioral check: uniform stable_json still keeps a bare
    # string distinct from a dict that wraps it.
    assert content_hash("foo") != content_hash({"value": "foo"})


# -- content_hash -------------------------------------------------------------

def test_content_hash_stable_across_calls():
    values = ("source-text", {"provider": "out"})
    assert content_hash(*values) == content_hash(*values)


def test_content_hash_changes_with_any_input():
    base = content_hash("source-text", {"provider": "out"})
    assert base != content_hash("other-text", {"provider": "out"})
    assert base != content_hash("source-text", {"provider": "changed"})


def test_content_hash_dict_insensitive_to_key_order():
    # Reordering keys of a mapping input must not drift the hash.
    first = content_hash({"a": 1, "b": 2, "c": 3})
    second = content_hash({"c": 3, "b": 2, "a": 1})
    assert first == second


def test_content_hash_accepts_prepared_bytes_alongside_values():
    prepared = shared_snapshot("shared")
    a = content_hash("item", *prepared)
    b = content_hash("item", *prepared)
    assert a == b


# -- shared_snapshot + combined_hash (the two-tier split) ---------------------

def test_shared_snapshot_returns_canonical_bytes_tuple():
    snap = shared_snapshot("unit-source", {"prompt": "p"})
    assert isinstance(snap, tuple)
    assert all(isinstance(part, bytes) for part in snap)


def test_combined_hash_stable_across_calls():
    shared = shared_snapshot("unit-source", {"prompt": "p"})
    item = {"item_id": "k1", "input": "text"}
    assert combined_hash(item, shared) == combined_hash(item, shared)


def test_combined_hash_default_length_is_16():
    shared = shared_snapshot("unit-source")
    h = combined_hash({"item_id": "k1"}, shared)
    assert len(h) == 16
    int(h, 16)


def test_combined_hash_changes_when_item_changes():
    shared = shared_snapshot("unit-source", {"prompt": "p"})
    before = combined_hash({"item_id": "k1", "input": "a"}, shared)
    after = combined_hash({"item_id": "k1", "input": "b"}, shared)
    assert before != after


def test_combined_hash_changes_when_shared_changes():
    item = {"item_id": "k1", "input": "a"}
    before = combined_hash(item, shared_snapshot("unit-source", {"prompt": "p1"}))
    after = combined_hash(item, shared_snapshot("unit-source", {"prompt": "p2"}))
    assert before != after


def test_one_item_change_does_not_touch_another_items_hash():
    # The point of the per-item split: editing item A must not invalidate
    # item B's stored hash.
    shared = shared_snapshot("unit-source", {"prompt": "p"})
    a = {"item_id": "A", "input": "a"}
    b = {"item_id": "B", "input": "b"}
    b_hash_before = combined_hash(b, shared)
    a["input"] = "a-edited"
    b_hash_after = combined_hash(b, shared)
    assert b_hash_before == b_hash_after


# -- corpus_hash (cross-reference digest) -------------------------------------

def test_corpus_hash_stable():
    pairs = [("K1", "h1"), ("K2", "h2")]
    assert corpus_hash(pairs) == corpus_hash(list(pairs))


def test_corpus_hash_changes_when_a_source_hash_changes():
    before = corpus_hash([("K1", "h1"), ("K2", "h2")])
    after = corpus_hash([("K1", "h1_NEW"), ("K2", "h2")])
    assert before != after


def test_corpus_hash_full_length_by_default():
    h = corpus_hash([("K1", "h1")])
    assert len(h) == 64


# -- deterministic canonicalization of set-bearing payloads -------------------

def test_content_hash_over_a_set_is_stable_across_process_hash_seeds():
    import os
    import subprocess
    import sys

    lib_path = os.path.normpath(
        os.path.join(os.path.dirname(__file__), os.pardir, os.pardir,
                     "plugins", "content-pipeline-kit", "lib")
    )
    script = (
        "from content_pipeline.freshness.hashing import content_hash\n"
        "print(content_hash({'tags': {'b', 'a', 'c'}, 'k': 1}))\n"
    )
    base_env = dict(os.environ)
    base_env["PYTHONPATH"] = lib_path + os.pathsep + base_env.get("PYTHONPATH", "")

    def run_with_seed(seed: str) -> str:
        env = dict(base_env)
        env["PYTHONHASHSEED"] = seed
        return subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        ).stdout.strip()

    assert run_with_seed("0") == run_with_seed("1000")


def test_content_hash_over_a_plain_object_raises_type_error():
    class Unhashable:
        pass

    try:
        content_hash({"v": Unhashable()})
    except TypeError as exc:
        assert "v" in str(exc)
    else:
        raise AssertionError("expected TypeError for an uncanonicalizable value")
