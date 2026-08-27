"""D-7: a 'single' source covers the keys no other source on its path claims.

Two sources can legitimately address different regions of ONE file -- a
'single' source (whole document, no identity) and a 'rows' source with
'key:' (one named key, a list of records). Before this fix the validator
treated the whole document as belonging to the 'single' source, so the
'rows' source's own key showed up as unknown fields on the 'single' record.
"""

import re
from pathlib import Path
from typing import Callable

import pytest

from yaml_data_editor_kit.comments import EvaluationError, evaluate, parse_selector
from yaml_data_editor_kit.schema import (
    errors_only,
    load_corpus,
    load_profile,
    validate_corpus,
)

Writer = Callable[[str, str], Path]


MANIFEST_TYPE = """
dialect: type/1
id: manifest
fields:
  seed:   { type: int }
  motto:  { type: string }
  slogan: { type: string }
---
dialect: source/1
of: manifest
layout: single
path: content/manifest.yaml
"""

FAMILY_TYPE = """
dialect: type/1
id: family
identified_by: id
fields:
  id:   { type: id }
  name: { type: string }
---
dialect: source/1
of: family
layout: rows
path: content/manifest.yaml
key: families
"""

MANIFEST_DOCUMENT = """
seed: 7
motto: onward
slogan: together
families:
  - { id: alpha, name: Alpha }
  - { id: beta,  name: Beta }
  - { id: gamma, name: Gamma }
"""


def _write_manifest_and_family(write: Writer) -> None:
    write("profile/manifest.yaml", MANIFEST_TYPE)
    write("profile/family.yaml", FAMILY_TYPE)
    write("content/manifest.yaml", MANIFEST_DOCUMENT)


# -- the main ruling ------------------------------------------------------


def test_single_excludes_a_key_a_rows_source_claims(tmp_path, profile_dir, write) -> None:
    _write_manifest_and_family(write)
    profile = load_profile(profile_dir)

    corpus = load_corpus(profile, tmp_path)

    manifest = corpus.find("manifest", None) or corpus.of_type("manifest")[0]
    assert "families" not in manifest.data
    assert manifest.data == {"seed": 7, "motto": "onward", "slogan": "together"}
    assert manifest.excluded_keys == {"families"}
    assert [r.identity for r in corpus.of_type("family")] == ["alpha", "beta", "gamma"]
    assert errors_only(validate_corpus(profile, tmp_path)) == []


def test_single_excludes_two_distinct_claimed_keys_from_two_rows_sources(
    tmp_path, profile_dir, write
) -> None:
    """Two DIFFERENT 'rows'+'key:' sources on the same path, each claiming a
    different key: both must be excluded, not just the first one found."""
    write("profile/manifest.yaml", MANIFEST_TYPE)
    write("profile/family.yaml", FAMILY_TYPE)
    write(
        "profile/tribe.yaml",
        """
dialect: type/1
id: tribe
identified_by: id
fields:
  id:   { type: id }
  name: { type: string }
---
dialect: source/1
of: tribe
layout: rows
path: content/manifest.yaml
key: tribes
""",
    )
    write(
        "content/manifest.yaml",
        """
seed: 7
motto: onward
slogan: together
families:
  - { id: alpha, name: Alpha }
  - { id: beta,  name: Beta }
tribes:
  - { id: north, name: North }
  - { id: south, name: South }
""",
    )
    profile = load_profile(profile_dir)

    corpus = load_corpus(profile, tmp_path)
    manifest = corpus.of_type("manifest")[0]

    assert manifest.excluded_keys == {"families", "tribes"}
    assert manifest.data == {"seed": 7, "motto": "onward", "slogan": "together"}
    assert [r.identity for r in corpus.of_type("family")] == ["alpha", "beta"]
    assert [r.identity for r in corpus.of_type("tribe")] == ["north", "south"]
    assert errors_only(validate_corpus(profile, tmp_path)) == []


def test_single_record_stays_multi_row_and_multi_key_so_truncation_would_show(
    tmp_path, profile_dir, write
) -> None:
    """(a) size-0/1 fixture guard: 3 rows and 3 remaining single keys, so a
    `[:1]` truncation mutant on either side would change these counts."""
    _write_manifest_and_family(write)
    profile = load_profile(profile_dir)

    corpus = load_corpus(profile, tmp_path)

    manifest = corpus.of_type("manifest")[0]
    assert len(manifest.data) == 3
    assert len(corpus.of_type("family")) == 3


def test_unclaimed_keys_are_still_validated(tmp_path, profile_dir, write) -> None:
    """(e) half-pinned invariant: exclusion must not become "skip everything"."""
    write("profile/manifest.yaml", MANIFEST_TYPE)
    write("profile/family.yaml", FAMILY_TYPE)
    write(
        "content/manifest.yaml",
        """
seed: not-a-number
motto: onward
slogan: together
families:
  - { id: alpha, name: Alpha }
""",
    )
    profile = load_profile(profile_dir)

    problems = errors_only(validate_corpus(profile, tmp_path))

    assert len(problems) == 1
    assert problems[0].field == "seed"
    assert "declared 'int'" in problems[0].message


def test_unknown_unclaimed_key_is_still_reported(tmp_path, profile_dir, write) -> None:
    """A key nobody claims and the type does not declare is still an error --
    exclusion is scoped to claimed keys only."""
    write("profile/manifest.yaml", MANIFEST_TYPE)
    write("profile/family.yaml", FAMILY_TYPE)
    write(
        "content/manifest.yaml",
        """
seed: 7
motto: onward
slogan: together
mystery_key: surprise
families:
  - { id: alpha, name: Alpha }
""",
    )
    profile = load_profile(profile_dir)

    problems = errors_only(validate_corpus(profile, tmp_path))

    assert len(problems) == 1
    assert problems[0].field == "mystery_key"
    assert "is not a field type 'manifest' declares" in problems[0].message


def test_a_genuinely_missing_required_field_still_errors_on_a_record_with_an_exclusion(
    tmp_path, profile_dir, write
) -> None:
    """(f) dead-proof guard on the required-check exemption: the exemption
    must be scoped to the NAMED excluded key, not to "this record has any
    exclusion at all". A record that legitimately excludes 'families' but
    is ALSO missing its own required 'motto' must still report 'motto' --
    proving the exemption did not widen into a blanket amnesty."""
    write("profile/manifest.yaml", MANIFEST_TYPE)
    write("profile/family.yaml", FAMILY_TYPE)
    write(
        "content/manifest.yaml",
        """
seed: 7
slogan: together
families:
  - { id: alpha, name: Alpha }
""",
    )
    profile = load_profile(profile_dir)

    problems = errors_only(validate_corpus(profile, tmp_path))

    assert len(problems) == 1
    assert problems[0].field == "motto"
    assert "is required but absent" in problems[0].message


# -- near-twins -------------------------------------------------------------


def test_a_same_named_key_claimed_on_a_different_path_is_not_excluded(
    tmp_path, profile_dir, write
) -> None:
    """(b) near-twin: a 'rows'+'key: families' source on a DIFFERENT file must
    not exclude 'families' from an unrelated 'single' source that happens to
    declare a field of the same name."""
    write("profile/manifest.yaml", MANIFEST_TYPE)
    write(
        "profile/family.yaml",
        """
dialect: type/1
id: family
identified_by: id
fields:
  id:   { type: id }
  name: { type: string }
---
dialect: source/1
of: family
layout: rows
path: content/other-file.yaml
key: families
""",
    )
    write(
        "content/manifest.yaml",
        """
seed: 7
motto: onward
slogan: together
families:
  - { id: alpha, name: Alpha }
""",
    )
    write("content/other-file.yaml", "families: []\n")
    profile = load_profile(profile_dir)

    corpus = load_corpus(profile, tmp_path)
    manifest = corpus.of_type("manifest")[0]

    assert "families" in manifest.data
    assert manifest.excluded_keys == set()
    problems = errors_only(validate_corpus(profile, tmp_path))
    assert len(problems) == 1
    assert problems[0].field == "families"
    assert "is not a field type 'manifest' declares" in problems[0].message


def test_a_claim_on_a_path_that_is_only_a_suffix_match_is_not_excluded(
    tmp_path, profile_dir, write
) -> None:
    """(b) near-twin, path-matching variant: a 'rows' source at
    'archive/content/manifest.yaml' must not claim a key on the DIFFERENT
    file 'content/manifest.yaml', even though one path is a suffix of the
    other -- 'single' compares paths by equality, not by suffix."""
    write("profile/manifest.yaml", MANIFEST_TYPE)
    write(
        "profile/family.yaml",
        """
dialect: type/1
id: family
identified_by: id
fields:
  id:   { type: id }
  name: { type: string }
---
dialect: source/1
of: family
layout: rows
path: archive/content/manifest.yaml
key: families
""",
    )
    write(
        "content/manifest.yaml",
        """
seed: 7
motto: onward
slogan: together
families:
  - { id: alpha, name: Alpha }
""",
    )
    write("archive/content/manifest.yaml", "families: []\n")
    profile = load_profile(profile_dir)

    corpus = load_corpus(profile, tmp_path)
    manifest = corpus.of_type("manifest")[0]

    assert "families" in manifest.data
    assert manifest.excluded_keys == set()


def test_claimed_key_also_declared_as_a_field_is_excluded_without_error(
    tmp_path, profile_dir, write
) -> None:
    """(b) near-twin: gear_manifest's real shape -- the 'single' type ALSO
    declares a field with the claimed key's name. The claim still wins: the
    key is excluded, the field is exempt from 'required but absent', and the
    key's data is validated entirely through the claiming source."""
    write(
        "profile/manifest.yaml",
        """
dialect: type/1
id: manifest
fields:
  seed: { type: int }
  families:
    type: list
    of: { type: record, fields: {} }
---
dialect: source/1
of: manifest
layout: single
path: content/manifest.yaml
""",
    )
    write("profile/family.yaml", FAMILY_TYPE)
    write(
        "content/manifest.yaml",
        """
seed: 7
families:
  - { id: alpha, name: Alpha }
  - { id: beta,  name: Beta }
""",
    )
    profile = load_profile(profile_dir)

    corpus = load_corpus(profile, tmp_path)
    manifest = corpus.of_type("manifest")[0]

    assert "families" not in manifest.data
    assert manifest.excluded_keys == {"families"}
    # Not excluded from validation altogether -- the OTHER source (rows)
    # still validates its own rows normally.
    assert [r.identity for r in corpus.of_type("family")] == ["alpha", "beta"]
    assert errors_only(validate_corpus(profile, tmp_path)) == []


def test_claimed_key_absent_from_the_document_does_not_crash_single(
    tmp_path, profile_dir, write
) -> None:
    """(b) near-twin: a claimed key that never appears in the document. The
    'single' record is unaffected; the claiming 'rows' source reports its
    own missing-key diagnostic."""
    write("profile/manifest.yaml", MANIFEST_TYPE)
    write("profile/family.yaml", FAMILY_TYPE)
    write(
        "content/manifest.yaml",
        """
seed: 7
motto: onward
slogan: together
""",
    )
    profile = load_profile(profile_dir)

    corpus = load_corpus(profile, tmp_path)
    manifest = corpus.of_type("manifest")[0]

    assert manifest.data == {"seed": 7, "motto": "onward", "slogan": "together"}
    problems = errors_only(load_corpus(profile, tmp_path).diagnostics)
    assert len(problems) == 1
    assert "no containing key 'families'" in problems[0].message


def test_exclusion_does_not_depend_on_the_key_being_present_in_the_document(
    tmp_path, profile_dir, write
) -> None:
    """(M7) the claim is a property of the PROFILE (a 'rows' source declares
    'key: families' on this path), not of what today's document happens to
    hold. A claimed key that is ALSO declared, REQUIRED, and absent from the
    document must still be excluded and exempt from the required check --
    reverting to 'resolves as ABSENT' the moment the document temporarily
    lacks the row block would restore the original defect."""
    write("profile/manifest.yaml", MANIFEST_TYPE_DECLARING_FAMILIES)
    write("profile/family.yaml", FAMILY_TYPE)
    write(
        "content/manifest.yaml",
        """
seed: 7
motto: onward
slogan: together
""",
    )
    profile = load_profile(profile_dir)

    corpus = load_corpus(profile, tmp_path)
    manifest = corpus.of_type("manifest")[0]

    # The exclusion set comes from the PROFILE, so it holds even though
    # 'families' is not in this document at all.
    assert manifest.excluded_keys == {"families"}

    problems = errors_only(validate_corpus(profile, tmp_path))
    # Only the 'rows' source's own "no containing key" complaint -- NOT a
    # "families is required but absent" finding on the manifest record.
    assert len(problems) == 1
    assert "no containing key 'families'" in problems[0].message


# -- refusals: coexistence that is NOT a well-defined proper subset --------


def test_two_single_sources_on_one_path_is_a_load_error(tmp_path, profile_dir, write) -> None:
    write(
        "profile/manifest.yaml",
        """
dialect: type/1
id: manifest
fields:
  seed: { type: int }
---
dialect: source/1
of: manifest
layout: single
path: content/manifest.yaml
""",
    )
    write(
        "profile/other_manifest.yaml",
        """
dialect: type/1
id: other_manifest
fields:
  seed: { type: int }
---
dialect: source/1
of: other_manifest
layout: single
path: content/manifest.yaml
""",
    )
    write("content/manifest.yaml", "seed: 7\n")
    profile = load_profile(profile_dir)

    problems = errors_only(load_corpus(profile, tmp_path).diagnostics)

    assert len(problems) == 1
    assert "two 'single' sources" in problems[0].message
    assert "manifest" in problems[0].message and "other_manifest" in problems[0].message


def test_rows_with_no_key_cannot_coexist_with_single(tmp_path, profile_dir, write) -> None:
    write("profile/manifest.yaml", MANIFEST_TYPE)
    write(
        "profile/family.yaml",
        """
dialect: type/1
id: family
identified_by: id
fields:
  id:   { type: id }
  name: { type: string }
---
dialect: source/1
of: family
layout: rows
path: content/manifest.yaml
""",
    )
    write(
        "content/manifest.yaml",
        """
seed: 7
motto: onward
slogan: together
""",
    )
    profile = load_profile(profile_dir)

    problems = errors_only(load_corpus(profile, tmp_path).diagnostics)

    # Pinned as a TOTAL, not a filtered subset: the coexistence refusal, plus
    # the 'rows' source's own separate complaint that a mapping is not a
    # list of records (the key-less 'rows' source really does try to read
    # this whole document as its sequence). A spurious third finding would
    # otherwise pass unnoticed.
    assert len(problems) == 2
    assert {p.message for p in problems} == {
        "a 'single' source for type 'manifest' shares this file with a "
        "'rows' source for type 'family': no 'key:', so it IS the "
        "document's sequence -- the whole file; a 'single' source can only "
        "coexist with a 'rows' source that names a specific 'key:'",
        "a 'rows' source must hold a list of records",
    }


def test_keyed_map_cannot_coexist_with_single(tmp_path, profile_dir, write) -> None:
    write("profile/manifest.yaml", MANIFEST_TYPE)
    write(
        "profile/family.yaml",
        """
dialect: type/1
id: family
identified_by: id
fields:
  id:   { type: id }
  name: { type: string }
---
dialect: source/1
of: family
layout: keyed_map
path: content/manifest.yaml
metadata_keys: [seed]
""",
    )
    write(
        "content/manifest.yaml",
        """
seed: 7
alpha: { id: alpha, name: Alpha }
""",
    )
    profile = load_profile(profile_dir)

    problems = errors_only(load_corpus(profile, tmp_path).diagnostics)

    assert len(problems) == 1
    assert problems[0].message == (
        "a 'single' source for type 'manifest' shares this file with a "
        "'keyed_map' source for type 'family': every top-level key is "
        "either one of its records or its metadata, leaving no third "
        "region for 'single'; a 'single' source can only coexist with a "
        "'rows' source that names a specific 'key:'"
    )


def test_file_per_record_glob_naming_the_exact_file_cannot_coexist_with_single(
    tmp_path, profile_dir, write
) -> None:
    """(a)/(4a): a FOURTH whole-document claimant -- a 'file_per_record'
    source whose glob happens to name this exact file reads it as one whole
    record, same as a key-less 'rows' source."""
    write("profile/manifest.yaml", MANIFEST_TYPE)
    write(
        "profile/family.yaml",
        """
dialect: type/1
id: family
identified_by: id
fields:
  id:   { type: id }
  name: { type: string }
---
dialect: source/1
of: family
layout: file_per_record
path: content/manifest.yaml
""",
    )
    write(
        "content/manifest.yaml",
        """
seed: 7
motto: onward
slogan: together
""",
    )
    profile = load_profile(profile_dir)

    problems = errors_only(load_corpus(profile, tmp_path).diagnostics)

    coexistence = [p for p in problems if "shares this file with a 'file_per_record'" in p.message]
    assert len(coexistence) == 1
    assert coexistence[0].message == (
        "a 'single' source for type 'manifest' shares this file with a "
        "'file_per_record' source for type 'family': its 'path:' glob "
        "matches this exact file, and it reads every matched file as one "
        "whole record; a 'single' source can only coexist with a 'rows' "
        "source that names a specific 'key:'"
    )


def test_file_per_record_glob_that_EXPANDS_to_the_single_path_also_refuses(
    tmp_path, profile_dir, write
) -> None:
    """(Defect 3) the glob need not spell the file literally to claim it:
    'content/*.yaml' EXPANDS to match 'content/manifest.yaml' exactly as
    surely as a pattern naming it verbatim. Before this fix, siblings were
    selected by literal string equality only, so this case produced NO
    coexistence refusal and the file loaded TWICE under two type ids,
    surfacing only a misleading incidental identity complaint."""
    write("profile/manifest.yaml", MANIFEST_TYPE)
    write(
        "profile/family.yaml",
        """
dialect: type/1
id: family
identified_by: id
fields:
  id:   { type: id }
  name: { type: string }
---
dialect: source/1
of: family
layout: file_per_record
path: content/*.yaml
""",
    )
    write(
        "content/manifest.yaml",
        """
seed: 7
motto: onward
slogan: together
""",
    )
    profile = load_profile(profile_dir)

    problems = errors_only(load_corpus(profile, tmp_path).diagnostics)

    coexistence = [p for p in problems if "shares this file with a 'file_per_record'" in p.message]
    assert len(coexistence) == 1


def test_recursive_file_per_record_glob_expanding_to_nested_single_path_refuses(
    tmp_path, profile_dir, write
) -> None:
    """M18: ``**`` must span the real intermediate directory instead of
    being erased and compared as though the file lived directly in
    ``content/``. This is the reproduced double-load shape: the recursive
    glob genuinely loads the nested file, so it must also trigger the
    whole-document coexistence refusal for that same concrete file."""
    write(
        "profile/manifest.yaml",
        MANIFEST_TYPE.replace(
            "path: content/manifest.yaml", "path: content/sub/manifest.yaml"
        ),
    )
    write(
        "profile/family.yaml",
        """
dialect: type/1
id: family
identified_by: id
fields:
  id:   { type: id }
  name: { type: string }
---
dialect: source/1
of: family
layout: file_per_record
path: content/**/manifest.yaml
""",
    )
    write(
        "content/sub/manifest.yaml",
        """
id: manifest
name: Shared document
seed: 7
motto: onward
slogan: together
""",
    )
    profile = load_profile(profile_dir)

    corpus = load_corpus(profile, tmp_path)
    coexistence = [
        problem
        for problem in errors_only(corpus.diagnostics)
        if "shares this file with a 'file_per_record'" in problem.message
    ]

    assert len(coexistence) == 1
    assert coexistence[0].file == "content/sub/manifest.yaml"
    assert [record.file for record in corpus.of_type("manifest")] == [
        "content/sub/manifest.yaml"
    ]
    assert [record.file for record in corpus.of_type("family")] == [
        "content/sub/manifest.yaml"
    ]


def test_recursive_file_per_record_glob_can_span_zero_directories(
    tmp_path, profile_dir, write
) -> None:
    """Boundary pin for recursive matching: ``**`` includes zero directory
    segments, so the direct child is claimed just like a nested child."""
    write("profile/manifest.yaml", MANIFEST_TYPE)
    write(
        "profile/family.yaml",
        """
dialect: type/1
id: family
identified_by: id
fields:
  id:   { type: id }
  name: { type: string }
---
dialect: source/1
of: family
layout: file_per_record
path: content/**/manifest.yaml
""",
    )
    write(
        "content/manifest.yaml",
        "id: manifest\nname: Shared document\nseed: 7\nmotto: onward\nslogan: together\n",
    )
    profile = load_profile(profile_dir)

    coexistence = [
        problem
        for problem in errors_only(load_corpus(profile, tmp_path).diagnostics)
        if "shares this file with a 'file_per_record'" in problem.message
    ]

    assert len(coexistence) == 1
    assert coexistence[0].file == "content/manifest.yaml"


def test_recursive_file_per_record_glob_with_a_different_filename_is_not_a_sibling(
    tmp_path, profile_dir, write
) -> None:
    """Near-twin for M18: ``**`` is recursive, but it must not turn a
    different terminal filename into a claim on the single source's file."""
    write(
        "profile/manifest.yaml",
        MANIFEST_TYPE.replace(
            "path: content/manifest.yaml", "path: content/sub/manifest.yaml"
        ),
    )
    write(
        "profile/family.yaml",
        """
dialect: type/1
id: family
identified_by: id
fields:
  id:   { type: id }
  name: { type: string }
---
dialect: source/1
of: family
layout: file_per_record
path: content/**/family.yaml
""",
    )
    write(
        "content/sub/manifest.yaml",
        "seed: 7\nmotto: onward\nslogan: together\n",
    )
    profile = load_profile(profile_dir)

    problems = errors_only(load_corpus(profile, tmp_path).diagnostics)

    assert [
        problem
        for problem in problems
        if "shares this file with a 'file_per_record'" in problem.message
    ] == []


def test_file_per_record_glob_that_does_not_expand_to_the_path_is_not_a_sibling(
    tmp_path, profile_dir, write
) -> None:
    """(b) near-twin: a 'file_per_record' glob that does NOT match this
    file at all (different directory) must not be treated as sharing the
    path -- glob matching, not "any file_per_record source anywhere"."""
    write("profile/manifest.yaml", MANIFEST_TYPE)
    write(
        "profile/family.yaml",
        """
dialect: type/1
id: family
identified_by: id
fields:
  id:   { type: id }
  name: { type: string }
---
dialect: source/1
of: family
layout: file_per_record
path: other-content/*.yaml
""",
    )
    write(
        "content/manifest.yaml",
        """
seed: 7
motto: onward
slogan: together
""",
    )
    profile = load_profile(profile_dir)

    problems = errors_only(load_corpus(profile, tmp_path).diagnostics)

    coexistence = [p for p in problems if "shares this file with a 'file_per_record'" in p.message]
    assert len(coexistence) == 0


def test_two_rows_sources_claiming_the_same_key_is_a_load_error(
    tmp_path, profile_dir, write
) -> None:
    write("profile/manifest.yaml", MANIFEST_TYPE)
    write("profile/family.yaml", FAMILY_TYPE)
    write(
        "profile/other_family.yaml",
        """
dialect: type/1
id: other_family
identified_by: id
fields:
  id:   { type: id }
  name: { type: string }
---
dialect: source/1
of: other_family
layout: rows
path: content/manifest.yaml
key: families
""",
    )
    write("content/manifest.yaml", MANIFEST_DOCUMENT)
    profile = load_profile(profile_dir)

    problems = errors_only(load_corpus(profile, tmp_path).diagnostics)

    assert len(problems) == 1
    assert problems[0].message == (
        "key 'families' of this file is claimed by two 'rows' sources -- "
        "one for type 'family' and one for type 'other_family'; one "
        "top-level key cannot have two owners"
    )
    assert problems[0].record == "families"


def test_two_rows_sources_sharing_a_key_with_no_single_present_is_out_of_scope(
    tmp_path, profile_dir, write
) -> None:
    """(4b) scope pin: the duplicate-claim refusal above only fires on a
    path a 'single' source ALSO occupies, because that is the only case
    '_precompute_single_claims' inspects. The identical two-'rows'-sources-
    same-'key:' setup with NO 'single' source on that path produces ZERO
    coexistence errors today, and silently loads the same rows twice under
    two type ids -- a real, separate defect this ruling does not cover.
    This test pins that scope choice so a future change does not silently
    widen or narrow it without a test noticing."""
    write("profile/family.yaml", FAMILY_TYPE)
    write(
        "profile/other_family.yaml",
        """
dialect: type/1
id: other_family
identified_by: id
fields:
  id:   { type: id }
  name: { type: string }
---
dialect: source/1
of: other_family
layout: rows
path: content/manifest.yaml
key: families
""",
    )
    write(
        "content/manifest.yaml",
        """
families:
  - { id: alpha, name: Alpha }
  - { id: beta,  name: Beta }
""",
    )
    profile = load_profile(profile_dir)

    problems = errors_only(load_corpus(profile, tmp_path).diagnostics)

    assert problems == []


# -- the anchor layer must not resolve an excluded key to ABSENT -----------


MANIFEST_TYPE_DECLARING_FAMILIES = """
dialect: type/1
id: manifest
fields:
  seed:   { type: int }
  motto:  { type: string }
  slogan: { type: string }
  families:
    type: list
    of: { type: record, fields: {} }
---
dialect: source/1
of: manifest
layout: single
path: content/manifest.yaml
"""


def test_an_address_into_an_excluded_key_raises_naming_the_claiming_source(
    tmp_path, profile_dir, write
) -> None:
    """The defect this guards: gear_manifest's real shape -- 'families' IS
    declared on the 'single' type (a placeholder field, per D-7). Without
    this check, a selector stepping into it resolves through
    'record.data.get(first, ABSENT)' to ABSENT instead of erroring.
    hashing.py canonicalizes ABSENT to a stable value, so a persisted
    comment anchor at that address would go stale exactly once (reported
    MOVED on the upgrade that introduced the exclusion) and then read OK
    forever after -- silently pointing at nothing, blind to every future
    edit of the region it used to name. The walk must fail loudly here
    instead."""
    write("profile/manifest.yaml", MANIFEST_TYPE_DECLARING_FAMILIES)
    write("profile/family.yaml", FAMILY_TYPE)
    write("content/manifest.yaml", MANIFEST_DOCUMENT)
    profile = load_profile(profile_dir)
    corpus = load_corpus(profile, tmp_path)

    with pytest.raises(EvaluationError) as exc_info:
        evaluate(parse_selector("manifest/@doc/families"), profile, corpus)

    message = str(exc_info.value)
    assert "excludes it" in message
    assert "'rows' source for type 'family'" in message

    # The suggested replacement address must actually RESOLVE, not merely
    # look like one -- extract it from "address it as '<addr>'" and evaluate
    # it for real.
    suggested = re.search(r"address it as '([^']+)'", message).group(1)
    assert suggested == "family/alpha"
    resolved = evaluate(parse_selector(suggested), profile, corpus)
    assert len(resolved.points) == 1


def test_an_undeclared_excluded_key_names_its_claiming_source_and_working_address(
    tmp_path, profile_dir, write
) -> None:
    """The normal D-7 shape does not repeat the claimed key as a placeholder
    field on the ``single`` type. Ownership is still known from
    ``excluded_keys``, so that actionable refusal must win over the generic
    unknown-field error."""
    _write_manifest_and_family(write)
    profile = load_profile(profile_dir)
    corpus = load_corpus(profile, tmp_path)

    with pytest.raises(EvaluationError) as exc_info:
        evaluate(parse_selector("manifest/@doc/families"), profile, corpus)

    message = str(exc_info.value)
    assert "'rows' source for type 'family' claims it" in message
    assert "fields of type 'manifest'" not in message
    suggested = re.search(r"address it as '([^']+)'", message).group(1)
    assert suggested == "family/alpha"
    assert len(evaluate(parse_selector(suggested), profile, corpus).points) == 1


def test_the_excluded_field_error_names_the_source_when_the_type_has_no_identity(
    tmp_path, profile_dir, write
) -> None:
    """(b) near-twin: the claiming type has no 'identified_by:', so the
    error must fall back to a working phrase rather than fabricate an
    example address that does not parse."""
    write("profile/manifest.yaml", MANIFEST_TYPE_DECLARING_FAMILIES)
    write(
        "profile/family.yaml",
        """
dialect: type/1
id: family
fields:
  name: { type: string }
---
dialect: source/1
of: family
layout: rows
path: content/manifest.yaml
key: families
""",
    )
    write("content/manifest.yaml", MANIFEST_DOCUMENT)
    profile = load_profile(profile_dir)
    corpus = load_corpus(profile, tmp_path)

    with pytest.raises(EvaluationError) as exc_info:
        evaluate(parse_selector("manifest/@doc/families"), profile, corpus)

    message = str(exc_info.value)
    assert "excludes it" in message
    assert "family" in message
    assert "the 'family' source's own records" in message


def test_an_address_into_an_unexcluded_field_still_resolves_normally(
    tmp_path, profile_dir, write
) -> None:
    """(e) half-pinned invariant: the new raise must not fire for a field
    that was never excluded."""
    _write_manifest_and_family(write)
    profile = load_profile(profile_dir)
    corpus = load_corpus(profile, tmp_path)

    result = evaluate(parse_selector("manifest/@doc/seed"), profile, corpus)

    assert len(result.points) == 1


# -- M5: the exclusion raise must fire on the WILDCARD walk too, not only --
# -- the single-selector walk ('_expand_field_paths' vs '_walk_named_fields')


def test_a_wildcard_selector_into_an_excluded_key_also_raises(
    tmp_path, profile_dir, write
) -> None:
    """(M5) 'parse_selector' strips a TRAILING '*', so 'families.*' reaches
    '_walk_named_fields' (already covered above). A wildcard with a segment
    AFTER it -- 'families.*.name' -- takes the DIFFERENT '_expand_field_paths'
    branch, which had its own independent 'record.data.get(first, ABSENT)'
    call and its own independent excluded-key check. Deleting that second
    check silently turns this selector into 'OK, points: []' instead of
    raising -- exactly the silent-empty failure class this revision exists
    to remove, surviving on the one branch a single-field test cannot reach."""
    write("profile/manifest.yaml", MANIFEST_TYPE_DECLARING_FAMILIES)
    write("profile/family.yaml", FAMILY_TYPE)
    write("content/manifest.yaml", MANIFEST_DOCUMENT)
    profile = load_profile(profile_dir)
    corpus = load_corpus(profile, tmp_path)

    with pytest.raises(EvaluationError) as exc_info:
        evaluate(parse_selector("manifest/@doc/families.*.name"), profile, corpus)

    message = str(exc_info.value)
    assert "excludes it" in message
    assert "'rows' source for type 'family'" in message


# -- M2: the required-check exemption must be scoped to a TOP-LEVEL key ----


def test_a_nested_field_sharing_an_excluded_keys_name_is_not_amnestied(
    tmp_path, profile_dir, write
) -> None:
    """(M2) 'validate.py' only exempts 'name in record.excluded_keys' when
    'prefix == \"\"' -- i.e. only a TOP-LEVEL key. A NESTED, required field
    that happens to share the excluded key's NAME ('families') is a
    different field entirely (a key of a different record shape, one level
    down) and must still be checked. Dropping the 'prefix == \"\"' guard
    would amnesty it by name alone, on every record of the type."""
    write(
        "profile/manifest.yaml",
        """
dialect: type/1
id: manifest
fields:
  seed:   { type: int }
  motto:  { type: string }
  slogan: { type: string }
  nested:
    type: record
    fields:
      families: { type: string }
  families:
    type: list
    of: { type: record, fields: {} }
---
dialect: source/1
of: manifest
layout: single
path: content/manifest.yaml
""",
    )
    write("profile/family.yaml", FAMILY_TYPE)
    write(
        "content/manifest.yaml",
        """
seed: 7
motto: onward
slogan: together
nested: {}
families:
  - { id: alpha, name: Alpha }
""",
    )
    profile = load_profile(profile_dir)

    problems = errors_only(validate_corpus(profile, tmp_path))

    assert len(problems) == 1
    assert problems[0].field == "nested.families"
    assert "is required but absent" in problems[0].message


# -- Defect 1a: the PREDICATE path must not reach an excluded key either ---


def test_a_predicate_over_an_excluded_required_field_raises_not_a_false_data_error(
    tmp_path, profile_dir, write
) -> None:
    """(1a) Before this fix: a predicate naming an excluded, required field
    ('families' on gear_manifest's real shape -- no 'required: false') hit
    the generic "has no value at required predicate field" error, which is
    FALSE (the data is fine; the key belongs elsewhere) and useless (it
    names neither the claiming source nor a working address). The predicate
    path must raise the SAME actionable error the plain field-walk does."""
    write("profile/manifest.yaml", MANIFEST_TYPE_DECLARING_FAMILIES)
    write("profile/family.yaml", FAMILY_TYPE)
    write("content/manifest.yaml", MANIFEST_DOCUMENT)
    profile = load_profile(profile_dir)
    corpus = load_corpus(profile, tmp_path)

    with pytest.raises(EvaluationError) as exc_info:
        evaluate(parse_selector("manifest/[families has alpha]/seed"), profile, corpus)

    message = str(exc_info.value)
    assert "excludes it" in message
    assert "'rows' source for type 'family'" in message
    assert "has no value at required predicate field" not in message


def test_a_predicate_over_an_undeclared_excluded_key_names_the_claiming_source(
    tmp_path, profile_dir, write
) -> None:
    """Predicate near-twin of the plain field walk: the excluded ownership
    check must precede the predicate's type-wide unknown-field rejection."""
    _write_manifest_and_family(write)
    profile = load_profile(profile_dir)
    corpus = load_corpus(profile, tmp_path)

    with pytest.raises(EvaluationError) as exc_info:
        evaluate(parse_selector("manifest/[families has alpha]/seed"), profile, corpus)

    message = str(exc_info.value)
    assert "'rows' source for type 'family' claims it" in message
    assert "does not resolve against the fields" not in message
    assert "address it as 'family/alpha'" in message


def test_a_predicate_over_an_excluded_optional_field_raises_not_a_silent_skip(
    tmp_path, profile_dir, write
) -> None:
    """(1a) near-twin: the SAME excluded key, but declared 'required: false'
    on the manifest type. Before this fix this was a SILENT SKIP -- ('OK',
    [], 0), zero points, zero matched records, no diagnostic at all. It must
    raise too: the field is unreachable through this record regardless of
    whether it is required."""
    write(
        "profile/manifest.yaml",
        """
dialect: type/1
id: manifest
fields:
  seed:   { type: int }
  motto:  { type: string }
  slogan: { type: string }
  families:
    type: list
    required: false
    of: { type: record, fields: {} }
---
dialect: source/1
of: manifest
layout: single
path: content/manifest.yaml
""",
    )
    write("profile/family.yaml", FAMILY_TYPE)
    write("content/manifest.yaml", MANIFEST_DOCUMENT)
    profile = load_profile(profile_dir)
    corpus = load_corpus(profile, tmp_path)

    with pytest.raises(EvaluationError) as exc_info:
        evaluate(parse_selector("manifest/[families has alpha]/seed"), profile, corpus)

    message = str(exc_info.value)
    assert "excludes it" in message


def test_a_predicate_over_an_unexcluded_field_still_evaluates_normally(
    tmp_path, profile_dir, write
) -> None:
    """(1a) half-pinned invariant: the new predicate-path raise must not
    fire for a field that was never excluded."""
    _write_manifest_and_family(write)
    profile = load_profile(profile_dir)
    corpus = load_corpus(profile, tmp_path)

    result = evaluate(parse_selector("family/[name=Alpha]/id"), profile, corpus)

    assert {point.record for point in result.points} == {"alpha"}
