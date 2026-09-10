"""Tests for bootstrap_lib.code_review.lane_prompts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from bootstrap_lib.code_review import lane_prompts as lp
from bootstrap_lib.code_review import review_profiles as rp


class TestModelClassification:
    @pytest.mark.parametrize("alias", ["sonnet", "opus", "haiku", "fable"])
    def test_agent_aliases_are_agent_path(self, alias: str) -> None:
        assert lp.is_agent_alias(alias)

    def test_surrounding_whitespace_is_tolerated(self) -> None:
        assert lp.is_agent_alias("  opus  ")

    @pytest.mark.parametrize(
        "value", ["my-local-endpoint", "qwen", "openrouter", "sonnett", "Opus"]
    )
    def test_everything_else_is_an_endpoint_id(self, value: str) -> None:
        """A typo must fall to the endpoint path and fail loudly there.

        The alias set is closed precisely so 'sonnett' cannot quietly launch
        some default Agent -- it becomes an unknown endpoint id instead, which
        the runner reports by name.
        """
        assert not lp.is_agent_alias(value)


class TestShippedDefaultsPreserveAgentDispatch:
    def test_every_shipped_model_is_an_agent_alias(self, tmp_path: Path) -> None:
        """The no-override path must dispatch exactly as it did before.

        This is the compatibility guarantee the whole feature rests on: if a
        shipped default ever became an endpoint id, every user would silently
        start routing reviews off the Agent tool.

        The shipped `code` profile states reviewer_c as a priority list, so the
        table is resolved first -- with a stub owner that reports no seat, which
        is the no-peer path. Each resolved value is asserted to be a STRING
        before it reaches is_agent_alias: a list must never get this far, since
        every downstream consumer (is_agent_alias, the lane runner) reads one
        model per lane.
        """
        config, _provenance = rp.resolve_config(tmp_path / "project", home=tmp_path / "home")
        config, _disclosures, _diagnostics = rp.apply_model_priority(
            config, discover=lambda self_ref, **kwargs: None
        )
        for profile in config["profiles"]:
            for reviewer in profile["reviewers"]:
                assert isinstance(reviewer["model"], str), (
                    f"resolved {profile['id']}.{reviewer['name']} is not a string"
                )
                assert lp.is_agent_alias(reviewer["model"]), (
                    f"shipped {profile['id']}.{reviewer['name']} is not an Agent alias"
                )
            for reason, model in profile["validator_models"].items():
                assert isinstance(model, str), (
                    f"shipped {profile['id']}.validator_models.{reason} is not a string"
                )
                assert lp.is_agent_alias(model), (
                    f"shipped {profile['id']}.validator_models.{reason} is not an Agent alias"
                )


class TestParseIssueArray:
    def test_accepts_a_minimal_issue(self) -> None:
        text = json.dumps(
            [{"file": "a.py", "lines": "4", "reason": "bug", "description": "boom"}]
        )
        assert lp.parse_issue_array(text) == [
            {"file": "a.py", "lines": "4", "reason": "bug", "description": "boom"}
        ]

    def test_accepts_an_empty_array(self) -> None:
        assert lp.parse_issue_array("[]") == []

    def test_unwraps_a_code_fence(self) -> None:
        text = '```json\n[{"file": "a.py", "lines": "4", "reason": "bug", "description": "x"}]\n```'
        assert len(lp.parse_issue_array(text)) == 1

    def test_keeps_an_optional_citation(self) -> None:
        text = json.dumps(
            [
                {
                    "file": "a.py",
                    "lines": "4",
                    "reason": "claude_md",
                    "description": "x",
                    "citation": "use pathlib",
                }
            ]
        )
        assert lp.parse_issue_array(text)[0]["citation"] == "use pathlib"

    def test_empty_response_is_an_error_not_an_empty_result(self) -> None:
        """'said nothing' and 'found nothing' must not collapse together.

        Only the second may render as a clean review; treating the first as []
        would report an unreviewed chunk as clean.
        """
        with pytest.raises(lp.LaneOutputError, match="empty response"):
            lp.parse_issue_array("   ")

    def test_prose_is_rejected(self) -> None:
        with pytest.raises(lp.LaneOutputError, match="not valid JSON"):
            lp.parse_issue_array("I reviewed the diff and found no issues.")

    def test_object_instead_of_array_is_rejected(self) -> None:
        with pytest.raises(lp.LaneOutputError, match="must be a JSON array"):
            lp.parse_issue_array('{"file": "a.py"}')

    def test_missing_required_field_is_rejected(self) -> None:
        with pytest.raises(lp.LaneOutputError, match="missing required field 'lines'"):
            lp.parse_issue_array('[{"file": "a.py", "reason": "bug", "description": "x"}]')

    def test_unknown_field_is_rejected(self) -> None:
        text = json.dumps(
            [
                {
                    "file": "a.py",
                    "lines": "4",
                    "reason": "bug",
                    "description": "x",
                    "severity": "high",
                }
            ]
        )
        with pytest.raises(lp.LaneOutputError, match="unknown field"):
            lp.parse_issue_array(text)

    def test_unknown_reason_is_rejected(self) -> None:
        text = json.dumps(
            [{"file": "a.py", "lines": "4", "reason": "style", "description": "x"}]
        )
        with pytest.raises(lp.LaneOutputError, match="reason must be one of"):
            lp.parse_issue_array(text)

    def test_non_string_field_is_rejected(self) -> None:
        text = json.dumps(
            [{"file": "a.py", "lines": 4, "reason": "bug", "description": "x"}]
        )
        with pytest.raises(lp.LaneOutputError, match="lines must be a string"):
            lp.parse_issue_array(text)

    def test_blank_required_field_is_rejected(self) -> None:
        text = json.dumps(
            [{"file": "a.py", "lines": "4", "reason": "bug", "description": "  "}]
        )
        with pytest.raises(lp.LaneOutputError, match="description must not be empty"):
            lp.parse_issue_array(text)


class TestBuildUserMessage:
    def test_inlines_the_diff_and_context(self) -> None:
        message = lp.build_user_message(
            "reviewer_b_diff_only_bugs",
            diff_text="diff --git a/x b/x",
            files=["x"],
            description="add x",
        )
        assert "diff --git a/x b/x" in message
        assert "- x" in message
        assert "add x" in message

    def test_omits_absent_optional_context(self) -> None:
        message = lp.build_user_message(
            "reviewer_b_diff_only_bugs", diff_text="d"
        )
        assert "Files in this chunk" not in message
        assert "Change description" not in message

    def test_unknown_lane_raises(self) -> None:
        with pytest.raises(KeyError):
            lp.build_user_message("reviewer_z", diff_text="d")

    def test_claimed_files_are_listed_by_path(self) -> None:
        """A claimed file is absent from the chunk, so its PATH is the whole signal.

        Without it the lane reads the change as though the Markdown were never
        touched, and reports a docs-currency rule as violated when the update is
        present in a file it was not shown.
        """
        message = lp.build_user_message(
            "reviewer_a_claude_md_compliance",
            diff_text="diff --git a/x.py b/x.py",
            files=["x.py"],
            claimed_files=["docs/architecture.md"],
        )
        assert "Also changed in this review" in message
        assert "- docs/architecture.md" in message

    def test_claimed_files_section_is_omitted_when_empty(self) -> None:
        message = lp.build_user_message(
            "reviewer_a_claude_md_compliance", diff_text="d", files=["x.py"]
        )
        assert "Also changed in this review" not in message

    def test_reviewer_a_is_told_not_to_report_a_listed_path_as_missing(self) -> None:
        """The prompt half of the fix -- the list alone does not say what to do with it."""
        system = lp.LANE_PROMPTS["reviewer_a_claude_md_compliance"].system
        assert "Also changed in this review" in system
        assert "never report that" in system


class TestLaneEligibility:
    """Which lanes may carry an endpoint id, and what a backend must be."""

    @pytest.mark.parametrize(
        "lane",
        [
            "reviewer_a_claude_md_compliance",
            "reviewer_b_diff_only_bugs",
            "reviewer_c_introduced_code",
        ],
    )
    def test_every_reviewer_is_eligible(self, lane: str) -> None:
        assert lane in lp.ENDPOINT_ELIGIBLE_LANES

    def test_the_validator_is_not_eligible(self) -> None:
        """The control that suppresses a weak reviewer's noise stays native.

        Weakening the reviewer and the instrument that measures it in the same
        run makes a regression unattributable.
        """
        assert "validator" in lp.KNOWN_LANES
        assert "validator" not in lp.ENDPOINT_ELIGIBLE_LANES

    def test_eligibility_is_bounded_by_the_known_lanes(self) -> None:
        assert lp.ENDPOINT_ELIGIBLE_LANES <= lp.KNOWN_LANES

    def test_the_repo_reading_lanes_need_an_agent_loop(self) -> None:
        """A lane whose prompt sends it to open files cannot be a completion.

        Reviewer A reads the governing CLAUDE.md files and reviewer C reads the
        changed files; neither text is inlined into the user message, so a
        transport-kind endpoint would review a context it cannot fetch.
        """
        assert lp.LANES_REQUIRING_AGENT_LOOP == {
            "reviewer_a_claude_md_compliance",
            "reviewer_c_introduced_code",
        }

    def test_the_diff_only_lane_does_not_need_an_agent_loop(self) -> None:
        assert "reviewer_b_diff_only_bugs" not in lp.LANES_REQUIRING_AGENT_LOOP

    def test_agent_loop_lanes_are_eligible(self) -> None:
        """Otherwise the backend-kind guard guards a lane nothing can reach."""
        assert lp.LANES_REQUIRING_AGENT_LOOP <= lp.ENDPOINT_ELIGIBLE_LANES


class TestPromptContent:
    def test_reviewer_b_prompt_carries_the_guardrails(self) -> None:
        assert lp.GUARDRAILS in lp.REVIEWER_B_SYSTEM
        assert lp.OUTPUT_INSTRUCTION in lp.REVIEWER_B_SYSTEM

    @pytest.mark.parametrize("lane", sorted(lp.ENDPOINT_ELIGIBLE_LANES))
    def test_every_eligible_prompt_carries_the_shared_contract(self, lane: str) -> None:
        """One guardrail set and one output contract, whoever serves the lane."""
        system = lp.LANE_PROMPTS[lane].system
        assert lp.GUARDRAILS in system
        assert lp.OUTPUT_INSTRUCTION in system

    def test_the_diff_only_prompt_forbids_reading_anything(self) -> None:
        """It is the one lane a plain completion may serve, so it must not ask."""
        assert "everything you get" in lp.REVIEWER_B_SYSTEM

    @pytest.mark.parametrize("lane", sorted(lp.LANES_REQUIRING_AGENT_LOOP))
    def test_an_agent_loop_prompt_says_what_to_read(self, lane: str) -> None:
        assert "read" in lp.LANE_PROMPTS[lane].system.lower()

    def test_every_prompt_is_ascii(self) -> None:
        """Rendered into two tracked SKILL.md files, which are ASCII-only."""
        for lane, prompt in lp.LANE_PROMPTS.items():
            prompt.system.encode("ascii"), lane

    def test_endpoint_eligible_lanes_all_have_prompts(self) -> None:
        """The "eligible but has no canonical prompt" guard must stay unreachable."""
        assert lp.ENDPOINT_ELIGIBLE_LANES <= set(lp.LANE_PROMPTS)

    @pytest.mark.parametrize("lane", sorted(lp.ENDPOINT_ELIGIBLE_LANES))
    def test_every_eligible_lane_builds_a_user_message(self, lane: str) -> None:
        message = lp.build_user_message(lane, diff_text="UNIQUE-DIFF", files=["a.py"])
        assert "UNIQUE-DIFF" in message
        assert "a.py" in message

    def test_rendered_prompt_survives_yaml_round_trip(self) -> None:
        """It is emitted into SKILL.md as a block scalar; it must read back."""
        document = yaml.safe_dump({"canonical_prompt": lp.REVIEWER_B_SYSTEM})
        assert yaml.safe_load(document)["canonical_prompt"] == lp.REVIEWER_B_SYSTEM


# ---------------------------------------------------------------------------
# Pre-computed mechanical findings in the lane's user message
# ---------------------------------------------------------------------------


class TestMechanicalFindingsInPrompt:
    _HIT = {
        "file": "config/rows.yaml",
        "line": 11,
        "check": "non_ascii",
        "detail": "U+2014 in: a row",
    }

    def test_no_system_prompt_asserts_a_scan_section(self):
        """The claim "a scan already ran, stop looking" is only true for a
        caller that passes findings, and the two dispatch paths do not adopt
        the scan in lockstep. A system prompt carrying it would be FALSE for
        any caller that had not started passing them, and would license a lane
        to skip a check nothing ran. Move the preamble into a system prompt
        and this goes red.
        """
        for name in (
            "REVIEWER_A_SYSTEM",
            "REVIEWER_B_SYSTEM",
            "REVIEWER_C_SYSTEM",
        ):
            assert "Already checked mechanically" not in getattr(lp, name), name

    def test_a_caller_passing_nothing_makes_no_claim(self):
        msg = lp.build_user_message(
            "reviewer_b_diff_only_bugs", diff_text="@@ -1 +1 @@\n+x"
        )
        assert "Already checked mechanically" not in msg
        assert "Mechanical scan" not in msg

    def test_an_empty_scan_is_stated_rather_than_omitted(self):
        """Absent and silent must differ. A lane that cannot tell a clean scan
        from no scan has to re-scan to be safe, which is the duplicated work
        this removes."""
        msg = lp.build_user_message(
            "reviewer_b_diff_only_bugs", diff_text="d", mechanical_findings=[]
        )
        assert "Already checked mechanically" in msg
        assert "non_ascii (non-ASCII characters)" in msg
        assert "abs_path (absolute paths)" in msg
        assert "Findings: none for the checks listed above" in msg

    def test_findings_are_rendered_with_file_line_and_check(self):
        msg = lp.build_user_message(
            "reviewer_a_claude_md_compliance",
            diff_text="d",
            mechanical_findings=[self._HIT],
        )
        assert "- config/rows.yaml:11 [non_ascii] U+2014 in: a row" in msg

    def test_findings_are_sorted_by_file_then_line(self):
        msgs = lp.format_mechanical_findings(
            [
                {"file": "b.yaml", "line": 2, "check": "abs_path", "detail": "x"},
                {"file": "a.yaml", "line": 9, "check": "non_ascii", "detail": "y"},
                {"file": "a.yaml", "line": 3, "check": "non_ascii", "detail": "z"},
            ]
        )
        # Parse only the rows BELOW the scan header -- the preamble above it
        # is a bulleted list too, and would otherwise be read as findings.
        body = msgs.split("Mechanical scan (added lines only):", 1)[1]
        order = [line.split()[1] for line in body.splitlines() if line.startswith("  - ")]
        assert order == ["a.yaml:3", "a.yaml:9", "b.yaml:2"]

    def test_v2_coverage_is_derived_and_scoped_per_file(self):
        msg = lp.format_mechanical_findings(
            {
                "schema_version": 2,
                "files": [
                    {
                        "file": "a.yaml",
                        "checks_run": ["non_ascii"],
                        "findings": [],
                    },
                    {"file": "b.jsonc", "checks_run": [], "findings": []},
                ],
            }
        )
        assert "Checks run: non_ascii (non-ASCII characters)" in msg
        assert "abs_path (absolute paths)" not in msg
        assert "File: b.jsonc\n  Checks run: none (no mechanical coverage" in msg
        assert "file/check pair" in msg

    def test_the_preamble_tells_the_lane_the_scan_does_not_adjudicate(self):
        """Detection is the script's and adjudication the reviewer's. Without
        this the lane reports every hit as a violation, which trades a
        probabilistic answer for a confidently wrong one -- this repo permits
        box-drawing characters inside a diagram."""
        text = lp.MECHANICAL_PREAMBLE
        assert "detects; it does not decide" in text
        assert 'file/check pair listed under "Checks run"' in text
        assert "restriction does not apply to a check omitted for that file" in text
