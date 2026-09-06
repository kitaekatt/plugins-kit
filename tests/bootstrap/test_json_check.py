"""Tests for plugins/bootstrap/lib/json_check.py."""

import json

import pytest

from bootstrap_lib.json_check import check_json_entries, merge_json_entries


class TestCheckJsonEntries:
    def test_matching_fields(self, tmp_path):
        ref = tmp_path / "ref.json"
        target = tmp_path / "target.json"
        ref.write_text(json.dumps({"entry": {"source": "local", "path": "/foo"}}))
        target.write_text(json.dumps({"entry": {"source": "local", "path": "/foo", "extra": True}}))

        result = check_json_entries(str(ref), str(target), ["source", "path"])
        assert result.passed is True

    def test_nested_changed_field_fails(self, tmp_path):
        ref = tmp_path / "ref.json"
        target = tmp_path / "target.json"
        ref.write_text(json.dumps({"entry": {"source": "local", "path": "/foo"}}))
        target.write_text(json.dumps({"entry": {"source": "local", "path": "/bar"}}))

        result = check_json_entries(str(ref), str(target), ["source", "path"])

        assert result.passed is False
        assert "path" in result.message

    def test_mismatched_field(self, tmp_path):
        ref = tmp_path / "ref.json"
        target = tmp_path / "target.json"
        # Library expects nested JSON: top-level keys are entry names, merge_fields are sub-fields
        ref.write_text(json.dumps({"entry1": {"source": "local"}}))
        target.write_text(json.dumps({"entry1": {"source": "remote"}}))

        result = check_json_entries(str(ref), str(target), ["source"])
        assert result.passed is False
        assert "source" in result.message

    def test_target_missing(self, tmp_path):
        ref = tmp_path / "ref.json"
        ref.write_text(json.dumps({"source": "local"}))

        result = check_json_entries(str(ref), str(tmp_path / "missing.json"), ["source"])
        assert result.passed is False
        assert "does not exist" in result.message

    def test_reference_missing(self, tmp_path):
        target = tmp_path / "target.json"
        target.write_text(json.dumps({"source": "local"}))

        result = check_json_entries(str(tmp_path / "missing.json"), str(target), ["source"])
        assert result.passed is False
        assert "reference" in result.message


class TestMergeJsonEntries:
    def test_merge_creates_target(self, tmp_path):
        ref = tmp_path / "ref.json"
        target = tmp_path / "out" / "target.json"
        # Library expects nested JSON: top-level keys are entry names
        ref.write_text(json.dumps({"my-market": {"source": "local", "autoUpdate": True}}))

        result = merge_json_entries(str(ref), str(target), ["source", "autoUpdate"])
        assert result.passed is True
        assert target.is_file()

        data = json.loads(target.read_text())
        assert data["my-market"]["source"] == "local"
        assert data["my-market"]["autoUpdate"] is True

    def test_merge_preserves_fields(self, tmp_path):
        ref = tmp_path / "ref.json"
        target = tmp_path / "target.json"
        ref.write_text(json.dumps({"my-market": {"source": "local", "autoUpdate": True}}))
        target.write_text(json.dumps({"my-market": {"source": "remote", "lastUpdated": "2026-01-01"}}))

        result = merge_json_entries(
            str(ref), str(target),
            merge_fields=["source", "autoUpdate"],
            preserve_fields=["lastUpdated"],
        )
        assert result.passed is True

        data = json.loads(target.read_text())
        assert data["my-market"]["source"] == "local"  # Merged from ref
        assert data["my-market"]["autoUpdate"] is True  # Merged from ref
        assert data["my-market"]["lastUpdated"] == "2026-01-01"  # Preserved from target

    def test_deep_merge_dicts(self, tmp_path):
        ref = tmp_path / "ref.json"
        target = tmp_path / "target.json"
        # Top-level key "my-market" is the entry, "plugins" is a sub-field that's a dict
        ref.write_text(json.dumps({"my-market": {"plugins": {"a": True, "b": True}}}))
        target.write_text(json.dumps({"my-market": {"plugins": {"c": True}}}))

        result = merge_json_entries(str(ref), str(target), ["plugins"])
        assert result.passed is True

        data = json.loads(target.read_text())
        # merge_fields copies entire value from ref — ref's plugins replaces target's
        assert data["my-market"]["plugins"]["a"] is True
        assert data["my-market"]["plugins"]["b"] is True

    def test_reference_missing(self, tmp_path):
        target = tmp_path / "target.json"
        result = merge_json_entries(str(tmp_path / "missing.json"), str(target), ["source"])
        assert result.passed is False

    @pytest.mark.parametrize("content", ["not valid json", "[]", "null", "1"])
    def test_unreadable_target_is_not_overwritten(self, tmp_path, content):
        ref = tmp_path / "ref.json"
        target = tmp_path / "target.json"
        ref.write_text(json.dumps({"my-market": {"source": "local"}}))
        target.write_text(content)
        before = target.read_bytes()

        result = merge_json_entries(str(ref), str(target), ["source"])

        assert result.passed is False
        assert target.read_bytes() == before

    def test_scalar_target_entry_fails_without_exception(self, tmp_path):
        ref = tmp_path / "ref.json"
        target = tmp_path / "target.json"
        ref.write_text(json.dumps({"my-market": {"source": "local"}}))
        target.write_text(json.dumps({"my-market": "scalar"}))

        result = merge_json_entries(str(ref), str(target), ["source"])

        assert result.passed is False

    def test_preserve_fields_seed_defaults_for_new_entry(self, tmp_path):
        """A reference value for a preserve_field becomes the default when the
        target entry doesn't have it yet — required so freshly merged entries
        pass downstream schema validators (e.g. claude CLI marketplace schema
        requires installLocation/lastUpdated to be strings, not undefined)."""
        ref = tmp_path / "ref.json"
        target = tmp_path / "target.json"
        ref.write_text(json.dumps({
            "my-market": {
                "source": {"source": "git", "url": "https://example.com"},
                "autoUpdate": True,
                "installLocation": "",
                "lastUpdated": "",
            }
        }))
        # Target file does not exist yet — entry will be created from scratch.

        result = merge_json_entries(
            str(ref), str(target),
            merge_fields=["source", "autoUpdate"],
            preserve_fields=["installLocation", "lastUpdated"],
        )
        assert result.passed is True

        data = json.loads(target.read_text())
        entry = data["my-market"]
        assert entry["source"] == {"source": "git", "url": "https://example.com"}
        assert entry["autoUpdate"] is True
        assert entry["installLocation"] == ""
        assert entry["lastUpdated"] == ""

    def test_preserve_fields_keep_target_value_over_reference(self, tmp_path):
        """When target already has a preserve_field, reference defaults must
        not overwrite it."""
        ref = tmp_path / "ref.json"
        target = tmp_path / "target.json"
        ref.write_text(json.dumps({
            "my-market": {
                "source": "local",
                "installLocation": "",
                "lastUpdated": "",
            }
        }))
        target.write_text(json.dumps({
            "my-market": {
                "source": "remote",
                "installLocation": "/real/path",
                "lastUpdated": "2026-04-27",
            }
        }))

        result = merge_json_entries(
            str(ref), str(target),
            merge_fields=["source"],
            preserve_fields=["installLocation", "lastUpdated"],
        )
        assert result.passed is True

        entry = json.loads(target.read_text())["my-market"]
        assert entry["source"] == "local"
        assert entry["installLocation"] == "/real/path"
        assert entry["lastUpdated"] == "2026-04-27"

    def test_preserve_fields_seed_defaults_for_partial_existing_entry(self, tmp_path):
        """When target entry exists but is missing required preserve_fields
        (e.g. user has the bad partial entry from older bootstrap), the next
        merge backfills the defaults from reference."""
        ref = tmp_path / "ref.json"
        target = tmp_path / "target.json"
        ref.write_text(json.dumps({
            "my-market": {
                "source": "local",
                "autoUpdate": True,
                "installLocation": "",
                "lastUpdated": "",
            }
        }))
        # Partial target — missing installLocation & lastUpdated entirely.
        target.write_text(json.dumps({
            "my-market": {"source": "local", "autoUpdate": True}
        }))

        result = merge_json_entries(
            str(ref), str(target),
            merge_fields=["source", "autoUpdate"],
            preserve_fields=["installLocation", "lastUpdated"],
        )
        assert result.passed is True

        entry = json.loads(target.read_text())["my-market"]
        assert entry["installLocation"] == ""
        assert entry["lastUpdated"] == ""
