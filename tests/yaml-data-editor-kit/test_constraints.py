"""``constraints`` -- covers, matches_files and unique."""

from yaml_data_editor_kit.schema import errors_only, load_profile, validate_corpus

COVERS_PROFILE = """
dialect: type/1
id: measure
identified_by: id
fields:
  id: { type: id }
constraints:
  - kind: covers
    from: measure.id
    to: measure_cost.id
    both_ways: true
    why: "every measure must be priced and every price must name a real measure"
---
dialect: source/1
of: measure
layout: rows
path: content/measures.yaml
---
dialect: type/1
id: measure_cost
identified_by: id
fields:
  id:     { type: id }
  amount: { type: int }
---
dialect: source/1
of: measure_cost
layout: rows
path: content/measure_costs.yaml
"""


def test_covers_passes_when_both_sets_agree(tmp_path, profile_dir, write) -> None:
    write("profile/measure.yaml", COVERS_PROFILE)
    write("content/measures.yaml", "- { id: mass }\n- { id: length }\n")
    write("content/measure_costs.yaml", "- { id: mass, amount: 1 }\n- { id: length, amount: 2 }\n")
    profile = load_profile(profile_dir)
    assert errors_only(validate_corpus(profile, tmp_path)) == []


def test_covers_names_the_record_that_has_no_counterpart(tmp_path, profile_dir, write) -> None:
    write("profile/measure.yaml", COVERS_PROFILE)
    write("content/measures.yaml", "- { id: mass }\n- { id: length }\n")
    write("content/measure_costs.yaml", "- { id: mass, amount: 1 }\n")
    profile = load_profile(profile_dir)
    problems = errors_only(validate_corpus(profile, tmp_path))
    assert len(problems) == 1
    assert problems[0].file == "content/measures.yaml"
    assert problems[0].record == "length"
    assert problems[0].field == "measure.id"
    assert "every measure must be priced" in problems[0].message


def test_both_ways_checks_the_reverse_direction_too(tmp_path, profile_dir, write) -> None:
    write("profile/measure.yaml", COVERS_PROFILE)
    write("content/measures.yaml", "- { id: mass }\n")
    write("content/measure_costs.yaml", "- { id: mass, amount: 1 }\n- { id: volume, amount: 3 }\n")
    profile = load_profile(profile_dir)
    problems = errors_only(validate_corpus(profile, tmp_path))
    reverse = [p for p in problems if p.field == "measure_cost.id"]
    assert len(reverse) == 1
    assert reverse[0].file == "content/measure_costs.yaml"
    assert reverse[0].record == "volume"


MATCHES_FILES_PROFILE = """
dialect: type/1
id: manifest
identified_by: id
fields:
  id:    { type: id }
  names: { type: list, of: { type: string } }
constraints:
  - kind: matches_files
    ids: manifest.names
    files: "templates/*.yaml"
    why: "the manifest list and the directory must not drift"
---
dialect: source/1
of: manifest
layout: single
path: content/manifest.yaml
"""


def test_matches_files_passes_when_the_set_equals_the_glob(tmp_path, profile_dir, write) -> None:
    write("profile/manifest.yaml", MATCHES_FILES_PROFILE)
    write("content/manifest.yaml", "id: main\nnames: [alpha, beta]\n")
    write("templates/alpha.yaml", "name: alpha\n")
    write("templates/beta.yaml", "name: beta\n")
    profile = load_profile(profile_dir)
    assert errors_only(validate_corpus(profile, tmp_path)) == []


def test_matches_files_reports_a_declared_id_with_no_file(tmp_path, profile_dir, write) -> None:
    write("profile/manifest.yaml", MATCHES_FILES_PROFILE)
    write("content/manifest.yaml", "id: main\nnames: [alpha, beta]\n")
    write("templates/alpha.yaml", "name: alpha\n")
    profile = load_profile(profile_dir)
    problems = errors_only(validate_corpus(profile, tmp_path))
    assert len(problems) == 1
    assert problems[0].file == "content/manifest.yaml"
    assert problems[0].field == "manifest.names"
    assert "'beta' matches no file" in problems[0].message
    assert "must not drift" in problems[0].message


def test_matches_files_reports_a_file_no_id_names(tmp_path, profile_dir, write) -> None:
    write("profile/manifest.yaml", MATCHES_FILES_PROFILE)
    write("content/manifest.yaml", "id: main\nnames: [alpha]\n")
    write("templates/alpha.yaml", "name: alpha\n")
    write("templates/gamma.yaml", "name: gamma\n")
    profile = load_profile(profile_dir)
    problems = errors_only(validate_corpus(profile, tmp_path))
    assert len(problems) == 1
    assert problems[0].file == "templates/gamma.yaml"
    assert problems[0].record == "gamma"
    assert "named by no value" in problems[0].message


UNIQUE_PROFILE = """
dialect: type/1
id: label
identified_by: id
fields:
  id: { type: id }
constraints:
  - kind: unique
    ids: label.id
    why: "two labels sharing an id make every reference ambiguous"
---
dialect: source/1
of: label
layout: rows
path: content/labels.yaml
"""


def test_unique_passes_on_a_set_with_no_duplicates(tmp_path, profile_dir, write) -> None:
    write("profile/label.yaml", UNIQUE_PROFILE)
    write("content/labels.yaml", "- { id: metal }\n- { id: plastic }\n")
    profile = load_profile(profile_dir)
    assert errors_only(validate_corpus(profile, tmp_path)) == []


def test_unique_names_the_duplicated_value(tmp_path, profile_dir, write) -> None:
    write("profile/label.yaml", UNIQUE_PROFILE)
    write("content/labels.yaml", "- { id: metal }\n- { id: metal }\n")
    profile = load_profile(profile_dir)
    problems = errors_only(validate_corpus(profile, tmp_path))
    assert len(problems) == 1
    assert problems[0].file == "content/labels.yaml"
    assert problems[0].record == "metal"
    assert problems[0].field == "label.id"
    assert "appears more than once" in problems[0].message
    assert "ambiguous" in problems[0].message
