"""Tests for bootstrap_lib.code_review.ledger.

Covers the three invariants the design calls out -- key normalization (anchor is
wording/case/line-number insensitive), baseline invalidation (a moved baseline
re-surfaces the finding), and SERIOUS-never-collapsed (a SERIOUS md-audit finding
is never stored so it can never produce a hit) -- plus the record/hits roundtrip
and the --ledger-record file entry point.
"""

import json

from bootstrap_lib.code_review import ledger


# ---------------------------------------------------------------------------
# key normalization
# ---------------------------------------------------------------------------


class TestKeyNormalization:
    def test_anchor_ignores_line_numbers_and_casing(self):
        a = {
            "kind": "code_review", "file": "src/app.py", "reason": "bug",
            "description": "Null deref of items at line 42",
        }
        b = {
            "kind": "code_review", "file": "src/app.py", "reason": "bug",
            "description": "null DEREF of items at line 9001",
        }
        # Same finding, different embedded line number + casing -> same key
        # (pure-digit tokens are dropped, casing folded).
        assert ledger.key_for(a) == ledger.key_for(b)

    def test_anchor_drops_pure_digit_tokens(self):
        assert ledger.normalize_anchor("error 404 not 500 found") == "error not found"

    def test_anchor_reword_of_first_tokens_changes_key(self):
        a = {"kind": "code_review", "file": "f", "reason": "bug",
             "description": "off by one in the counter"}
        b = {"kind": "code_review", "file": "f", "reason": "bug",
             "description": "unterminated string in the counter"}
        assert ledger.key_for(a) != ledger.key_for(b)

    def test_file_case_and_slashes_normalized(self):
        a = {"kind": "md_audit", "file": "D:/Dev/X/CLAUDE.md",
             "criterion": "H-11", "taxonomy": "convention", "message": "same finding text here"}
        b = {"kind": "md_audit", "file": "d:\\dev\\x\\CLAUDE.md",
             "criterion": "h-11", "taxonomy": "Convention", "message": "same finding text here"}
        assert ledger.key_for(a) == ledger.key_for(b)

    def test_md_audit_and_code_review_never_collide(self):
        a = {"kind": "md_audit", "file": "f", "criterion": "c", "taxonomy": "t",
             "message": "one two three four"}
        b = {"kind": "code_review", "file": "f", "reason": "c",
             "description": "one two three four"}
        assert ledger.key_for(a) != ledger.key_for(b)

    def test_unknown_kind_raises(self):
        try:
            ledger.key_for({"kind": "mystery", "file": "f"})
            assert False, "expected ValueError"
        except ValueError:
            pass

    def test_normalize_anchor_caps_token_count(self):
        text = "a b c d e f g h i j k l"
        assert ledger.normalize_anchor(text, n=3) == "a b c"


# ---------------------------------------------------------------------------
# baseline invalidation
# ---------------------------------------------------------------------------


class TestBaselineInvalidation:
    def _finding(self):
        return {"kind": "code_review", "file": "src/a.py", "reason": "bug",
                "description": "null deref in handler path"}

    def test_hit_returned_when_baseline_matches(self, tmp_path):
        led = tmp_path / "ledger.json"
        ledger.record_declined(led, "CL1", "base-A", [self._finding()])
        hits = ledger.ledger_hits(led, "CL1", "base-A")
        assert len(hits) == 1
        assert hits[0]["verdict"] == "declined"
        assert hits[0]["kind"] == "code_review"

    def test_stale_baseline_hides_entry(self, tmp_path):
        led = tmp_path / "ledger.json"
        ledger.record_declined(led, "CL1", "base-A", [self._finding()])
        # A later run at a moved baseline sees no hit -> finding re-surfaces.
        assert ledger.ledger_hits(led, "CL1", "base-B") == []

    def test_record_prunes_stale_entries_for_change(self, tmp_path):
        led = tmp_path / "ledger.json"
        ledger.record_declined(led, "CL1", "base-A", [self._finding()])
        other = {"kind": "code_review", "file": "src/b.py", "reason": "bug",
                 "description": "unused import at top"}
        ledger.record_declined(led, "CL1", "base-B", [other])
        doc = json.loads(led.read_text(encoding="utf-8"))
        entries = doc["changes"]["CL1"]["entries"]
        # Only the base-B entry survives; the stale base-A entry was pruned.
        assert [e["baseline"] for e in entries] == ["base-B"]

    def test_unknown_change_id_has_no_hits(self, tmp_path):
        led = tmp_path / "ledger.json"
        assert ledger.ledger_hits(led, "nope", "any") == []


# ---------------------------------------------------------------------------
# SERIOUS never collapsed
# ---------------------------------------------------------------------------


class TestSeriousNeverCollapsed:
    def test_serious_md_audit_is_not_recorded(self, tmp_path):
        led = tmp_path / "ledger.json"
        serious = {"kind": "md_audit", "file": "CLAUDE.md", "criterion": "H-1",
                   "taxonomy": "accuracy", "message": "claims a nonexistent path",
                   "severity": "SERIOUS"}
        stored = ledger.record_declined(led, "CL1", "b", [serious])
        assert stored == 0
        assert ledger.ledger_hits(led, "CL1", "b") == []

    def test_non_serious_md_audit_is_recorded(self, tmp_path):
        led = tmp_path / "ledger.json"
        minor = {"kind": "md_audit", "file": "CLAUDE.md", "criterion": "H-1",
                 "taxonomy": "density", "message": "verbose phrasing here",
                 "severity": "MINOR"}
        stored = ledger.record_declined(led, "CL1", "b", [minor])
        assert stored == 1
        hits = ledger.ledger_hits(led, "CL1", "b")
        assert hits[0]["severity"] == "minor"

    def test_is_serious_only_for_md_audit(self):
        assert ledger.is_serious(
            {"kind": "md_audit", "severity": "serious"}) is True
        assert ledger.is_serious(
            {"kind": "code_review", "severity": "serious"}) is False


# ---------------------------------------------------------------------------
# record / dedup / roundtrip / file entry point
# ---------------------------------------------------------------------------


class TestRecordAndRoundtrip:
    def test_dedup_by_key(self, tmp_path):
        led = tmp_path / "ledger.json"
        f = {"kind": "code_review", "file": "f", "reason": "bug",
             "description": "same finding one two three"}
        ledger.record_declined(led, "CL1", "b", [f])
        stored = ledger.record_declined(led, "CL1", "b", [f])
        assert stored == 0  # already present
        assert len(ledger.ledger_hits(led, "CL1", "b")) == 1

    def test_label_defaults_to_trimmed_message(self, tmp_path):
        led = tmp_path / "ledger.json"
        f = {"kind": "code_review", "file": "f", "reason": "bug",
             "description": "  multiple   spaces   collapse  "}
        ledger.record_declined(led, "CL1", "b", [f])
        hit = ledger.ledger_hits(led, "CL1", "b")[0]
        assert hit["label"] == "multiple spaces collapse"

    def test_empty_bucket_dropped(self, tmp_path):
        led = tmp_path / "ledger.json"
        serious = {"kind": "md_audit", "file": "f", "criterion": "c",
                   "taxonomy": "t", "message": "x y z", "severity": "SERIOUS"}
        ledger.record_declined(led, "CL1", "b", [serious])
        doc = json.loads(led.read_text(encoding="utf-8"))
        assert "CL1" not in doc["changes"]

    def test_record_from_file(self, tmp_path):
        led = tmp_path / "ledger.json"
        payload = {
            "change_id": "42", "baseline": "base-X",
            "declined": [
                {"kind": "code_review", "file": "a.py", "reason": "bug",
                 "description": "one two three four five"},
                {"kind": "md_audit", "file": "CLAUDE.md", "criterion": "H-2",
                 "taxonomy": "accuracy", "message": "bad path claim",
                 "severity": "SERIOUS"},  # dropped
            ],
        }
        pfile = tmp_path / "declined.json"
        pfile.write_text(json.dumps(payload), encoding="utf-8")
        stored = ledger.record_from_file(led, pfile)
        assert stored == 1  # SERIOUS md_audit dropped
        assert len(ledger.ledger_hits(led, "42", "base-X")) == 1

    def test_record_from_file_requires_change_id_and_baseline(self, tmp_path):
        led = tmp_path / "ledger.json"
        pfile = tmp_path / "bad.json"
        pfile.write_text(json.dumps({"declined": []}), encoding="utf-8")
        try:
            ledger.record_from_file(led, pfile)
            assert False, "expected ValueError"
        except ValueError:
            pass

    def test_load_tolerates_corruption(self, tmp_path):
        led = tmp_path / "ledger.json"
        led.write_text("{not json", encoding="utf-8")
        assert ledger.load_ledger(led) == ledger.default_ledger()

    def test_hits_ignore_malformed_change_buckets_and_entries(self, tmp_path):
        malformed = [
            {"version": 1, "changes": {"CL1": []}},
            {"version": 1, "changes": {"CL1": {"entries": None}}},
            {"version": 1, "changes": {"CL1": {"entries": 7}}},
        ]
        for index, document in enumerate(malformed):
            led = tmp_path / f"ledger-{index}.json"
            led.write_text(json.dumps(document), encoding="utf-8")
            assert ledger.ledger_hits(led, "CL1", "base") == []

    def test_baseline_token_is_deterministic(self):
        a = ledger.baseline_token({"shelf": {"//d/x": "abc"}, "actions": {}})
        b = ledger.baseline_token({"actions": {}, "shelf": {"//d/x": "abc"}})
        assert a == b  # key order independent
        c = ledger.baseline_token({"shelf": {"//d/x": "def"}, "actions": {}})
        assert a != c
