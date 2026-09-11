"""Tests for p4-kit scripts/prepare_review.py."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, get_type_hints
from unittest.mock import call, patch

import pytest

import prepare_review as pr


def test_rendered_p4_skill_discloses_shelf_drift():
    skill = Path("plugins/p4-kit/skills/p4-code-review/SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "shelf_drift" in skill
    assert "disclose each depot path" in skill


def test_rendered_p4_skill_discloses_foreign_change():
    skill = Path("plugins/p4-kit/skills/p4-code-review/SKILL.md").read_text(
        encoding="utf-8"
    )
    assert (
        "When `bundle.foreign_change` is present, disclose the author and foreign client"
        in skill
    )


def test_rendered_p4_skill_retries_foreign_claim_without_claim():
    skill = Path("plugins/p4-kit/skills/p4-code-review/SKILL.md").read_text(
        encoding="utf-8"
    )
    assert (
        "If prepare reports that the CL belongs to a foreign client, re-run once "
        "without `--claim` and use that bundle."
        in skill
    )


def test_rendered_p4_skill_uses_reopen_for_default_open_in_step_three():
    skill = Path("plugins/p4-kit/skills/p4-code-review/SKILL.md").read_text(
        encoding="utf-8"
    )
    step_three = skill.split("        - n: 3\n", 1)[1].split(
        "        - n: 4\n", 1
    )[0]
    assert (
        "For `bundle.default_open`, use `p4 reopen -c <CL> <local-paths>`"
        in step_three
    )


def _concat_diff_from_chunks(bundle: dict) -> str:
    """Read all chunk files for a bundle and concatenate -- the historical
    bundle["diff"] string, reconstructed from on-disk chunks.

    Tests that used to assert `bundle["diff"]` should call this instead.
    """
    bundle_dir = Path(bundle["bundle_dir"])
    return "".join(
        (bundle_dir / entry["path"]).read_text(encoding="utf-8")
        for entry in bundle["diff_chunks"]
    )


# ---------------------------------------------------------------------------
# run_p4 -- subprocess invocation
# ---------------------------------------------------------------------------


class TestRunP4:
    def test_forces_utf8_decoding(self):
        """On Windows, default text decoding is cp1252 -- CJK bytes abort the reader.

        `run_p4` must pin encoding to utf-8 with errors='replace' so diffs with
        non-Latin-1 content (CJK, emoji) decode cleanly on any platform.
        """
        captured: dict = {}

        def fake_run(cmd, **kwargs):
            captured.update(kwargs)
            result = subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")
            return result

        with patch.object(subprocess, "run", side_effect=fake_run):
            pr.run_p4(["describe", "-du", "123"])

        assert captured.get("encoding") == "utf-8"
        assert captured.get("errors") == "replace"
        assert captured.get("capture_output") is True

    def test_coalesces_none_stdout_to_empty_string(self):
        """If the subprocess produced no output, callers should see '' not None."""
        fake = subprocess.CompletedProcess(["p4"], 0, stdout=None, stderr=None)
        with patch.object(subprocess, "run", return_value=fake):
            rc, out, err = pr.run_p4(["info"])
        assert rc == 0
        assert out == ""
        assert err == ""

    def test_replaces_invalid_utf8_bytes(self):
        """A real child emits invalid UTF-8 so replacement is observable."""
        payload = b"diff: \xff\n"
        expected = "diff: \ufffd\n"
        expected_command = ["p4", "describe", "-du", "1"]
        real_run = subprocess.run

        def run_python(
            cmd: list[str], **kwargs: Any
        ) -> subprocess.CompletedProcess[str]:
            if cmd != expected_command:
                raise AssertionError(f"unexpected command: {cmd!r}")
            emit = (
                "import sys; sys.stdout.buffer.write(bytes.fromhex("
                f"{payload.hex()!r}))"
            )
            return real_run([sys.executable, "-c", emit], **kwargs)

        with patch.object(subprocess, "run", side_effect=run_python):
            result = pr.run_p4(["describe", "-du", "1"])

        assert result == (0, expected, "")


    def test_timeout_reads_p4kit_vcs_timeout_s_env_var(self, monkeypatch):
        """run_p4 shares P4KIT_VCS_TIMEOUT_S with p4kit_vcs's own adapter --
        one knob covers every p4 subprocess this plugin spawns."""
        monkeypatch.setenv("P4KIT_VCS_TIMEOUT_S", "5")
        captured: dict = {}

        def fake_run(cmd, **kwargs):
            captured.update(kwargs)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with patch.object(subprocess, "run", side_effect=fake_run):
            pr.run_p4(["info"])

        assert captured.get("timeout") == 5.0

    def test_timeout_falls_back_to_the_default_when_env_var_is_garbage(
        self, monkeypatch
    ):
        monkeypatch.setenv("P4KIT_VCS_TIMEOUT_S", "not-a-number")
        captured: dict = {}

        def fake_run(cmd, **kwargs):
            captured.update(kwargs)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with patch.object(subprocess, "run", side_effect=fake_run):
            pr.run_p4(["info"])

        assert captured.get("timeout") == 60.0

    def test_timeout_falls_back_to_the_default_when_env_var_is_unset(
        self, monkeypatch
    ):
        monkeypatch.delenv("P4KIT_VCS_TIMEOUT_S", raising=False)
        captured: dict = {}

        def fake_run(cmd, **kwargs):
            captured.update(kwargs)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with patch.object(subprocess, "run", side_effect=fake_run):
            pr.run_p4(["info"])

        assert captured.get("timeout") == 60.0


class TestBootstrapDependencyDiagnostics:
    @staticmethod
    def _run_prepare(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-S", str(Path(pr.__file__))],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

    def test_absent_bootstrap_reports_install_remedy(self, tmp_path):
        env = dict(os.environ)
        env["_BOOTSTRAP_GUARD_VENV_REEXEC"] = "1"
        env["PYTHONPATH"] = str(tmp_path)

        completed = self._run_prepare(env)

        assert completed.stderr == (
            "[p4-kit] the 'plugins-kit:bootstrap' plugin has not provisioned "
            "p4-kit's code review (missing: bootstrap_lib). Install/enable the "
            "bootstrap plugin and start a new session so it can build this "
            "plugin's dependencies, then retry.\n"
        )

    def test_manifest_requires_bootstrap_0113_contract_floor(self):
        manifest = json.loads(
            Path("plugins/p4-kit/bootstrap.json").read_text(encoding="utf-8")
        )

        assert manifest["requires_bootstrap"] == "0.113.0"

    @pytest.mark.parametrize(("error", "bootstrap_failure"), [
        ("ModuleNotFoundError(\"No module named 'markdown_it'\", name='markdown_it')", False),
        ("ImportError('broken third-party package', name='yaml')", False),
        ("ImportError('unclassified import failure')", False),
        ("ImportError('missing shared symbol', name='bootstrap_lib.code_review.pipeline')", True),
    ])
    def test_import_failure_diagnostics(
        self, tmp_path: Path, error: str, bootstrap_failure: bool,
    ) -> None:
        package = tmp_path / "bootstrap_lib" / "code_review"
        package.mkdir(parents=True)
        (package.parent / "__init__.py").write_text("", encoding="utf-8")
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "pipeline.py").write_text(f"raise {error}\n", encoding="utf-8")
        env = dict(os.environ, _BOOTSTRAP_GUARD_VENV_REEXEC="1", PYTHONPATH=str(tmp_path))

        completed = self._run_prepare(env)

        assert completed.returncode != 0
        assert ("Traceback" in completed.stderr) is not bootstrap_failure
        assert ("stale for" in completed.stderr) is bootstrap_failure
        assert ("claude plugin update" in completed.stderr) is bootstrap_failure

    def test_bootstrap_without_run_vcs_timeout_reports_update_remedy(self, tmp_path):
        bootstrap_package = tmp_path / "bootstrap_lib"
        code_review_package = bootstrap_package / "code_review"
        code_review_package.mkdir(parents=True)
        (bootstrap_package / "__init__.py").write_text("", encoding="utf-8")
        (code_review_package / "__init__.py").write_text("", encoding="utf-8")
        (bootstrap_package / "path_repair.py").write_text(
            "def repair_path() -> None:\n"
            "    return None\n",
            encoding="utf-8",
        )
        (code_review_package / "ledger.py").write_text("", encoding="utf-8")
        (code_review_package / "pipeline.py").write_text(
            "assemble_bundle = emit_bundle = matches_claim = None\n"
            "preimage_relpath = split_sections = None\n"
            "def run_vcs(executable: str, args: list[str], cwd: object = None) "
            "-> tuple[int, str, str]:\n"
            "    return 0, '', ''\n",
            encoding="utf-8",
        )
        env = dict(os.environ)
        env["_BOOTSTRAP_GUARD_VENV_REEXEC"] = "1"
        env["PYTHONPATH"] = str(tmp_path)

        completed = self._run_prepare(env)

        assert completed.stderr == (
            "[p4-kit] the installed 'plugins-kit:bootstrap' plugin is too old "
            "or stale for p4-kit's code review (requires bootstrap >= 0.113.0; "
            "missing: bootstrap_lib.code_review.pipeline.run_vcs(timeout=...), "
            "bootstrap_lib.code_review.mechanical_repository, "
            "bootstrap_lib.code_review.pipeline.assemble_bundle(mechanical_contract=...)). "
            "Run `claude plugin update bootstrap@plugins-kit`. Then start a new "
            "session and retry.\n"
        )


class TestZtagReaderCharacterization:
    @staticmethod
    def _response(
        expected_command: list[str], output: str
    ) -> Callable[[list[str]], tuple[int, str, str]]:
        def run(args: list[str]) -> tuple[int, str, str]:
            if args != expected_command:
                raise AssertionError(f"unexpected p4 command: {args!r}")
            return 0, output, ""

        return run

    def test_shelf_fingerprint_keeps_trailing_record(self):
        output = (
            "... depotFile //depot/a.cpp\n"
            "... digest AAAA\n"
            "... headAction edit\n\n"
            "... depotFile //depot/b.cpp\n"
            "... digest BBBB\n"
            "... action add\n"
        )
        command = ["-ztag", "fstat", "-Ol", "//...@=123"]
        with patch.object(pr, "run_p4", side_effect=self._response(command, output)):
            result = pr.fetch_shelf_fingerprint("123")
        assert result == pr.ShelfScanResult(
            digests={"//depot/a.cpp": "AAAA", "//depot/b.cpp": "BBBB"},
            actions={"//depot/a.cpp": "edit", "//depot/b.cpp": "add"},
        )

    def test_opened_files_keeps_trailing_record(self):
        output = (
            "... depotFile //depot/a.cpp\n"
            "... action edit\n\n"
            "... depotFile //depot/b.cpp\n"
            "... action add\n"
        )
        assert pr._parse_opened_files(output) == {
            "//depot/a.cpp": "edit",
            "//depot/b.cpp": "add",
        }

    def test_local_paths_keeps_trailing_record(self):
        output = (
            "... depotFile //depot/a.cpp\n"
            "... path /ws/a.cpp\n\n"
            "... depotFile //depot/b.cpp\n"
            "... path /ws/b.cpp\n"
        )
        depots = ["//depot/a.cpp", "//depot/b.cpp"]
        command = ["-ztag", "where", *depots]
        with patch.object(pr, "run_p4", side_effect=self._response(command, output)):
            result = pr.resolve_local_paths(depots)
        assert result == {
            "//depot/a.cpp": "/ws/a.cpp",
            "//depot/b.cpp": "/ws/b.cpp",
        }

    def test_local_paths_splits_records_without_blank_separator(self):
        output = (
            "... depotFile //depot/a.cpp\n"
            "... path /ws/a.cpp\n"
            "... depotFile //depot/b.cpp\n"
            "... path /ws/b.cpp\n"
        )
        depots = ["//depot/a.cpp", "//depot/b.cpp"]
        command = ["-ztag", "where", *depots]
        with patch.object(pr, "run_p4", side_effect=self._response(command, output)):
            result = pr.resolve_local_paths(depots)
        assert result == {
            "//depot/a.cpp": "/ws/a.cpp",
            "//depot/b.cpp": "/ws/b.cpp",
        }

    def test_reconcile_keeps_trailing_record(self):
        output = (
            "... depotFile //depot/a.cpp\n"
            "... clientFile /ws/a.cpp\n"
            "... action edit\n\n"
            "... depotFile //depot/b.cpp\n"
            "... clientFile /ws/b.cpp\n"
            "... action add\n"
        )
        assert pr._parse_reconcile_output(output) == [
            {"local": "/ws/a.cpp", "depot": "//depot/a.cpp", "action": "edit"},
            {"local": "/ws/b.cpp", "depot": "//depot/b.cpp", "action": "add"},
        ]

    def test_default_open_keeps_trailing_record(self):
        output = (
            "... depotFile //depot/a.cpp\n"
            "... clientFile /ws/a.cpp\n"
            "... action edit\n\n"
            "... depotFile //depot/b.cpp\n"
            "... clientFile /ws/b.cpp\n"
            "... action add\n"
        )
        assert pr._parse_default_open_output(output) == [
            {"local": "/ws/a.cpp", "depot": "//depot/a.cpp", "action": "edit"},
            {"local": "/ws/b.cpp", "depot": "//depot/b.cpp", "action": "add"},
        ]

    def test_default_open_splits_records_without_blank_separator(self):
        output = (
            "... depotFile //depot/a.cpp\n"
            "... clientFile /ws/a.cpp\n"
            "... action edit\n"
            "... depotFile //depot/b.cpp\n"
            "... clientFile /ws/b.cpp\n"
            "... action add\n"
        )
        assert pr._parse_default_open_output(output) == [
            {"local": "/ws/a.cpp", "depot": "//depot/a.cpp", "action": "edit"},
            {"local": "/ws/b.cpp", "depot": "//depot/b.cpp", "action": "add"},
        ]

    def test_unresolved_keeps_trailing_record(self):
        output = (
            "... clientFile /ws/a.cpp\n"
            "... toFile //depot/a.cpp\n"
            "... resolveType content\n\n"
            "... clientFile /ws/b.cpp\n"
            "... toFile //depot/b.cpp\n"
            "... fromFile //depot/source.cpp\n"
            "... resolveType branch\n"
        )
        command = ["-ztag", "resolve", "-n", "-c", "123"]
        with patch.object(pr, "run_p4", side_effect=self._response(command, output)):
            result = pr.find_unresolved("123")
        assert result == (
            [
                {
                    "local": "/ws/a.cpp",
                    "depot": "//depot/a.cpp",
                    "resolve_type": "content",
                    "from_file": "",
                },
                {
                    "local": "/ws/b.cpp",
                    "depot": "//depot/b.cpp",
                    "resolve_type": "branch",
                    "from_file": "//depot/source.cpp",
                },
            ],
            [],
        )

    def test_workspace_identity_keeps_trailing_record(self):
        output = (
            "... serverAddress perforce.example:1666\n\n"
            "... clientRoot /ws\n"
            "... clientName review-client\n"
        )
        command = ["-ztag", "info"]
        with patch.object(pr, "run_p4", side_effect=self._response(command, output)):
            result = pr.get_workspace_root()
        assert result == (Path("/ws"), "review-client")


# ---------------------------------------------------------------------------
# parse_description
# ---------------------------------------------------------------------------


class TestParseDescription:
    def test_single_line(self):
        out = (
            "Change 12345 by user@client on 2026/01/01 12:00:00 *pending*\n"
            "\n"
            "\tAdd inventory overflow check\n"
            "\n"
            "Affected files ...\n"
        )
        assert pr.parse_description(out) == "Add inventory overflow check"

    def test_multi_line(self):
        out = (
            "Change 12345 by user@client on 2026/01/01 12:00:00\n"
            "\n"
            "\tFix race in quest item pickup\n"
            "\t\n"
            "\tThe inventory lock was being released early.\n"
            "\n"
            "Affected files ...\n"
        )
        result = pr.parse_description(out)
        assert "Fix race in quest item pickup" in result
        assert "inventory lock was being released early" in result

    def test_no_description(self):
        assert pr.parse_description("") == ""


# ---------------------------------------------------------------------------
# parse_depot_files
# ---------------------------------------------------------------------------


class TestParseDepotFiles:
    def test_extracts_depot_paths(self):
        out = (
            "Differences ...\n"
            "\n"
            "==== //depot/foo/bar.cpp#3 (text) ====\n"
            "@@ -1,3 +1,4 @@\n"
            "==== //depot/foo/baz.h#1 (text) ====\n"
            "@@ -10,5 +10,6 @@\n"
        )
        assert pr.parse_depot_files(out) == [
            "//depot/foo/bar.cpp",
            "//depot/foo/baz.h",
        ]

    def test_no_files(self):
        assert pr.parse_depot_files("Change 1 ...\n") == []

    def test_ignores_non_header_lines(self):
        out = (
            "==== //depot/a.cpp#1 (text) ====\n"
            "+ ==== fake header in diff ====\n"
            "==== //depot/b.cpp#2 (text) ====\n"
        )
        assert pr.parse_depot_files(out) == ["//depot/a.cpp", "//depot/b.cpp"]


# ---------------------------------------------------------------------------
# parse_file_actions
# ---------------------------------------------------------------------------


class TestParseFileActions:
    def test_affected_files_section(self):
        out = (
            "Change 100 by u@c on 2026/01/01\n"
            "\n"
            "\tdesc\n"
            "\n"
            "Affected files ...\n"
            "\n"
            "... //depot/a.cpp#3 edit\n"
            "... //depot/new.py#1 add\n"
            "... //depot/gone.py#2 delete\n"
            "\n"
            "Differences ...\n"
            "==== //depot/a.cpp#3 (text) ====\n"
        )
        assert pr.parse_file_actions(out) == {
            "//depot/a.cpp": ("3", "edit"),
            "//depot/new.py": ("1", "add"),
            "//depot/gone.py": ("2", "delete"),
        }

    def test_shelved_files_section(self):
        out = (
            "Shelved files ...\n"
            "\n"
            "... //depot/x.py#1 add\n"
            "\n"
            "Differences ...\n"
        )
        assert pr.parse_file_actions(out) == {"//depot/x.py": ("1", "add")}

    def test_move_actions(self):
        out = (
            "Affected files ...\n"
            "... //depot/new.py#1 move/add\n"
            "... //depot/old.py#5 move/delete\n"
            "Differences ...\n"
        )
        assert pr.parse_file_actions(out) == {
            "//depot/new.py": ("1", "move/add"),
            "//depot/old.py": ("5", "move/delete"),
        }

    def test_empty(self):
        assert pr.parse_file_actions("") == {}


# ---------------------------------------------------------------------------
# split_diff_sections
# ---------------------------------------------------------------------------


class TestSplitDiffSections:
    def test_splits_by_file_header(self):
        diff = (
            "==== //depot/a.cpp#1 (text) ====\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
            "==== //depot/b.cpp#2 (text) ====\n"
            "@@ -5 +5 @@\n"
            "-foo\n"
            "+bar\n"
        )
        preamble, sections = pr.split_diff_sections(diff)
        assert preamble == ""
        assert len(sections) == 2
        assert sections[0]["depot"] == "//depot/a.cpp"
        assert sections[0]["rev"] == "1"
        assert "@@ -1 +1 @@" in sections[0]["body"]
        assert sections[1]["depot"] == "//depot/b.cpp"
        assert sections[1]["rev"] == "2"

    def test_empty_body_for_add(self):
        diff = (
            "==== //depot/edit.py#2 (text) ====\n"
            "@@ -1 +1 @@\n"
            "-a\n"
            "+b\n"
            "==== //depot/new.py#1 (text) ====\n"
            "\n"
        )
        _, sections = pr.split_diff_sections(diff)
        assert len(sections) == 2
        assert "@@" in sections[0]["body"]
        assert "@@" not in sections[1]["body"]

    def test_preamble_before_first_header(self):
        diff = "some preamble text\n==== //depot/a.cpp#1 (text) ====\n@@ -1 +1 @@\n"
        preamble, sections = pr.split_diff_sections(diff)
        assert "preamble" in preamble
        assert len(sections) == 1


# ---------------------------------------------------------------------------
# synthesize_add_hunk / synthesize_delete_hunk
# ---------------------------------------------------------------------------


class TestSynthesizeHunks:
    def test_add_hunk_prefixes_plus(self):
        content = "line one\nline two\nline three\n"
        hunk = pr.synthesize_add_hunk(content)
        assert "@@ -0,0 +1,3 @@" in hunk
        assert "+line one" in hunk
        assert "+line three" in hunk

    def test_add_hunk_empty_content(self):
        assert pr.synthesize_add_hunk("") == ""

    def test_delete_hunk_prefixes_minus(self):
        content = "old a\nold b\n"
        hunk = pr.synthesize_delete_hunk(content)
        assert "@@ -1,2 +0,0 @@" in hunk
        assert "-old a" in hunk
        assert "-old b" in hunk


# ---------------------------------------------------------------------------
# fetch_file_content -- p4 print spec selection
# ---------------------------------------------------------------------------


class TestFetchFileContent:
    def test_shelved_add_uses_at_equals_cl(self):
        with patch.object(pr, "run_p4", return_value=(0, "content\n", "")) as mock:
            pr.fetch_file_content("//depot/x.py", "1", "12345", is_shelved=True, is_delete=False)
        assert mock.call_args_list[0][0][0] == ["print", "-q", "//depot/x.py@=12345"]

    def test_submitted_add_uses_hash_rev(self):
        with patch.object(pr, "run_p4", return_value=(0, "content\n", "")) as mock:
            pr.fetch_file_content("//depot/x.py", "7", "12345", is_shelved=False, is_delete=False)
        assert mock.call_args_list[0][0][0] == ["print", "-q", "//depot/x.py#7"]

    def test_submitted_delete_uses_prior_rev(self):
        with patch.object(pr, "run_p4", return_value=(0, "content\n", "")) as mock:
            pr.fetch_file_content("//depot/x.py", "5", "12345", is_shelved=False, is_delete=True)
        assert mock.call_args_list[0][0][0] == ["print", "-q", "//depot/x.py#4"]

    def test_submitted_delete_at_rev_1_returns_none(self):
        # No rev 0 to fetch -- pre-history has no content.
        with patch.object(pr, "run_p4") as mock:
            result = pr.fetch_file_content(
                "//depot/x.py", "1", "12345", is_shelved=False, is_delete=True
            )
        assert result is None
        assert mock.call_count == 0

    def test_shelved_delete_uses_head(self):
        with patch.object(pr, "run_p4", return_value=(0, "content\n", "")) as mock:
            pr.fetch_file_content(
                "//depot/x.py", "3", "12345", is_shelved=True, is_delete=True
            )
        assert mock.call_args_list[0][0][0] == ["print", "-q", "//depot/x.py#head"]

    def test_p4_failure_returns_none(self):
        with patch.object(pr, "run_p4", return_value=(1, "", "error")):
            result = pr.fetch_file_content(
                "//depot/x.py", "1", "12345", is_shelved=False, is_delete=False
            )
        assert result is None


# ---------------------------------------------------------------------------
# fetch_file_content -- utf16 filetype: decode-aware, not reclassified as binary
# ---------------------------------------------------------------------------
#
# p4's base filetype set includes `utf16`. Whether `p4 print` emits such a
# file's bytes verbatim (as UTF-16) or translates them to the client charset
# is untestable here (no live p4 server, and it depends on unicode mode) --
# these tests drive both sides of that fork through the SAME code path and
# assert the decode is correct either way, per the module's utf16 handling.


class TestFetchFileContentUtf16Aware:
    def test_non_utf16_filetype_uses_the_plain_captured_stdout_path(self):
        """filetype=None (or any non-utf16 value) must not go through the
        temp-file byte path at all -- behavior byte-identical to before this
        change."""
        with patch.object(pr, "run_p4", return_value=(0, "content\n", "")) as mock:
            result = pr.fetch_file_content(
                "//depot/x.py", "1", "12345", is_shelved=False, is_delete=False,
                filetype="text",
            )
        assert result == "content\n"
        assert mock.call_args_list[0][0][0] == ["print", "-q", "//depot/x.py#1"]

    def test_utf16_filetype_with_bom_decodes_as_utf16(self, tmp_path):
        """If p4 print emits real UTF-16 bytes (BOM present), the sniff must
        recover the text rather than mojibake it through a forced UTF-8
        decode."""
        utf16_bytes = b"\xff\xfe" + "hello utf16\n".encode("utf-16-le")

        def fake_run_p4(args):
            assert args[:3] == ["print", "-q", "-o"]
            Path(args[3]).write_bytes(utf16_bytes)
            return (0, "", "")

        with patch.object(pr, "run_p4", side_effect=fake_run_p4):
            result = pr.fetch_file_content(
                "//depot/x.txt", "1", "12345", is_shelved=False, is_delete=False,
                filetype="utf16",
            )
        assert result == "hello utf16\n"

    def test_utf16_filetype_without_bom_falls_back_to_utf8(self):
        """If p4 translates to the client charset (UTF-8, no BOM, no
        NUL-alternation pattern), the sniff must not misfire and mangle
        already-correct UTF-8 content."""
        utf8_bytes = "plain utf8 text\n".encode("utf-8")

        def fake_run_p4(args):
            Path(args[3]).write_bytes(utf8_bytes)
            return (0, "", "")

        with patch.object(pr, "run_p4", side_effect=fake_run_p4):
            result = pr.fetch_file_content(
                "//depot/x.txt", "1", "12345", is_shelved=False, is_delete=False,
                filetype="utf16",
            )
        assert result == "plain utf8 text\n"

    def test_utf16_filetype_writes_and_cleans_up_a_temp_file(self, tmp_path):
        """The byte-sniffing path must go through a temp file (to sidestep
        run_vcs's forced UTF-8 stdout decode) and must not leak it."""
        seen_path = {}

        def fake_run_p4(args):
            seen_path["path"] = Path(args[3])
            seen_path["path"].write_bytes(b"\xff\xfeh\x00i\x00")
            return (0, "", "")

        with patch.object(pr, "run_p4", side_effect=fake_run_p4):
            pr.fetch_file_content(
                "//depot/x.txt", "1", "12345", is_shelved=False, is_delete=False,
                filetype="utf16",
            )
        assert not seen_path["path"].exists()

    def test_utf16_filetype_p4_failure_returns_none(self):
        with patch.object(pr, "run_p4", return_value=(1, "", "error")):
            result = pr.fetch_file_content(
                "//depot/x.txt", "1", "12345", is_shelved=False, is_delete=False,
                filetype="utf16",
            )
        assert result is None

    def test_utf16_modifier_suffix_still_recognized(self):
        """A `+modifiers` suffix (e.g. `utf16+ko`) must not defeat the base-type
        match -- same convention as `_is_text_filetype`."""
        utf16_bytes = b"\xff\xfe" + "x\n".encode("utf-16-le")

        def fake_run_p4(args):
            Path(args[3]).write_bytes(utf16_bytes)
            return (0, "", "")

        with patch.object(pr, "run_p4", side_effect=fake_run_p4):
            result = pr.fetch_file_content(
                "//depot/x.txt", "1", "12345", is_shelved=False, is_delete=False,
                filetype="utf16+ko",
            )
        assert result == "x\n"


# ---------------------------------------------------------------------------
# extract_diff
# ---------------------------------------------------------------------------


class TestExtractDiff:
    def test_returns_content_after_marker(self):
        out = "header\n\nDifferences ...\n\n==== //a#1 (text) ====\n@@ -1 +1 @@\n"
        result = pr.extract_diff(out)
        assert result.startswith("==== //a#1")

    def test_no_differences_section(self):
        assert pr.extract_diff("just a header\n") == ""

    def test_no_actions_returns_raw(self):
        out = "Differences ...\n==== //a.cpp#1 (text) ====\n@@ -1 +1 @@\n"
        # actions=None -> behave like a passthrough
        result = pr.extract_diff(out, actions=None)
        assert "==== //a.cpp#1" in result

    def test_synthesizes_add_hunk_for_pure_add(self):
        describe = (
            "Affected files ...\n"
            "... //depot/edit.py#2 edit\n"
            "... //depot/new.py#1 add\n"
            "Differences ...\n"
            "\n"
            "==== //depot/edit.py#2 (text) ====\n"
            "@@ -1 +1 @@\n"
            "-a\n"
            "+b\n"
            "==== //depot/new.py#1 (text) ====\n"
            "\n"
        )
        actions = pr.parse_file_actions(describe)

        def fake_run_p4(args):
            if args == ["print", "-q", "//depot/new.py#1"]:
                return (0, "def foo():\n    return 42\n", "")
            return (1, "", "unexpected p4 call")

        with patch.object(pr, "run_p4", side_effect=fake_run_p4):
            diff = pr.extract_diff(describe, actions, cl="100", is_shelved=False)

        # Both files present in output
        assert "==== //depot/edit.py#2" in diff
        assert "==== //depot/new.py#1" in diff
        # Edit file's hunk preserved
        assert "@@ -1 +1 @@" in diff
        assert "-a" in diff
        assert "+b" in diff
        # Add file's synthesized hunk present
        assert "@@ -0,0 +1,2 @@" in diff
        assert "+def foo():" in diff
        assert "+    return 42" in diff

    def test_synthesizes_add_hunk_for_utf16_add_via_bom_sniff(self):
        """A utf16-typed add file's synthesized hunk must come from the
        BOM/NUL-sniffed decode, not a forced-UTF-8 stdout capture that would
        mojibake real UTF-16 bytes. utf16 stays classified as text-like (the
        binary placeholder path is never taken)."""
        describe = (
            "Affected files ...\n"
            "... //depot/new.txt#1 add\n"
            "Differences ...\n"
            "\n"
            "==== //depot/new.txt#1 (utf16) ====\n"
            "\n"
        )
        actions = pr.parse_file_actions(describe)
        utf16_bytes = b"\xff\xfe" + "hi there\n".encode("utf-16-le")

        def fake_run_p4(args):
            if args[:3] == ["print", "-q", "-o"]:
                Path(args[3]).write_bytes(utf16_bytes)
                return (0, "", "")
            return (1, "", f"unexpected p4 call: {args}")

        with patch.object(pr, "run_p4", side_effect=fake_run_p4):
            diff = pr.extract_diff(describe, actions, cl="300", is_shelved=False)

        assert "==== //depot/new.txt#1" in diff
        assert "(binary file" not in diff
        assert "+hi there" in diff

    def test_synthesizes_delete_hunk_for_delete(self):
        describe = (
            "Affected files ...\n"
            "... //depot/gone.py#5 delete\n"
            "Differences ...\n"
            "\n"
            "==== //depot/gone.py#5 (text) ====\n"
            "\n"
        )
        actions = pr.parse_file_actions(describe)

        def fake_run_p4(args):
            if args == ["print", "-q", "//depot/gone.py#4"]:
                return (0, "old line 1\nold line 2\n", "")
            return (1, "", "unexpected")

        with patch.object(pr, "run_p4", side_effect=fake_run_p4):
            diff = pr.extract_diff(describe, actions, cl="200", is_shelved=False)

        assert "@@ -1,2 +0,0 @@" in diff
        assert "-old line 1" in diff
        assert "-old line 2" in diff

    def test_shelved_add_uses_at_equals_cl(self):
        describe = (
            "Shelved files ...\n"
            "... //depot/new.py#1 add\n"
            "Differences ...\n"
            "\n"
            "==== //depot/new.py#1 (text) ====\n"
            "\n"
        )
        actions = pr.parse_file_actions(describe)

        calls: list[list[str]] = []

        def fake_run_p4(args):
            calls.append(args)
            if args == ["print", "-q", "//depot/new.py@=144072"]:
                return (0, "hello\n", "")
            return (1, "", "")

        with patch.object(pr, "run_p4", side_effect=fake_run_p4):
            diff = pr.extract_diff(describe, actions, cl="144072", is_shelved=True)

        assert ["print", "-q", "//depot/new.py@=144072"] in calls
        assert "+hello" in diff

    def test_mixed_cl_adds_missing_from_differences_synthesized(self):
        """Mixed CL: pure-adds listed in Shelved files but omitted from Differences.

        Real p4 behavior on some servers: `p4 describe -du -S` for a shelved mixed
        CL emits `==== ...====` headers ONLY for edits. Pure-add files appear only
        in the Shelved files listing, never in the Differences section. The script
        must still synthesize sections (header + hunk) for these missing files,
        not drop them silently.
        """
        describe = (
            "Shelved files ...\n"
            "... //depot/edit.cpp#3 edit\n"
            "... //depot/add1.cpp#1 add\n"
            "... //depot/add2.cpp#1 add\n"
            "Differences ...\n"
            "\n"
            "==== //depot/edit.cpp#3 (text) ====\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        )
        actions = pr.parse_file_actions(describe)

        def fake_run_p4(args):
            if args == ["print", "-q", "//depot/add1.cpp@=99"]:
                return (0, "content of add1\n", "")
            if args == ["print", "-q", "//depot/add2.cpp@=99"]:
                return (0, "content of add2\n", "")
            return (1, "", f"unexpected: {args}")

        with patch.object(pr, "run_p4", side_effect=fake_run_p4):
            diff = pr.extract_diff(describe, actions, cl="99", is_shelved=True)

        # All three files must appear in the diff
        assert "==== //depot/edit.cpp#3" in diff
        assert "==== //depot/add1.cpp#1" in diff
        assert "==== //depot/add2.cpp#1" in diff
        # Edit preserved
        assert "-old" in diff
        assert "+new" in diff
        # Adds synthesized
        assert "+content of add1" in diff
        assert "+content of add2" in diff

    def test_warns_on_unhandled_add_to_stderr(self, capsys):
        describe = (
            "Affected files ...\n"
            "... //depot/new.py#1 add\n"
            "Differences ...\n"
            "==== //depot/new.py#1 (text) ====\n"
            "\n"
        )
        actions = pr.parse_file_actions(describe)
        with patch.object(pr, "run_p4", return_value=(1, "", "no such file")):
            pr.extract_diff(describe, actions, cl="300", is_shelved=False)
        err = capsys.readouterr().err
        assert "could not synthesize" in err
        assert "//depot/new.py" in err

    def test_stderr_notes_synthesized_files(self, capsys):
        describe = (
            "Affected files ...\n"
            "... //depot/new.py#1 add\n"
            "Differences ...\n"
            "==== //depot/new.py#1 (text) ====\n"
            "\n"
        )
        actions = pr.parse_file_actions(describe)
        with patch.object(pr, "run_p4", return_value=(0, "x\n", "")):
            pr.extract_diff(describe, actions, cl="400", is_shelved=False)
        err = capsys.readouterr().err
        assert "synthesized add hunks" in err
        assert "//depot/new.py" in err


# ---------------------------------------------------------------------------
# _p4_diff_to_sections -- thin adapter feeding bootstrap_lib's chunker
# ---------------------------------------------------------------------------


class TestP4DiffToSections:
    def test_passes_preamble_and_depot_as_identifier(self):
        diff = (
            "preamble line\n"
            "==== //depot/foo/bar.cpp#1 (text) ====\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
            "==== //depot/foo/baz.h#2 (text) ====\n"
            "@@ -5 +5 @@\n"
            "-a\n"
            "+b\n"
        )
        preamble, sections = pr._p4_diff_to_sections(diff)
        assert preamble == "preamble line\n"
        assert len(sections) == 2
        assert sections[0]["identifier"] == "//depot/foo/bar.cpp"
        assert sections[0]["text"].startswith("==== //depot/foo/bar.cpp#1")
        assert "-old" in sections[0]["text"]
        assert sections[1]["identifier"] == "//depot/foo/baz.h"


# ---------------------------------------------------------------------------
# has_describe_content
# ---------------------------------------------------------------------------


class TestHasDescribeContent:
    def test_true_when_marker_and_file_header(self):
        out = "Differences ...\n==== //depot/a.cpp#1 (text) ====\n"
        assert pr.has_describe_content(out)

    def test_true_when_add_only_has_header_no_hunk(self):
        # The bug fix: pure-add sections have no @@ but we still want to accept the describe.
        out = "Differences ...\n==== //depot/new.py#1 (text) ====\n\n"
        assert pr.has_describe_content(out)

    def test_false_when_no_differences_section(self):
        assert not pr.has_describe_content("Affected files ...\n")

    def test_false_when_differences_empty(self):
        assert not pr.has_describe_content("Differences ...\n(no files)\n")

    def test_true_for_pure_add_with_only_affected_files_section(self):
        """Pure-add CLs may emit no Differences-section headers at all.

        Perforce gives pure-add CLs no `==== ====` headers under
        `Differences ...` because there is no prior version to diff against.
        The synthesizable add action listed under `Affected files ...`
        is enough to mark the describe as reviewable -- extract_diff
        will fill in the diff body via p4 print.
        """
        out = (
            "Affected files ...\n"
            "\n"
            "... //depot/new.py#1 add\n"
            "\n"
            "Differences ...\n"
        )
        assert pr.has_describe_content(out)

    def test_true_for_pure_delete_with_only_shelved_files_section(self):
        out = (
            "Shelved files ...\n"
            "\n"
            "... //depot/old.py#3 delete\n"
            "\n"
            "Differences ...\n"
        )
        assert pr.has_describe_content(out)

    def test_false_for_edit_only_section_without_differences_headers(self):
        """Edits cannot be synthesized -- they need real diff bodies.

        A describe that lists only edits in Affected files but has no
        Differences-section headers means the diff is genuinely missing
        (e.g. a pending edit that hasn't been shelved yet). Reject it so
        callers fall through to the shelved fallback.
        """
        out = (
            "Affected files ...\n"
            "\n"
            "... //depot/changed.py#5 edit\n"
            "\n"
        )
        assert not pr.has_describe_content(out)


# ---------------------------------------------------------------------------
# fetch_describe -- shelved fallback
# ---------------------------------------------------------------------------


class TestFetchDescribe:
    def test_committed_returns_first_with_is_shelved_false(self):
        committed_out = "Differences ...\n==== //a.cpp#1 (text) ====\n@@ -1 +1 @@\n"
        with patch.object(pr, "run_p4", return_value=(0, committed_out, "")) as mock:
            out, is_shelved = pr.fetch_describe("123")
            assert out == committed_out
            assert is_shelved is False
            assert mock.call_count == 1
            assert mock.call_args_list[0][0][0] == ["describe", "-du", "123"]

    def test_shelved_fallback_used(self):
        empty = "Affected files ...\n... //depot/a.cpp#1 edit\n"
        shelved = "Differences ...\n==== //a.cpp#1 (text) ====\n@@ -1 +1 @@\n"

        def side(args):
            if "-S" in args:
                return (0, shelved, "")
            return (0, empty, "")

        with patch.object(pr, "run_p4", side_effect=side) as mock:
            out, is_shelved = pr.fetch_describe("123")
            assert out == shelved
            assert is_shelved is True
            assert mock.call_count == 2

    def test_raises_when_neither_has_differences(self):
        empty = "Affected files ...\n"
        with patch.object(pr, "run_p4", return_value=(0, empty, "")):
            with pytest.raises(ValueError, match="no describe content"):
                pr.fetch_describe("123")

    def test_accepts_add_only_cl_without_hunks(self):
        # An add-only CL has file headers but no @@ -- should still succeed.
        add_only = (
            "Differences ...\n"
            "==== //depot/new.py#1 (text) ====\n"
            "\n"
        )
        with patch.object(pr, "run_p4", return_value=(0, add_only, "")):
            out, is_shelved = pr.fetch_describe("123")
        assert out == add_only
        assert is_shelved is False

    def test_accepts_submitted_pure_add_with_no_differences_headers(self):
        """Submitted pure-add CLs whose `==== ====` headers are missing under Differences.

        Some Perforce describe responses for submitted pure-add CLs list the
        adds in `Affected files ...` but emit an empty `Differences ...`
        section -- there is nothing to diff against. extract_diff can still
        synthesize hunks via `p4 print #<rev>`, so fetch_describe should
        accept this output rather than rejecting it.
        """
        submitted_pure_add = (
            "Change 999 by user@client on 2026/01/01 12:00:00\n"
            "\n"
            "\tdesc\n"
            "\n"
            "Affected files ...\n"
            "\n"
            "... //depot/new.py#1 add\n"
            "\n"
            "Differences ...\n"
        )
        with patch.object(pr, "run_p4", return_value=(0, submitted_pure_add, "")) as mock:
            out, is_shelved = pr.fetch_describe("999")
        assert is_shelved is False
        # No fallback to -S needed since the submitted output is reviewable.
        assert mock.call_count == 1

    def test_pending_pure_add_routed_to_shelved_path(self):
        """Pending pure-add CLs must be read via -S so synthesis uses @=<CL>.

        Going through the regular describe would return is_shelved=False,
        and `p4 print #1` for a pending add would fail (no submitted rev
        exists). Forcing the -S path returns is_shelved=True so synthesis
        uses the shelved spec @=<CL>, which works.
        """
        pending_unshelved = (
            "Change 144098 by user@client on 2026/01/01 12:00:00 *pending*\n"
            "\n"
            "\tdesc\n"
            "\n"
            "Affected files ...\n"
            "\n"
            "... //depot/new.py#1 add\n"
            "\n"
        )
        pending_shelved = (
            "Change 144098 by user@client on 2026/01/01 12:00:00 *pending*\n"
            "\n"
            "\tdesc\n"
            "\n"
            "Shelved files ...\n"
            "\n"
            "... //depot/new.py#1 add\n"
            "\n"
            "Differences ...\n"
        )

        def side(args):
            if "-S" in args:
                return (0, pending_shelved, "")
            return (0, pending_unshelved, "")

        with patch.object(pr, "run_p4", side_effect=side) as mock:
            out, is_shelved = pr.fetch_describe("144098")
        assert is_shelved is True
        assert out == pending_shelved
        assert mock.call_count == 2

    def test_pending_unshelved_raises_pending_unshelved_error(self):
        """A pending CL with no shelved content raises PendingUnshelvedError.

        Distinct from a generic `no describe content` failure so build_bundle
        can react with auto-shelve + retry instead of propagating.
        """
        pending_unshelved = (
            "Change 144098 by user@client on 2026/01/01 12:00:00 *pending*\n"
            "\n"
            "\tdesc\n"
            "\n"
            "Affected files ...\n"
            "\n"
            "... //depot/new.py#1 add\n"
            "\n"
        )
        empty_shelved = (
            "Change 144098 by user@client on 2026/01/01 12:00:00 *pending*\n"
        )

        def side(args):
            if "-S" in args:
                return (0, empty_shelved, "")
            return (0, pending_unshelved, "")

        with patch.object(pr, "run_p4", side_effect=side):
            with pytest.raises(pr.PendingUnshelvedError):
                pr.fetch_describe("144098")


# ---------------------------------------------------------------------------
# resolve_local_paths
# ---------------------------------------------------------------------------


class TestResolveLocalPaths:
    def test_parses_ztag_where_output(self):
        out = (
            "... depotFile //depot/a.cpp\n"
            "... clientFile //ws/a.cpp\n"
            "... path C:\\workspace\\a.cpp\n"
            "\n"
            "... depotFile //depot/b.h\n"
            "... clientFile //ws/b.h\n"
            "... path C:\\workspace\\b.h\n"
        )
        with patch.object(pr, "run_p4", return_value=(0, out, "")):
            result = pr.resolve_local_paths(["//depot/a.cpp", "//depot/b.h"])
        assert result == {
            "//depot/a.cpp": "C:\\workspace\\a.cpp",
            "//depot/b.h": "C:\\workspace\\b.h",
        }

    def test_empty_input(self):
        assert pr.resolve_local_paths([]) == {}

    def test_partial_failure_keeps_mapped_rows(self):
        """A batch can return non-zero (one unmapped depot path in the
        argument list) while still emitting ztag rows for the depot paths it
        COULD map. Parsing must not be skipped just because the batch's
        overall rc is non-zero -- otherwise one unmapped file in a batch of
        100 costs the whole batch its local paths."""
        out = (
            "... depotFile //depot/a.cpp\n"
            "... clientFile //ws/a.cpp\n"
            "... path C:\\workspace\\a.cpp\n"
            "\n"
        )
        with patch.object(
            pr,
            "run_p4",
            return_value=(1, out, "//depot/b.h - file(s) not in client view.\n"),
        ):
            result = pr.resolve_local_paths(["//depot/a.cpp", "//depot/b.h"])
        assert result == {
            "//depot/a.cpp": "C:\\workspace\\a.cpp",
            "//depot/b.h": None,
        }

    def test_full_batch_failure_with_empty_stdout_returns_none_for_each(self):
        """A batch is treated as empty only when stdout yields nothing --
        this is the one case where every path in the batch stays None."""
        with patch.object(pr, "run_p4", return_value=(1, "", "error")):
            result = pr.resolve_local_paths(["//depot/a.cpp"])
        assert result == {"//depot/a.cpp": None}


# ---------------------------------------------------------------------------
# compute_minimal_dirs
# ---------------------------------------------------------------------------


class TestComputeMinimalDirs:
    def test_collapses_descendants(self, tmp_path):
        a = tmp_path / "a"
        ab = a / "b"
        c = tmp_path / "c"
        ab.mkdir(parents=True)
        c.mkdir()
        files = [str(a / "f1.cpp"), str(ab / "f2.cpp"), str(c / "f3.cpp")]
        result = pr.compute_minimal_dirs(files)
        # /a covers /a/b -> only /a and /c remain, both recursive
        assert {(p.resolve(), r) for p, r in result} == {
            (a.resolve(), True),
            (c.resolve(), True),
        }

    def test_skips_none_and_missing(self, tmp_path):
        real = tmp_path / "real"
        real.mkdir()
        files = [None, str(real / "x.cpp"), str(tmp_path / "ghost" / "y.cpp")]
        result = pr.compute_minimal_dirs(files)
        assert {(p.resolve(), r) for p, r in result} == {(real.resolve(), True)}

    def test_empty(self):
        assert pr.compute_minimal_dirs([]) == []

    def test_unique_dir_kept(self, tmp_path):
        a = tmp_path / "a"
        a.mkdir()
        files = [str(a / "x.cpp"), str(a / "y.cpp"), str(a / "z.cpp")]
        result = pr.compute_minimal_dirs(files)
        assert len(result) == 1
        assert result[0][0].resolve() == a.resolve()
        assert result[0][1] is True

    def test_sibling_dirs_both_kept(self, tmp_path):
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        files = [str(a / "x.cpp"), str(b / "y.cpp")]
        result = pr.compute_minimal_dirs(files)
        assert {(p.resolve(), r) for p, r in result} == {
            (a.resolve(), True),
            (b.resolve(), True),
        }

    def test_workspace_root_does_not_absorb_descendants(self, tmp_path):
        """A CL touching a workspace-root file plus a deep file must NOT collapse
        to a recursive scan of the entire workspace. The root is kept as a
        non-recursive scan target (`<root>/*`); the deep dir keeps its own
        recursive scan separately. See module docstring for rationale."""
        ws = tmp_path / "ws"
        deep = ws / "plugins" / "p4-kit" / "scripts"
        deep.mkdir(parents=True)
        files = [str(ws / "CLAUDE.md"), str(deep / "prepare_review.py")]
        result = pr.compute_minimal_dirs(files, workspace_root=ws)
        assert {(p.resolve(), r) for p, r in result} == {
            (ws.resolve(), False),
            (deep.resolve(), True),
        }

    def test_workspace_root_only_is_non_recursive(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        files = [str(ws / "CLAUDE.md"), str(ws / "marketplace.json")]
        result = pr.compute_minimal_dirs(files, workspace_root=ws)
        assert result == [(ws.resolve(), False)]

    def test_no_workspace_root_arg_preserves_old_collapse(self, tmp_path):
        """When workspace_root is None, the function still collapses ancestors."""
        a = tmp_path / "a"
        ab = a / "b"
        ab.mkdir(parents=True)
        files = [str(a / "x.cpp"), str(ab / "y.cpp")]
        result = pr.compute_minimal_dirs(files)
        assert {(p.resolve(), r) for p, r in result} == {(a.resolve(), True)}


# ---------------------------------------------------------------------------
# find_unreconciled
# ---------------------------------------------------------------------------


class TestFindUnreconciled:
    def test_parses_ztag_output(self, tmp_path):
        d = tmp_path / "src"
        d.mkdir()
        out = (
            "... depotFile //depot/src/new.cpp\n"
            "... clientFile /ws/src/new.cpp\n"
            "... rev 1\n"
            "... action add\n"
            "... type text\n"
            "\n"
            "... depotFile //depot/src/edited.cpp\n"
            "... clientFile /ws/src/edited.cpp\n"
            "... rev 3\n"
            "... action edit\n"
            "... type text\n"
            "\n"
            "... depotFile //depot/src/gone.cpp\n"
            "... clientFile /ws/src/gone.cpp\n"
            "... rev 2\n"
            "... action delete\n"
            "... type text\n"
        )
        with patch.object(pr, "run_p4", return_value=(0, out, "")):
            result, incomplete = pr.find_unreconciled([(d, True)])
        assert result == [
            {"local": "/ws/src/new.cpp", "depot": "//depot/src/new.cpp", "action": "add"},
            {"local": "/ws/src/edited.cpp", "depot": "//depot/src/edited.cpp", "action": "edit"},
            {"local": "/ws/src/gone.cpp", "depot": "//depot/src/gone.cpp", "action": "delete"},
        ]
        assert incomplete == []

    def test_uses_recursive_dir_specs(self, tmp_path):
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        captured: list[list[str]] = []

        def fake_run_p4(args):
            captured.append(args)
            return (0, "", "")

        with patch.object(pr, "run_p4", side_effect=fake_run_p4):
            pr.find_unreconciled([(a, True), (b, True)])

        assert captured[0][:3] == ["-ztag", "reconcile", "-n"]
        # Each dir is passed as a recursive `<dir>/...` spec.
        specs = captured[0][3:]
        assert any(s.endswith("/...") and str(a) in s for s in specs)
        assert any(s.endswith("/...") and str(b) in s for s in specs)

    def test_non_recursive_dir_uses_star_spec(self, tmp_path):
        """A `(dir, False)` entry must scan with `<dir>/*` (immediate children only),
        not `<dir>/...` -- this is what bounds the workspace-root scan."""
        root = tmp_path / "ws"
        deep = root / "plugins" / "scripts"
        deep.mkdir(parents=True)
        captured: list[list[str]] = []

        def fake_run_p4(args):
            captured.append(args)
            return (0, "", "")

        with patch.object(pr, "run_p4", side_effect=fake_run_p4):
            pr.find_unreconciled([(root, False), (deep, True)])

        specs = captured[0][3:]
        assert f"{root}/*" in specs
        assert f"{deep}/..." in specs
        # The recursive workspace-root spec must NOT appear.
        assert f"{root}/..." not in specs

    def test_empty_dirs_returns_empty(self):
        # No p4 call should be made when there's nothing to scan.
        with patch.object(pr, "run_p4") as mock:
            assert pr.find_unreconciled([]) == ([], [])
        assert mock.call_count == 0

    def test_no_files_to_reconcile_treated_as_empty(self, tmp_path):
        d = tmp_path / "x"
        d.mkdir()
        with patch.object(
            pr,
            "run_p4",
            return_value=(1, "", "/ws/x - no file(s) to reconcile.\n"),
        ):
            assert pr.find_unreconciled([(d, True)]) == ([], [])

    def test_p4_failure_returns_incomplete_entry_not_a_clean_empty(self, tmp_path, capsys):
        """A hygiene scan that could not run must not serialize as a clean `[]` --
        it must say so via a `{scan, reason}` incomplete entry, so a consumer can
        tell "ran, found nothing" from "did not run". This replaces a prior test
        that asserted the empty-list fail-open result; that assertion pinned the
        defect this change fixes."""
        d = tmp_path / "x"
        d.mkdir()
        with patch.object(pr, "run_p4", return_value=(1, "", "fatal: bad workspace\n")):
            items, incomplete = pr.find_unreconciled([(d, True)])
        assert items == []
        assert len(incomplete) == 1
        assert incomplete[0]["scan"] == "unreconciled"
        assert "bad workspace" in incomplete[0]["reason"]
        err = capsys.readouterr().err
        assert "reconcile check failed" in err

    def test_skips_entries_missing_required_fields(self, tmp_path):
        # Defensive: an incomplete record (no action, or no clientFile) is dropped.
        d = tmp_path / "x"
        d.mkdir()
        out = (
            "... depotFile //depot/orphan.cpp\n"
            "... rev 1\n"
            "\n"
            "... clientFile /ws/no-action.cpp\n"
            "... depotFile //depot/no-action.cpp\n"
            "\n"
            "... depotFile //depot/good.cpp\n"
            "... clientFile /ws/good.cpp\n"
            "... action add\n"
        )
        with patch.object(pr, "run_p4", return_value=(0, out, "")):
            result, incomplete = pr.find_unreconciled([(d, True)])
        assert len(result) == 1
        assert result[0]["local"] == "/ws/good.cpp"
        assert incomplete == []


# ---------------------------------------------------------------------------
# find_default_open
# ---------------------------------------------------------------------------


class TestFindDefaultOpen:
    def test_reports_open_files_from_the_scoped_default_changelist(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        expected_command = [
            "-ztag",
            "opened",
            "-c",
            "default",
            f"{src}/...",
        ]
        output = (
            "... depotFile //depot/src/extra.cpp\n"
            "... clientFile /ws/src/extra.cpp\n"
            "... rev 3\n"
            "... action edit\n"
            "... change default\n"
            "... type text\n"
        )
        commands = []

        def fake_run_p4(args):
            if args != expected_command:
                raise AssertionError(f"unexpected p4 command: {args}")
            commands.append(args)
            return 0, output, ""

        with patch.object(pr, "run_p4", side_effect=fake_run_p4):
            items, incomplete = pr.find_default_open([(src, True)])

        assert (items, incomplete, commands) == (
            [
                {
                    "local": "/ws/src/extra.cpp",
                    "depot": "//depot/src/extra.cpp",
                    "action": "edit",
                }
            ],
            [],
            [expected_command],
        )

    def test_failure_is_reported_as_incomplete(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        expected_command = [
            "-ztag",
            "opened",
            "-c",
            "default",
            f"{src}/...",
        ]

        def fake_run_p4(args):
            if args != expected_command:
                raise AssertionError(f"unexpected p4 command: {args}")
            return 1, "", "server unavailable"

        with patch.object(pr, "run_p4", side_effect=fake_run_p4):
            result = pr.find_default_open([(src, True)])

        assert result == (
            [],
            [{"scan": "default_open", "reason": "server unavailable"}],
        )

    def test_nonzero_empty_spec_keeps_other_open_records(self, tmp_path):
        src = tmp_path / "src"
        empty = tmp_path / "empty"
        src.mkdir()
        empty.mkdir()
        expected_command = [
            "-ztag",
            "opened",
            "-c",
            "default",
            f"{src}/...",
            f"{empty}/...",
        ]
        output = (
            "... depotFile //depot/src/extra.cpp\n"
            "... clientFile /ws/src/extra.cpp\n"
            "... action edit\n"
        )

        def fake_run_p4(args):
            if args != expected_command:
                raise AssertionError(f"unexpected p4 command: {args}")
            return 1, output, f"{empty}/... - file(s) not opened on this client.\n"

        with patch.object(pr, "run_p4", side_effect=fake_run_p4):
            result = pr.find_default_open([(src, True), (empty, True)])

        assert result == (
            [
                {
                    "local": "/ws/src/extra.cpp",
                    "depot": "//depot/src/extra.cpp",
                    "action": "edit",
                }
            ],
            [],
        )


# ---------------------------------------------------------------------------
# find_unresolved
# ---------------------------------------------------------------------------


class TestFindUnresolved:
    def test_parses_ztag_output(self):
        out = (
            "... clientFile /ws/src/a.cpp\n"
            "... toFile //depot/src/a.cpp\n"
            "... fromFile //depot/src/a.cpp\n"
            "... resolveType content\n"
        )
        with patch.object(pr, "run_p4", return_value=(0, out, "")):
            result, incomplete = pr.find_unresolved("123")
        assert result == [
            {
                "local": "/ws/src/a.cpp",
                "depot": "//depot/src/a.cpp",
                "resolve_type": "content",
                "from_file": "//depot/src/a.cpp",
            }
        ]
        assert incomplete == []

    def test_no_files_to_resolve_treated_as_empty(self):
        with patch.object(
            pr, "run_p4", return_value=(1, "", "no file(s) to resolve.\n")
        ):
            assert pr.find_unresolved("123") == ([], [])

    def test_p4_failure_returns_incomplete_entry_not_a_clean_empty(self, capsys):
        """Same governing principle as find_unreconciled: a resolve check that
        could not run must say so, not serialize as a clean `[]`. Unresolved
        files gate submittability, so a silent empty list here is exactly the
        "review looks complete-and-clean while a mechanism did not run" shape."""
        with patch.object(pr, "run_p4", return_value=(1, "", "fatal: bad workspace\n")):
            items, incomplete = pr.find_unresolved("123")
        assert items == []
        assert len(incomplete) == 1
        assert incomplete[0]["scan"] == "unresolved"
        assert "bad workspace" in incomplete[0]["reason"]
        err = capsys.readouterr().err
        assert "resolve check failed" in err


# ---------------------------------------------------------------------------
# build_bundle -- integration
# ---------------------------------------------------------------------------


class TestBuildBundle:
    def test_full_pipeline(self, tmp_path):
        # Set up a fake workspace with one file and one CLAUDE.md
        ws = tmp_path / "ws"
        src = ws / "src"
        src.mkdir(parents=True)
        (ws / "CLAUDE.md").write_text("workspace rule\n")
        local_file = src / "foo.cpp"
        local_file.write_text("int x = 1;\n")

        describe_out = (
            "Change 999 by user@client on 2026/01/01 12:00:00 *pending*\n"
            "\n"
            "\tFix the thing\n"
            "\n"
            "Affected files ...\n"
            "... //depot/src/foo.cpp#1 edit\n"
            "\n"
            "Differences ...\n"
            "\n"
            "==== //depot/src/foo.cpp#1 (text) ====\n"
            "@@ -1 +1 @@\n"
            "-int x = 0;\n"
            "+int x = 1;\n"
        )
        where_out = (
            "... depotFile //depot/src/foo.cpp\n"
            f"... path {local_file}\n"
        )
        info_out = f"... clientRoot {ws}\n"

        def fake_run_p4(args):
            if args[:2] == ["describe", "-du"]:
                return (0, describe_out, "")
            if args[:2] == ["-ztag", "fstat"]:
                return (1, "", "no such file(s)")
            if args[:4] == ["-ztag", "opened", "-c", "default"]:
                return (0, "", "")
            if args[:3] == ["-ztag", "opened", "-c"]:
                return (0, "", "")
            if args[:2] == ["-ztag", "where"]:
                return (0, where_out, "")
            if args[:2] == ["-ztag", "info"]:
                return (0, info_out, "")
            if args[:3] == ["-ztag", "reconcile", "-n"]:
                return (1, "", "no file(s) to reconcile.\n")
            if args[:4] == ["-ztag", "resolve", "-n", "-c"]:
                return (1, "", "no file(s) to resolve.\n")
            if args[:3] == ["print", "-q", "-o"]:
                Path(args[3]).parent.mkdir(parents=True, exist_ok=True)
                Path(args[3]).write_text("int x = 0;\n", encoding="utf-8")
                return (0, "", "")
            raise AssertionError(f"unexpected p4 command: {args}")

        bundle_dir = tmp_path / "bundle"
        with patch.object(pr, "run_p4", side_effect=fake_run_p4):
            bundle = pr.build_bundle("999", bundle_dir)

        assert bundle["cl"] == "999"
        assert bundle["description"] == "Fix the thing"
        assert bundle["bundle_dir"] == str(bundle_dir)
        # Diff content lives in chunk files on disk now; no inline `diff` field.
        assert "diff" not in bundle
        assert len(bundle["diff_chunks"]) >= 1
        assert "==== //depot/src/foo.cpp#1" in _concat_diff_from_chunks(bundle)
        assert len(bundle["changed_files"]) == 1
        cf = bundle["changed_files"][0]
        assert cf["depot"] == "//depot/src/foo.cpp"
        assert Path(cf["local"]) == local_file
        # The single-file CL should land in a single chunk (index 0).
        assert cf["chunk_index"] == 0
        assert len(cf["claude_mds"]) == 1
        assert Path(cf["claude_mds"][0]).read_text() == "workspace rule\n"
        assert len(bundle["unique_claude_mds"]) == 1
        # Registered structured-data checks read the post-image, so an
        # unclaimed file's pre-image IS materialized. The gate itself -- that
        # this follows the registry rather than being unconditional -- is
        # pinned in the git kit's registry test, which forces both answers.
        snapshot = bundle_dir / pr.preimage_relpath("//depot/src/foo.cpp")
        assert snapshot.read_text(encoding="utf-8") == "int x = 0;\n"
        assert bundle["unreconciled"] == []
        assert bundle["hygiene_incomplete"] == []

    def test_hygiene_incomplete_reports_a_reconcile_scan_that_could_not_run(self, tmp_path):
        """A failed hygiene scan must never serialize as a clean empty
        `unreconciled` list with no other
        signal. When `p4 reconcile -n` fails for a reason other than "no
        file(s) to reconcile", build_bundle must still return `unreconciled=[]`
        (do not invent findings) but flag the scan as incomplete."""
        ws = tmp_path / "ws"
        src = ws / "src"
        src.mkdir(parents=True)
        local_file = src / "foo.cpp"
        local_file.write_text("int x = 1;\n")

        describe_out = (
            "Change 999 by user@client on 2026/01/01 12:00:00 *pending*\n"
            "\n"
            "\tFix the thing\n"
            "\n"
            "Affected files ...\n"
            "... //depot/src/foo.cpp#1 edit\n"
            "\n"
            "Differences ...\n"
            "\n"
            "==== //depot/src/foo.cpp#1 (text) ====\n"
            "@@ -1 +1 @@\n"
            "-int x = 0;\n"
            "+int x = 1;\n"
        )
        where_out = (
            "... depotFile //depot/src/foo.cpp\n"
            f"... path {local_file}\n"
        )
        info_out = f"... clientRoot {ws}\n"

        def fake_run_p4(args):
            if args[:2] == ["describe", "-du"]:
                return (0, describe_out, "")
            if args[:2] == ["-ztag", "fstat"]:
                return (1, "", "no such file(s)")
            if args[:4] == ["-ztag", "opened", "-c", "default"]:
                return (0, "", "")
            if args[:3] == ["-ztag", "opened", "-c"]:
                return (0, "", "")
            if args[:2] == ["-ztag", "where"]:
                return (0, where_out, "")
            if args[:2] == ["-ztag", "info"]:
                return (0, info_out, "")
            if args[:3] == ["-ztag", "reconcile", "-n"]:
                return (1, "", "fatal: bad workspace\n")
            if args[:4] == ["-ztag", "resolve", "-n", "-c"]:
                return (1, "", "no file(s) to resolve.\n")
            if args[:3] == ["print", "-q", "-o"]:
                Path(args[3]).parent.mkdir(parents=True, exist_ok=True)
                Path(args[3]).write_text("int x = 0;\n", encoding="utf-8")
                return (0, "", "")
            raise AssertionError(f"unexpected p4 command: {args}")

        with patch.object(pr, "run_p4", side_effect=fake_run_p4):
            bundle = pr.build_bundle("999", tmp_path / "bundle")

        # No invented findings: an incomplete scan reports no items either.
        assert bundle["unreconciled"] == []
        assert bundle["hygiene_incomplete"] == [
            {"scan": "unreconciled", "reason": "fatal: bad workspace"},
        ]

    def test_unreconciled_files_surfaced(self, tmp_path):
        """build_bundle reports files missing from the CL via `p4 reconcile -n`."""
        ws = tmp_path / "ws"
        src = ws / "src"
        src.mkdir(parents=True)
        local_file = src / "foo.cpp"
        local_file.write_text("int x = 1;\n")
        # A sibling file that exists on disk but isn't in the CL.
        forgotten = src / "forgot.cpp"
        forgotten.write_text("int y = 2;\n")

        describe_out = (
            "Change 1000 by user@client on 2026/01/01\n"
            "\n"
            "\tEdit foo\n"
            "\n"
            "Affected files ...\n"
            "... //depot/src/foo.cpp#1 edit\n"
            "\n"
            "Differences ...\n"
            "\n"
            "==== //depot/src/foo.cpp#1 (text) ====\n"
            "@@ -1 +1 @@\n"
            "-int x = 0;\n"
            "+int x = 1;\n"
        )
        where_out = (
            "... depotFile //depot/src/foo.cpp\n"
            f"... path {local_file}\n"
        )
        info_out = f"... clientRoot {ws}\n"
        reconcile_out = (
            "... depotFile //depot/src/forgot.cpp\n"
            f"... clientFile {forgotten}\n"
            "... rev 1\n"
            "... action add\n"
            "... type text\n"
        )

        def fake_run_p4(args):
            if args[:2] == ["describe", "-du"]:
                return (0, describe_out, "")
            if args[:2] == ["-ztag", "where"]:
                return (0, where_out, "")
            if args[:2] == ["-ztag", "info"]:
                return (0, info_out, "")
            if args[:3] == ["-ztag", "reconcile", "-n"]:
                return (0, reconcile_out, "")
            if args[:4] == ["-ztag", "opened", "-c", "default"]:
                return (0, "", "")
            if args[:4] == ["-ztag", "resolve", "-n", "-c"]:
                return (1, "", "no file(s) to resolve.\n")
            if args[:2] == ["-ztag", "fstat"]:
                return (1, "", "no such file(s)")
            raise AssertionError(f"unexpected p4 command: {args}")

        with patch.object(pr, "run_p4", side_effect=fake_run_p4):
            bundle = pr.build_bundle("1000", tmp_path / "bundle")

        assert len(bundle["unreconciled"]) == 1
        u = bundle["unreconciled"][0]
        assert u["action"] == "add"
        assert u["depot"] == "//depot/src/forgot.cpp"
        assert Path(u["local"]) == forgotten

    def test_root_level_cl_file_does_not_trigger_recursive_root_scan(self, tmp_path):
        """Regression: a CL touching a workspace-root file (e.g. CLAUDE.md) plus a
        deep file used to collapse to `<root>/...`, recursively scanning every
        untracked dir in the workspace (Binaries/, Intermediate/, IDE files, etc.
        that may not all be in .p4ignore). The fix bounds root to `<root>/*`."""
        ws = tmp_path / "ws"
        deep = ws / "plugins" / "p4-kit" / "scripts"
        deep.mkdir(parents=True)
        root_file = ws / "CLAUDE.md"
        root_file.write_text("rules\n")
        deep_file = deep / "prepare_review.py"
        deep_file.write_text("# code\n")

        describe_out = (
            "Change 7 by u@c on 2026/01/01\n"
            "\n"
            "\tEdit\n"
            "\n"
            "Affected files ...\n"
            "... //depot/CLAUDE.md#1 edit\n"
            "... //depot/plugins/p4-kit/scripts/prepare_review.py#1 edit\n"
            "\n"
            "Differences ...\n"
            "\n"
            "==== //depot/CLAUDE.md#1 (text) ====\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
            "==== //depot/plugins/p4-kit/scripts/prepare_review.py#1 (text) ====\n"
            "@@ -1 +1 @@\n"
            "-a\n"
            "+b\n"
        )
        where_out = (
            "... depotFile //depot/CLAUDE.md\n"
            f"... path {root_file}\n"
            "\n"
            "... depotFile //depot/plugins/p4-kit/scripts/prepare_review.py\n"
            f"... path {deep_file}\n"
        )
        info_out = f"... clientRoot {ws}\n"
        captured_reconcile_specs: list[str] = []

        def fake_run_p4(args):
            if args[:2] == ["describe", "-du"]:
                return (0, describe_out, "")
            if args[:2] == ["-ztag", "where"]:
                return (0, where_out, "")
            if args[:2] == ["-ztag", "info"]:
                return (0, info_out, "")
            if args[:3] == ["-ztag", "reconcile", "-n"]:
                captured_reconcile_specs.extend(args[3:])
                return (1, "", "no file(s) to reconcile.\n")
            if args[:4] == ["-ztag", "opened", "-c", "default"]:
                return (0, "", "")
            if args[:4] == ["-ztag", "resolve", "-n", "-c"]:
                return (1, "", "no file(s) to resolve.\n")
            if args[:2] == ["-ztag", "fstat"]:
                return (1, "", "no such file(s)")
            raise AssertionError(f"unexpected p4 command: {args}")

        with patch.object(pr, "run_p4", side_effect=fake_run_p4):
            pr.build_bundle("7", tmp_path / "bundle")

        ws_resolved = ws.resolve()
        deep_resolved = deep.resolve()
        # Root scanned non-recursively
        assert f"{ws_resolved}/*" in captured_reconcile_specs
        # Recursive root scan must NOT appear (this was the bug)
        assert f"{ws_resolved}/..." not in captured_reconcile_specs
        # Deep dir keeps its own recursive scan (not absorbed by root)
        assert f"{deep_resolved}/..." in captured_reconcile_specs

    def test_mixed_edit_and_add_synthesizes_add_hunk(self, tmp_path):
        """Regression: a CL with edits + pure adds must include synthesized hunks for adds."""
        ws = tmp_path / "ws"
        ws.mkdir()
        edit_local = ws / "edit.py"
        add_local = ws / "new.py"
        edit_local.write_text("x = 2\n")
        add_local.write_text("")  # not needed; content comes from p4 print mock

        describe_out = (
            "Change 144072 by user@client on 2026/01/01\n"
            "\n"
            "\tMixed edit + add CL\n"
            "\n"
            "Affected files ...\n"
            "... //depot/edit.py#3 edit\n"
            "... //depot/new.py#1 add\n"
            "\n"
            "Differences ...\n"
            "\n"
            "==== //depot/edit.py#3 (text) ====\n"
            "@@ -1 +1 @@\n"
            "-x = 1\n"
            "+x = 2\n"
            "==== //depot/new.py#1 (text) ====\n"
            "\n"
        )
        where_out = (
            "... depotFile //depot/edit.py\n"
            f"... path {edit_local}\n"
            "\n"
            "... depotFile //depot/new.py\n"
            f"... path {add_local}\n"
        )
        info_out = f"... clientRoot {ws}\n"

        def fake_run_p4(args):
            if args[:2] == ["describe", "-du"] and "-S" not in args:
                return (0, describe_out, "")
            if args[:2] == ["-ztag", "where"]:
                return (0, where_out, "")
            if args[:2] == ["-ztag", "info"]:
                return (0, info_out, "")
            if args[:3] == ["-ztag", "reconcile", "-n"]:
                return (1, "", "no file(s) to reconcile.\n")
            if args[:4] == ["-ztag", "opened", "-c", "default"]:
                return (0, "", "")
            if args[:4] == ["-ztag", "resolve", "-n", "-c"]:
                return (1, "", "no file(s) to resolve.\n")
            if args[:2] == ["-ztag", "fstat"]:
                return (1, "", "no such file(s)")
            if args == ["print", "-q", "//depot/new.py#1"]:
                return (0, "def brief():\n    pass\n", "")
            raise AssertionError(f"unexpected p4 command: {args}")

        with patch.object(pr, "run_p4", side_effect=fake_run_p4):
            bundle = pr.build_bundle("144072", tmp_path / "bundle")

        # Both files in changed_files
        depots = [f["depot"] for f in bundle["changed_files"]]
        assert "//depot/edit.py" in depots
        assert "//depot/new.py" in depots

        diff = _concat_diff_from_chunks(bundle)
        # Edit hunk intact
        assert "+x = 2" in diff
        # Add hunk synthesized
        assert "@@ -0,0 +1,2 @@" in diff
        assert "+def brief():" in diff

    def test_add_only_shelved_cl(self, tmp_path):
        """A shelved CL containing only adds (no edits) must bundle full content."""
        ws = tmp_path / "ws"
        ws.mkdir()

        # Committed describe finds no Differences section -> shelved fallback.
        committed_out = "Change 1 by u@c on 2026/01/01\n\n\tdesc\n\nShelved files ...\n"
        shelved_out = (
            "Change 1 by u@c on 2026/01/01\n"
            "\n"
            "\tAdd new modules\n"
            "\n"
            "Shelved files ...\n"
            "\n"
            "... //depot/a.py#1 add\n"
            "... //depot/b.py#1 add\n"
            "\n"
            "Differences ...\n"
            "\n"
            "==== //depot/a.py#1 (text) ====\n"
            "\n"
            "==== //depot/b.py#1 (text) ====\n"
            "\n"
        )

        def fake_run_p4(args):
            if args[:2] == ["describe", "-du"] and "-S" not in args:
                return (0, committed_out, "")
            if args[:3] == ["describe", "-du", "-S"]:
                return (0, shelved_out, "")
            if args[:2] == ["-ztag", "where"]:
                return (0, "", "")  # no local mapping needed for this test
            if args[:2] == ["-ztag", "info"]:
                return (0, f"... clientRoot {ws}\n", "")
            if args[:3] == ["-ztag", "reconcile", "-n"]:
                return (1, "", "no file(s) to reconcile.\n")
            if args == ["print", "-q", "//depot/a.py@=1"]:
                return (0, "content of a\n", "")
            if args == ["print", "-q", "//depot/b.py@=1"]:
                return (0, "content of b\n", "")
            if args[:4] == ["-ztag", "resolve", "-n", "-c"]:
                return (1, "", "no file(s) to resolve.\n")
            if args[:2] == ["-ztag", "fstat"]:
                return (1, "", "no such file(s)")
            if args[:3] == ["-ztag", "opened", "-c"]:
                return (0, "", "")
            if args[:2] == ["fstat", "-T"]:
                return (1, "", "no such file(s)")
            raise AssertionError(f"unexpected p4 command: {args}")

        with patch.object(pr, "run_p4", side_effect=fake_run_p4):
            bundle = pr.build_bundle("1", tmp_path / "bundle")

        assert bundle["description"] == "Add new modules"
        diff = _concat_diff_from_chunks(bundle)
        assert "+content of a" in diff
        assert "+content of b" in diff

    def test_mixed_cl_adds_omitted_from_differences(self, tmp_path):
        """Regression (observed on a consuming project's CL): on some p4 servers, `describe -du -S`
        for a shelved mixed CL emits ==== headers ONLY for edits. Pure-adds appear only
        in the Shelved files listing. Previously these were silently dropped.
        """
        ws = tmp_path / "ws"
        ws.mkdir()

        committed_out = "Change 144098 by u@c on 2026/01/01 *pending*\n"
        shelved_out = (
            "Change 144098 by u@c on 2026/01/01 *pending*\n"
            "\n"
            "\tModule + facade wiring\n"
            "\n"
            "Shelved files ...\n"
            "\n"
            "... //depot/facade.cpp#4 edit\n"
            "... //depot/mod_a.cpp#1 add\n"
            "... //depot/mod_b.cpp#1 add\n"
            "... //depot/mod_c.cpp#1 add\n"
            "\n"
            "Differences ...\n"
            "\n"
            "==== //depot/facade.cpp#4 (text) ====\n"
            "@@ -1 +1 @@\n"
            "-wire_old();\n"
            "+wire_new();\n"
        )

        def fake_run_p4(args):
            if args[:2] == ["describe", "-du"] and "-S" not in args:
                return (0, committed_out, "")
            if args[:3] == ["describe", "-du", "-S"]:
                return (0, shelved_out, "")
            if args[:2] == ["-ztag", "where"]:
                return (0, "", "")
            if args[:2] == ["-ztag", "info"]:
                return (0, f"... clientRoot {ws}\n", "")
            if args[:3] == ["-ztag", "reconcile", "-n"]:
                return (1, "", "no file(s) to reconcile.\n")
            if args == ["print", "-q", "//depot/mod_a.cpp@=144098"]:
                return (0, "mod_a contents\n", "")
            if args == ["print", "-q", "//depot/mod_b.cpp@=144098"]:
                return (0, "mod_b contents\n", "")
            if args == ["print", "-q", "//depot/mod_c.cpp@=144098"]:
                return (0, "mod_c contents\n", "")
            if args[:3] == ["print", "-q", "-o"]:
                Path(args[3]).parent.mkdir(parents=True, exist_ok=True)
                Path(args[3]).write_text("wire_old();\n", encoding="utf-8")
                return (0, "", "")
            if args[:4] == ["-ztag", "resolve", "-n", "-c"]:
                return (1, "", "no file(s) to resolve.\n")
            if args[:2] == ["-ztag", "fstat"]:
                return (1, "", "no such file(s)")
            if args[:3] == ["-ztag", "opened", "-c"]:
                return (0, "", "")
            if args[:2] == ["fstat", "-T"]:
                return (1, "", "no such file(s)")
            raise AssertionError(f"unexpected p4 command: {args}")

        with patch.object(pr, "run_p4", side_effect=fake_run_p4):
            bundle = pr.build_bundle("144098", tmp_path / "bundle")

        # All four files in changed_files -- not just the edit
        depots = [f["depot"] for f in bundle["changed_files"]]
        assert depots == [
            "//depot/facade.cpp",
            "//depot/mod_a.cpp",
            "//depot/mod_b.cpp",
            "//depot/mod_c.cpp",
        ]

        diff = _concat_diff_from_chunks(bundle)
        # Edit preserved
        assert "+wire_new();" in diff
        # Adds synthesized with full content, even though they had no ==== header in Differences
        assert "==== //depot/mod_a.cpp#1" in diff
        assert "==== //depot/mod_b.cpp#1" in diff
        assert "==== //depot/mod_c.cpp#1" in diff
        assert "+mod_a contents" in diff
        assert "+mod_b contents" in diff
        assert "+mod_c contents" in diff


# ---------------------------------------------------------------------------
# fetch_shelf_fingerprint
# ---------------------------------------------------------------------------


class TestFetchShelfFingerprint:
    def test_empty_when_p4_fails(self):
        """`p4 fstat ... @=<CL>` with no shelf returns non-zero; treat as empty."""
        with patch.object(pr, "run_p4", return_value=(1, "", "no such file(s)")):
            scan = pr.fetch_shelf_fingerprint("123")
        assert scan.digests == {}
        assert scan.scan_ok is True

    def test_failure_is_marked_incomplete(self):
        with patch.object(pr, "run_p4", return_value=(1, "", "server unavailable")):
            scan = pr.fetch_shelf_fingerprint("123")
        assert scan.digests == {}
        assert scan.scan_ok is False
        assert scan.scan_reason == "server unavailable"

    def test_parses_multi_file_shelf(self):
        out = (
            "... depotFile //depot/a.cpp\n"
            "... headRev 3\n"
            "... digest AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n"
            "\n"
            "... depotFile //depot/b.cpp\n"
            "... headRev 5\n"
            "... digest BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB\n"
            "\n"
        )
        with patch.object(pr, "run_p4", return_value=(0, out, "")) as mock:
            scan = pr.fetch_shelf_fingerprint("123")
        assert scan.digests == {
            "//depot/a.cpp": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            "//depot/b.cpp": "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
        }
        # Confirm the spec uses the shelved revspec.
        assert mock.call_args[0][0] == ["-ztag", "fstat", "-Ol", "//...@=123"]

    def test_delete_with_no_digest_recorded_as_empty(self):
        """Shelved deletes have no digest; record as empty string so file presence still counts."""
        out = (
            "... depotFile //depot/gone.cpp\n"
            "... headRev 7\n"
            "... headAction delete\n"
            "\n"
        )
        with patch.object(pr, "run_p4", return_value=(0, out, "")):
            scan = pr.fetch_shelf_fingerprint("123")
        assert scan.digests == {"//depot/gone.cpp": ""}
        assert scan.actions == {"//depot/gone.cpp": "delete"}

    def test_no_trailing_blank_line_still_captured(self):
        """Last record may not end with blank line; must still be parsed."""
        out = (
            "... depotFile //depot/a.cpp\n"
            "... digest AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        )
        with patch.object(pr, "run_p4", return_value=(0, out, "")):
            scan = pr.fetch_shelf_fingerprint("123")
        assert scan.digests == {
            "//depot/a.cpp": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        }


class TestShelfDivergence:
    def _fingerprint(self) -> "pr.ShelfScanResult":
        return pr.ShelfScanResult(
            digests={
                "//depot/a.cpp": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
                "//depot/gone.cpp": "",
            },
            actions={
                "//depot/a.cpp": "edit",
                "//depot/gone.cpp": "delete",
            },
        )

    def test_opened_after_shelving_is_reported(self):
        opened = "... depotFile //depot/new.cpp\n... action add\n\n"
        with patch.object(pr, "run_p4", return_value=(0, opened, "")):
            divergence, incomplete = pr.shelf_divergence("123", self._fingerprint())
        assert {"depot": "//depot/new.cpp", "kind": "opened after shelving"} in divergence
        assert incomplete == []

    def test_missing_open_is_reported(self):
        opened = "... depotFile //depot/a.cpp\n... action edit\n\n"
        with patch.object(pr, "run_p4", return_value=(0, opened, "")):
            divergence, incomplete = pr.shelf_divergence("123", self._fingerprint())
        assert {"depot": "//depot/gone.cpp", "kind": "not open in CL"} in divergence
        assert incomplete == []

    def test_action_change_is_reported(self):
        opened = "... depotFile //depot/a.cpp\n... action delete\n\n"
        with patch.object(pr, "run_p4", return_value=(0, opened, "")):
            divergence, incomplete = pr.shelf_divergence("123", self._fingerprint())
        assert {"depot": "//depot/a.cpp", "kind": "open action differs"} in divergence
        assert incomplete == []

    def test_opened_failure_is_incomplete(self):
        with patch.object(pr, "run_p4", return_value=(1, "", "server unavailable")):
            opened, incomplete = pr.fetch_opened_files("123")
        assert opened == {}
        assert incomplete == [
            {"scan": "shelf_opened", "reason": "server unavailable"}
        ]

# ---------------------------------------------------------------------------
# auto_shelve_cl
# ---------------------------------------------------------------------------


class TestAutoShelveCl:
    def test_shelve_failure_raises(self):
        with patch.object(pr, "run_p4", return_value=(1, "", "perm denied")):
            with pytest.raises(ValueError, match="p4 shelve -c 123 failed"):
                pr.auto_shelve_cl("123")

    def test_shelve_success_but_empty_shelf_raises(self):
        """Pathological: shelve reports success but no shelved files appear."""
        def side(args):
            if args[0] == "shelve":
                return (0, "Change 123 files shelved.\n", "")
            return (0, "", "")  # fstat returns nothing -> empty fingerprint

        with patch.object(pr, "run_p4", side_effect=side):
            with pytest.raises(ValueError, match="no shelved files were found"):
                pr.auto_shelve_cl("123")

    def test_returns_post_shelve_fingerprint(self):
        fstat_out = (
            "... depotFile //depot/x.cpp\n"
            "... digest ABCDEF0123456789ABCDEF0123456789\n"
            "\n"
        )

        def side(args):
            if args[0] == "shelve":
                return (0, "Change 123 files shelved.\n", "")
            return (0, fstat_out, "")

        with patch.object(pr, "run_p4", side_effect=side) as mock:
            scan = pr.auto_shelve_cl("123")
        assert get_type_hints(pr.auto_shelve_cl)["return"] is pr.ShelfScanResult
        assert isinstance(scan, pr.ShelfScanResult)
        assert scan.digests == {
            "//depot/x.cpp": "ABCDEF0123456789ABCDEF0123456789"
        }
        # First call is the shelve, second is the fingerprint fstat.
        assert mock.call_args_list[0][0][0] == ["shelve", "-c", "123"]
        assert mock.call_args_list[1][0][0] == ["-ztag", "fstat", "-Ol", "//...@=123"]


# ---------------------------------------------------------------------------
# cleanup_auto_shelve
# ---------------------------------------------------------------------------


class TestCleanupAutoShelve:
    def _write_bundle(self, tmp_path: Path, bundle: dict) -> Path:
        bundle_dir = tmp_path / bundle["cl"]
        bundle_dir.mkdir(parents=True, exist_ok=True)
        (bundle_dir / "bundle.json").write_text(json.dumps(bundle), encoding="utf-8")
        return bundle_dir

    def test_missing_bundle_returns_1(self, tmp_path, capsys):
        rc = pr.cleanup_auto_shelve(tmp_path / "nope")
        assert rc == 1
        assert "no bundle.json" in capsys.readouterr().err

    def test_not_auto_shelved_silent_noop(self, tmp_path, capsys):
        """auto_shelved=false means we did not create the shelf -- do nothing, silently."""
        bundle_dir = self._write_bundle(
            tmp_path,
            {"cl": "123", "auto_shelved": False, "shelf_fingerprint": {}},
        )
        with patch.object(pr, "run_p4") as mock:
            rc = pr.cleanup_auto_shelve(bundle_dir)
        assert rc == 0
        assert capsys.readouterr().err == ""
        assert mock.call_count == 0  # no p4 calls at all

    def test_fingerprint_match_triggers_delete(self, tmp_path, capsys):
        bundle_dir = self._write_bundle(
            tmp_path,
            {
                "cl": "123",
                "auto_shelved": True,
                "shelf_fingerprint": {"//depot/x.cpp": "DEADBEEF"},
            },
        )
        fstat_out = (
            "... depotFile //depot/x.cpp\n"
            "... digest DEADBEEF\n"
            "\n"
        )

        calls = []

        def side(args):
            calls.append(args)
            if args[0] == "-ztag" and args[1] == "fstat":
                return (0, fstat_out, "")
            if args[:2] == ["shelve", "-d"]:
                return (0, "Shelf deleted.\n", "")
            return (0, "", "")

        with patch.object(pr, "run_p4", side_effect=side):
            rc = pr.cleanup_auto_shelve(bundle_dir)
        assert rc == 0
        assert any(c[:3] == ["shelve", "-d", "-c"] for c in calls)
        assert "deleted auto-created shelf" in capsys.readouterr().err

    def test_fingerprint_mismatch_skips_delete(self, tmp_path, capsys):
        """Author reshelved with different content; leave their work alone."""
        bundle_dir = self._write_bundle(
            tmp_path,
            {
                "cl": "123",
                "auto_shelved": True,
                "shelf_fingerprint": {"//depot/x.cpp": "DEADBEEF"},
            },
        )
        fstat_out = (
            "... depotFile //depot/x.cpp\n"
            "... digest CAFEBABE\n"
            "\n"
        )
        calls = []

        def side(args):
            calls.append(args)
            return (0, fstat_out, "")

        with patch.object(pr, "run_p4", side_effect=side):
            rc = pr.cleanup_auto_shelve(bundle_dir)
        assert rc == 0
        assert not any(c[:2] == ["shelve", "-d"] for c in calls)
        assert "shelf changed" in capsys.readouterr().err

    def test_fingerprint_extra_file_skips_delete(self, tmp_path, capsys):
        """Author added a file to the shelf since we recorded it; don't delete."""
        bundle_dir = self._write_bundle(
            tmp_path,
            {
                "cl": "123",
                "auto_shelved": True,
                "shelf_fingerprint": {"//depot/x.cpp": "DEADBEEF"},
            },
        )
        fstat_out = (
            "... depotFile //depot/x.cpp\n"
            "... digest DEADBEEF\n"
            "\n"
            "... depotFile //depot/y.cpp\n"
            "... digest 12345678\n"
            "\n"
        )
        calls = []

        def side(args):
            calls.append(args)
            return (0, fstat_out, "")

        with patch.object(pr, "run_p4", side_effect=side):
            rc = pr.cleanup_auto_shelve(bundle_dir)
        assert rc == 0
        assert not any(c[:2] == ["shelve", "-d"] for c in calls)

    def test_shelf_gone_noop(self, tmp_path, capsys):
        """Author already submitted or deleted the shelf; nothing to do."""
        bundle_dir = self._write_bundle(
            tmp_path,
            {
                "cl": "123",
                "auto_shelved": True,
                "shelf_fingerprint": {"//depot/x.cpp": "DEADBEEF"},
            },
        )
        calls = []

        def side(args):
            calls.append(args)
            return (1, "", "no such file(s)")  # empty shelf

        with patch.object(pr, "run_p4", side_effect=side):
            rc = pr.cleanup_auto_shelve(bundle_dir)
        assert rc == 0
        assert not any(c[:2] == ["shelve", "-d"] for c in calls)
        assert "already gone" in capsys.readouterr().err

    def test_delete_failure_returns_1(self, tmp_path, capsys):
        bundle_dir = self._write_bundle(
            tmp_path,
            {
                "cl": "123",
                "auto_shelved": True,
                "shelf_fingerprint": {"//depot/x.cpp": "DEADBEEF"},
            },
        )
        fstat_out = (
            "... depotFile //depot/x.cpp\n"
            "... digest DEADBEEF\n"
            "\n"
        )

        def side(args):
            if args[0] == "-ztag" and args[1] == "fstat":
                return (0, fstat_out, "")
            return (1, "", "shelf is locked")

        with patch.object(pr, "run_p4", side_effect=side):
            rc = pr.cleanup_auto_shelve(bundle_dir)
        assert rc == 1
        assert "shelf is locked" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# build_bundle -- auto-shelve integration
# ---------------------------------------------------------------------------


class TestBuildBundleAutoShelve:
    def _committed_describe(self) -> str:
        """Minimal committed describe output for a hand-off to the rest of build_bundle."""
        return (
            "Change 1 by u@c on 2026/01/01 12:00:00\n"
            "\n"
            "\tdesc\n"
            "\n"
            "Affected files ...\n"
            "\n"
            "... //depot/new.py#1 add\n"
            "\n"
            "Differences ...\n"
            "==== //depot/new.py#1 (text) ====\n"
            "@@ -0,0 +1,1 @@\n"
            "+hello\n"
        )

    def test_pending_unshelved_triggers_auto_shelve(self, tmp_path):
        """build_bundle catches PendingUnshelvedError, shelves, retries, marks auto_shelved=true."""
        shelved_describe = self._committed_describe()
        shelve_calls = []
        fstat_calls = 0

        def fake_fetch_describe(cl):
            # First call raises; second call (after auto-shelve) succeeds.
            if not shelve_calls:
                raise pr.PendingUnshelvedError("no shelf")
            return shelved_describe, True

        def fake_fingerprint(cl):
            nonlocal fstat_calls
            fstat_calls += 1
            # F0 precedes describe, then the pre-shelve race check repeats at
            # the failure boundary. auto_shelve_cl returns the post-shelve F0.
            if fstat_calls <= 2:
                return pr.ShelfScanResult()
            return pr.ShelfScanResult(
                digests={"//depot/new.py": "DEADBEEF"}
            )

        def fake_auto_shelve(cl):
            shelve_calls.append(cl)
            return pr.ShelfScanResult(
                digests={"//depot/new.py": "DEADBEEF"}
            )

        with patch.object(pr, "fetch_describe", side_effect=fake_fetch_describe), \
                patch.object(pr, "fetch_shelf_fingerprint", side_effect=fake_fingerprint), \
                patch.object(pr, "auto_shelve_cl", side_effect=fake_auto_shelve), \
                patch.object(pr, "resolve_local_paths", return_value={"//depot/new.py": None}), \
                patch.object(pr, "get_workspace_root", return_value=(None, None)), \
                patch.object(pr, "find_unreconciled", return_value=([], [])), \
                patch.object(pr, "find_unresolved", return_value=([], [])):
            bundle = pr.build_bundle("123", tmp_path / "bundle")

        assert bundle["auto_shelved"] is True
        assert bundle["shelf_fingerprint"] == {"//depot/new.py": "DEADBEEF"}
        assert shelve_calls == ["123"]

    def test_pre_shelve_race_preserves_foreign_shelf(self, tmp_path):
        """A shelf found by the race check remains owned by its author."""
        shelved_describe = self._committed_describe()
        describe_calls = 0

        def fake_fetch_describe(cl):
            nonlocal describe_calls
            describe_calls += 1
            if describe_calls == 1:
                raise pr.PendingUnshelvedError("no shelf")
            return shelved_describe, True

        race_shelf = pr.ShelfScanResult(
            digests={"//depot/other.py": "FEEDFACE"}
        )

        with patch.object(pr, "fetch_describe", side_effect=fake_fetch_describe), \
                patch.object(pr, "fetch_shelf_fingerprint", return_value=race_shelf) as fingerprint_mock, \
                patch.object(
                    pr,
                    "auto_shelve_cl",
                    return_value=pr.ShelfScanResult(
                        digests={"//depot/new.py": "DEADBEEF"}
                    ),
                ) as shelve_mock, \
                patch.object(pr, "resolve_local_paths", return_value={"//depot/new.py": None}), \
                patch.object(pr, "get_workspace_root", return_value=(None, None)), \
                patch.object(pr, "find_unreconciled", return_value=([], [])), \
                patch.object(pr, "find_unresolved", return_value=([], [])):
            bundle = pr.build_bundle("123", tmp_path / "bundle")

        shelve_mock.assert_not_called()
        assert bundle["auto_shelved"] is False
        assert bundle["shelf_fingerprint"] == {}
        assert fingerprint_mock.call_args_list == [call("123"), call("123")]

    def test_failed_race_scan_refuses_auto_shelve(self, tmp_path):
        failed_scan = pr.ShelfScanResult(
            scan_ok=False,
            scan_reason="server unavailable",
        )

        with patch.object(
            pr,
            "fetch_describe",
            side_effect=pr.PendingUnshelvedError("no shelf"),
        ), patch.object(
            pr,
            "fetch_shelf_fingerprint",
            return_value=failed_scan,
        ), patch.object(
            pr,
            "auto_shelve_cl",
            return_value=pr.ShelfScanResult(digests={"//depot/new.py": "A"}),
        ) as shelve_mock:
            with pytest.raises(ValueError) as exc:
                pr.build_bundle("123", tmp_path / "bundle")

        shelve_mock.assert_not_called()
        assert "could not check CL 123 for a shelf" in str(exc.value)

    def test_normal_path_records_no_auto_shelve(self, tmp_path):
        """When fetch_describe succeeds directly, no shelve happens and the bundle reflects that."""
        shelved_describe = self._committed_describe()

        with patch.object(pr, "fetch_describe", return_value=(shelved_describe, False)), \
                patch.object(pr, "auto_shelve_cl") as shelve_mock, \
                patch.object(pr, "fetch_shelf_fingerprint", return_value=pr.ShelfScanResult()), \
                patch.object(pr, "resolve_local_paths", return_value={"//depot/new.py": None}), \
                patch.object(pr, "get_workspace_root", return_value=(None, None)), \
                patch.object(pr, "find_unreconciled", return_value=([], [])), \
                patch.object(pr, "find_unresolved", return_value=([], [])):
            bundle = pr.build_bundle("123", tmp_path / "bundle")

        assert shelve_mock.call_count == 0
        assert bundle["auto_shelved"] is False
        assert bundle["shelf_fingerprint"] == {}

    def test_auto_shelve_fingerprint_is_not_refetched(self, tmp_path):
        """auto_shelve_cl shelves, fetches the fingerprint, and RETURNS it.
        build_bundle must capture and use that return value directly rather
        than dropping it and re-fetching via a second fstat round trip --
        two depot-wide fstats where one suffices, and the recorded
        fingerprint would not be the one auto_shelve_cl validated."""
        shelved_describe = self._committed_describe()
        describe_calls = {"n": 0}
        shelved = {"done": False}
        fstat_calls_before_shelve = {"n": 0}
        fstat_calls_after_shelve = {"n": 0}
        fstat_out = (
            "... depotFile //depot/new.py\n"
            "... digest DEADBEEF\n"
            "\n"
        )

        def fake_fetch_describe(cl):
            describe_calls["n"] += 1
            if describe_calls["n"] == 1:
                raise pr.PendingUnshelvedError("no shelf")
            return shelved_describe, True

        def fake_run_p4(args):
            if args[:2] == ["-ztag", "fstat"]:
                if shelved["done"]:
                    fstat_calls_after_shelve["n"] += 1
                    return (0, fstat_out, "")
                fstat_calls_before_shelve["n"] += 1
                return (0, "", "")  # pre-shelve race check: no shelf yet
            if args[:2] == ["shelve", "-c"]:
                shelved["done"] = True
                return (0, "Change 123 files shelved.\n", "")
            return (1, "", f"unexpected: {args}")

        with patch.object(pr, "fetch_describe", side_effect=fake_fetch_describe), \
                patch.object(pr, "run_p4", side_effect=fake_run_p4), \
                patch.object(pr, "resolve_local_paths", return_value={"//depot/new.py": None}), \
                patch.object(pr, "get_workspace_root", return_value=(None, None)), \
                patch.object(pr, "find_unreconciled", return_value=([], [])), \
                patch.object(pr, "find_unresolved", return_value=([], [])):
            bundle = pr.build_bundle("123", tmp_path / "bundle")

        assert bundle["auto_shelved"] is True
        assert type(bundle["shelf_fingerprint"]) is dict
        assert bundle["shelf_fingerprint"] == {"//depot/new.py": "DEADBEEF"}
        assert fstat_calls_before_shelve["n"] == 2
        assert fstat_calls_after_shelve["n"] == 1


class TestBuildBundleShelfState:
    def _pending_describe(self) -> str:
        return (
            "Change 123 by u@c on 2026/01/01 12:00:00 *pending*\n"
            "\n\tdesc\n\n"
            "Shelved files ...\n\n"
            "... //depot/a.cpp#1 edit\n\n"
            "Differences ...\n"
            "==== //depot/a.cpp#1 (text) ====\n"
            "@@ -1 +1 @@\n"
            "+hello\n"
        )

    @pytest.mark.parametrize(
        ("opened", "expected"),
        [
            (
                "... depotFile //depot/new.cpp\n... action add\n\n",
                "//depot/new.cpp (opened after shelving)",
            ),
            (
                "",
                "//depot/a.cpp (not open in CL)",
            ),
            (
                "... depotFile //depot/a.cpp\n... action delete\n\n",
                "//depot/a.cpp (open action differs)",
            ),
        ],
    )
    def test_shelf_divergence_refuses_with_repair_command(self, opened, expected, tmp_path):
        shelf = pr.ShelfScanResult(
            digests={"//depot/a.cpp": "A"},
            actions={"//depot/a.cpp": "edit"},
        )
        with patch.object(pr, "fetch_describe", return_value=(self._pending_describe(), True)), \
                patch.object(pr, "fetch_shelf_fingerprint", return_value=shelf), \
                patch.object(pr, "run_p4", return_value=(0, opened, "")):
            with pytest.raises(ValueError, match=r"p4 shelve -f -c 123") as exc:
                pr.build_bundle("123", tmp_path / "bundle")
        assert expected in str(exc.value)

    def test_opened_failure_does_not_refuse_and_is_incomplete(self, tmp_path):
        shelf = pr.ShelfScanResult(digests={"//depot/a.cpp": "A"})
        with patch.object(pr, "fetch_describe", return_value=(self._pending_describe(), True)), \
                patch.object(pr, "fetch_shelf_fingerprint", return_value=shelf), \
                patch.object(pr, "run_p4", return_value=(1, "", "server unavailable")), \
                patch.object(pr, "resolve_local_paths", return_value={"//depot/a.cpp": None}), \
                patch.object(pr, "get_workspace_root", return_value=(None, None)), \
                patch.object(pr, "find_unreconciled", return_value=([], [])), \
                patch.object(pr, "find_unresolved", return_value=([], [])):
            bundle = pr.build_bundle("123", tmp_path / "bundle")
        assert bundle["hygiene_incomplete"] == [
            {"scan": "shelf_opened", "reason": "server unavailable"}
        ]

    def test_matching_shelf_does_not_refuse(self, tmp_path):
        shelf = pr.ShelfScanResult(
            digests={"//depot/a.cpp": "A"},
            actions={"//depot/a.cpp": "edit"},
        )
        with patch.object(pr, "fetch_describe", return_value=(self._pending_describe(), True)), \
                patch.object(pr, "fetch_shelf_fingerprint", return_value=shelf), \
                patch.object(pr, "fetch_opened_files", return_value=({"//depot/a.cpp": "edit"}, [])), \
                patch.object(pr, "resolve_local_paths", return_value={"//depot/a.cpp": None}), \
                patch.object(pr, "get_workspace_root", return_value=(None, None)), \
                patch.object(pr, "find_unreconciled", return_value=([], [])), \
                patch.object(pr, "find_unresolved", return_value=([], [])):
            bundle = pr.build_bundle("123", tmp_path / "bundle")
        assert bundle["shelf_drift"] == []
        assert bundle["shelf_fingerprint"] == {}

    def test_empty_opened_result_is_not_refetched(self, tmp_path):
        with patch.object(pr, "fetch_describe", return_value=(self._pending_describe(), True)), \
                patch.object(pr, "fetch_shelf_fingerprint", return_value=pr.ShelfScanResult()), \
                patch.object(pr, "fetch_opened_files", return_value=({}, [])) as opened_mock, \
                patch.object(pr, "resolve_local_paths", return_value={"//depot/a.cpp": None}), \
                patch.object(pr, "get_workspace_root", return_value=(None, None)), \
                patch.object(pr, "find_unreconciled", return_value=([], [])), \
                patch.object(pr, "find_unresolved", return_value=([], [])):
            pr.build_bundle("123", tmp_path / "bundle")

        opened_mock.assert_called_once_with("123")

    def test_auto_created_shelf_skips_divergence_check(self, tmp_path):
        describe = self._pending_describe()
        shelf = pr.ShelfScanResult(
            digests={"//depot/a.cpp": "A"},
            actions={"//depot/a.cpp": "edit"},
        )
        fetch_calls = iter([pr.PendingUnshelvedError("no shelf"), (describe, True)])
        with patch.object(pr, "fetch_describe", side_effect=fetch_calls), \
                patch.object(pr, "fetch_shelf_fingerprint", return_value=pr.ShelfScanResult()), \
                patch.object(pr, "auto_shelve_cl", return_value=shelf), \
                patch.object(pr, "fetch_opened_files", return_value=({"//depot/new.cpp": "add"}, [])), \
                patch.object(pr, "shelf_divergence", side_effect=AssertionError("called")), \
                patch.object(pr, "resolve_local_paths", return_value={"//depot/a.cpp": None}), \
                patch.object(pr, "get_workspace_root", return_value=(None, None)), \
                patch.object(pr, "find_unreconciled", return_value=([], [])), \
                patch.object(pr, "find_unresolved", return_value=([], [])):
            bundle = pr.build_bundle("123", tmp_path / "bundle")
        assert bundle["auto_shelved"] is True

    def test_submitted_cl_skips_divergence_check(self, tmp_path):
        describe = self._pending_describe().replace(" *pending*", "")
        shelf = pr.ShelfScanResult(
            digests={"//depot/a.cpp": "A"},
            actions={"//depot/a.cpp": "edit"},
        )
        with patch.object(pr, "fetch_describe", return_value=(describe, False)), \
                patch.object(pr, "fetch_shelf_fingerprint", return_value=shelf), \
                patch.object(pr, "fetch_opened_files", return_value=({"//depot/new.cpp": "add"}, [])), \
                patch.object(pr, "resolve_local_paths", return_value={"//depot/a.cpp": None}), \
                patch.object(pr, "get_workspace_root", return_value=(None, None)), \
                patch.object(pr, "find_unreconciled", return_value=([], [])), \
                patch.object(pr, "find_unresolved", return_value=([], [])):
            bundle = pr.build_bundle("123", tmp_path / "bundle")
        assert bundle["shelf_drift"] == []

    def test_content_drift_warns_and_returns_bundle(self, tmp_path, capsys):
        local = tmp_path / "a.cpp"
        local.write_text("workspace\n", encoding="utf-8")
        shelf = pr.ShelfScanResult(
            digests={"//depot/a.cpp": "A"},
            actions={"//depot/a.cpp": "edit"},
        )
        opened = {"//depot/a.cpp": "edit"}
        with patch.object(pr, "fetch_describe", return_value=(self._pending_describe(), True)), \
                patch.object(pr, "fetch_shelf_fingerprint", return_value=shelf) as fingerprint_mock, \
                patch.object(pr, "fetch_opened_files", return_value=(opened, [])) as opened_mock, \
                patch.object(pr, "resolve_local_paths", return_value={"//depot/a.cpp": str(local)}), \
                patch.object(pr, "get_workspace_root", return_value=(None, None)), \
                patch.object(pr, "find_unreconciled", return_value=([], [])), \
                patch.object(pr, "find_default_open", return_value=([], [])), \
                patch.object(pr, "find_unresolved", return_value=([], [])):
            bundle = pr.build_bundle("123", tmp_path / "bundle")
        assert bundle["shelf_drift"] == [{"depot": "//depot/a.cpp", "local": str(local)}]
        assert "shelf content differs" in capsys.readouterr().err
        assert fingerprint_mock.call_count == 1
        assert opened_mock.call_count == 1

    def test_unhashable_path_is_incomplete_not_clean(self, tmp_path):
        missing = tmp_path / "missing.cpp"
        shelf = pr.ShelfScanResult(
            digests={"//depot/a.cpp": "A"},
            actions={"//depot/a.cpp": "edit"},
        )
        with patch.object(pr, "fetch_describe", return_value=(self._pending_describe(), True)), \
                patch.object(pr, "fetch_shelf_fingerprint", return_value=shelf), \
                patch.object(pr, "fetch_opened_files", return_value=({"//depot/a.cpp": "edit"}, [])), \
                patch.object(pr, "resolve_local_paths", return_value={"//depot/a.cpp": str(missing)}), \
                patch.object(pr, "get_workspace_root", return_value=(None, None)), \
                patch.object(pr, "find_unreconciled", return_value=([], [])), \
                patch.object(pr, "find_default_open", return_value=([], [])), \
                patch.object(pr, "find_unresolved", return_value=([], [])):
            bundle = pr.build_bundle("123", tmp_path / "bundle")
        assert bundle["shelf_drift"] == []
        assert len(bundle["hygiene_incomplete"]) == 1
        incomplete = bundle["hygiene_incomplete"][0]
        assert incomplete["scan"] == "shelf_drift"
        assert incomplete["reason"].startswith("could not hash //depot/a.cpp:")
        assert str(missing) in incomplete["reason"]

    def test_empty_local_path_is_incomplete_not_clean(self):
        drift, incomplete = pr._shelf_content_drift(
            {"//depot/a.cpp": "A"},
            {"//depot/a.cpp": "edit"},
            {"//depot/a.cpp": ""},
        )
        assert drift == []
        assert incomplete == [
            {"scan": "shelf_drift", "reason": "no local mapping for //depot/a.cpp"}
        ]


# ---------------------------------------------------------------------------
# main -- CLI
# ---------------------------------------------------------------------------


class TestMain:
    def test_no_args_returns_2(self, capsys):
        rc = pr.main(["prepare_review.py"])
        assert rc == 2
        assert "Usage" in capsys.readouterr().err

    def test_value_error_returns_1(self, capsys, tmp_path):
        with patch.object(pr, "DEFAULT_BUNDLE_ROOT", tmp_path / "reviews"), \
                patch.object(pr, "build_bundle", side_effect=ValueError("nope")):
            rc = pr.main(["prepare_review.py", "123"])
        assert rc == 1
        assert "nope" in capsys.readouterr().err

    def test_missing_p4_executable_returns_1_not_a_traceback(self, capsys, tmp_path):
        """subprocess.run raises FileNotFoundError when `p4` is absent from
        PATH; main() must catch it and print an actionable message rather than
        letting it escape as an unhandled traceback."""
        with patch.object(pr, "DEFAULT_BUNDLE_ROOT", tmp_path / "reviews"), \
                patch.object(
                    pr, "build_bundle",
                    side_effect=FileNotFoundError(2, "No such file or directory", "p4"),
                ):
            rc = pr.main(["prepare_review.py", "123"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "p4" in err
        assert "PATH" in err or "Helix" in err

    def test_missing_p4_executable_during_cleanup_returns_1_not_a_traceback(self, capsys, tmp_path):
        bundle_dir = tmp_path / "bundle"
        with patch.object(
            pr, "cleanup_auto_shelve",
            side_effect=FileNotFoundError(2, "No such file or directory", "p4"),
        ):
            rc = pr.main(["prepare_review.py", "--cleanup", str(bundle_dir)])
        assert rc == 1
        err = capsys.readouterr().err
        assert "p4" in err

    def test_success_prints_json_and_persists_bundle(self, capsys, tmp_path):
        """main() prints the bundle to stdout AND persists bundle.json next to chunks."""
        reviews_root = tmp_path / "reviews"
        fake_bundle = {"cl": "123", "bundle_dir": str(reviews_root / "123")}
        with patch.object(pr, "DEFAULT_BUNDLE_ROOT", reviews_root), \
                patch.object(pr, "build_bundle", return_value=fake_bundle):
            rc = pr.main(["prepare_review.py", "123"])
        assert rc == 0
        captured = capsys.readouterr()
        assert json.loads(captured.out) == fake_bundle
        persisted = json.loads((reviews_root / "123" / "bundle.json").read_text())
        assert persisted == fake_bundle

    def test_cleanup_routes_to_cleanup_auto_shelve(self, tmp_path):
        bundle_dir = tmp_path / "bundle"
        with patch.object(pr, "cleanup_auto_shelve", return_value=0) as mock:
            rc = pr.main(["prepare_review.py", "--cleanup", str(bundle_dir)])
        assert rc == 0
        assert mock.call_args[0][0] == bundle_dir

    def test_cleanup_without_bundle_dir_returns_2(self, capsys):
        rc = pr.main(["prepare_review.py", "--cleanup"])
        assert rc == 2
        assert "Usage" in capsys.readouterr().err

    def test_non_numeric_cl_rejected_before_mkdir(self, tmp_path, capsys):
        """`../../foo` or `.` as the positional would otherwise be joined onto
        DEFAULT_BUNDLE_ROOT unvalidated and mkdir'd, writing outside the
        reviews root (or into the root itself, beside ledger.json)."""
        with patch.object(pr, "DEFAULT_BUNDLE_ROOT", tmp_path / "reviews"), \
                patch.object(Path, "mkdir") as mkdir_mock:
            rc = pr.main(["prepare_review.py", "../../foo"])
        assert rc == 2
        assert mkdir_mock.call_count == 0
        assert "Error" in capsys.readouterr().err

    def test_dot_cl_rejected_before_mkdir(self, tmp_path, capsys):
        with patch.object(pr, "DEFAULT_BUNDLE_ROOT", tmp_path / "reviews"), \
                patch.object(Path, "mkdir") as mkdir_mock:
            rc = pr.main(["prepare_review.py", "."])
        assert rc == 2
        assert mkdir_mock.call_count == 0
        assert "Error" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Binary/filetype guard in add/delete hunk synthesis (G5)
# ---------------------------------------------------------------------------


class TestIsTextFiletype:
    @pytest.mark.parametrize(
        "filetype",
        [
            "text", "text+x", "text+ko", "ktext", "xtext", "ltext",
            "unicode", "utf8", "utf16", "symlink",
            # Unknown/missing types default to text (historical behavior).
            "", None, "weirdtype",
        ],
    )
    def test_text_like(self, filetype):
        assert pr._is_text_filetype(filetype) is True

    @pytest.mark.parametrize(
        "filetype",
        [
            "binary", "binary+l", "binary+lFS64", "xbinary", "ubinary",
            "apple", "resource", "uresource", "tempobj", "ctempobj",
            "Binary",  # case-insensitive
        ],
    )
    def test_binary_like(self, filetype):
        assert pr._is_text_filetype(filetype) is False


class TestSplitDiffSectionsCapturesType:
    def test_type_field_captured_from_header(self):
        diff = (
            "==== //depot/a.cpp#1 (text) ====\n"
            "@@ -1 +1 @@\n"
            "==== //depot/b.uasset#2 (binary+l) ====\n"
            "\n"
        )
        _, sections = pr.split_diff_sections(diff)
        assert sections[0]["type"] == "text"
        assert sections[1]["type"] == "binary+l"


class TestExtractDiffBinaryGuard:
    def test_binary_add_in_differences_gets_placeholder_not_content(self):
        """A shelved binary add with a ==== header must NOT be content-inlined."""
        describe = (
            "Shelved files ...\n"
            "... //depot/Content/big.uasset#1 add\n"
            "Differences ...\n"
            "\n"
            "==== //depot/Content/big.uasset#1 (binary+l) ====\n"
            "\n"
        )
        actions = pr.parse_file_actions(describe)
        calls: list[list[str]] = []

        def fake_run_p4(args):
            calls.append(args)
            if args[:1] == ["fstat"] and "fileSize" in args:
                return (0, "... fileSize 123456\n", "")
            return (1, "", f"unexpected: {args}")

        with patch.object(pr, "run_p4", side_effect=fake_run_p4):
            diff = pr.extract_diff(describe, actions, cl="99", is_shelved=True)

        assert "==== //depot/Content/big.uasset#1 (binary+l)" in diff
        assert "(binary file added: 123456 bytes)" in diff
        # Content was never fetched -- no mojibake inlining.
        assert not any(args[:1] == ["print"] for args in calls)
        assert "@@ -0,0" not in diff

    def test_binary_delete_in_differences_gets_placeholder(self):
        describe = (
            "Affected files ...\n"
            "... //depot/old.bin#5 delete\n"
            "Differences ...\n"
            "\n"
            "==== //depot/old.bin#5 (binary) ====\n"
            "\n"
        )
        actions = pr.parse_file_actions(describe)

        def fake_run_p4(args):
            if args[:1] == ["fstat"] and "fileSize" in args:
                # Size of the prior rev (#4).
                assert args[-1] == "//depot/old.bin#4"
                return (0, "... fileSize 2048\n", "")
            return (1, "", f"unexpected: {args}")

        with patch.object(pr, "run_p4", side_effect=fake_run_p4):
            diff = pr.extract_diff(describe, actions, cl="100", is_shelved=False)

        assert "(binary file deleted: 2048 bytes)" in diff
        assert "@@ -1," not in diff

    def test_binary_add_size_unavailable_says_size_unknown(self):
        describe = (
            "Shelved files ...\n"
            "... //depot/x.uasset#1 add\n"
            "Differences ...\n"
            "\n"
            "==== //depot/x.uasset#1 (binary) ====\n"
            "\n"
        )
        actions = pr.parse_file_actions(describe)
        with patch.object(pr, "run_p4", return_value=(1, "", "fstat failed")):
            diff = pr.extract_diff(describe, actions, cl="7", is_shelved=True)
        assert "(binary file added: size unknown)" in diff

    def test_binary_add_omitted_from_differences_uses_fstat_type(self):
        """Files with no ==== header have no inline type; fstat supplies it."""
        describe = (
            "Shelved files ...\n"
            "... //depot/edit.cpp#3 edit\n"
            "... //depot/asset.uasset#1 add\n"
            "Differences ...\n"
            "\n"
            "==== //depot/edit.cpp#3 (text) ====\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        )
        actions = pr.parse_file_actions(describe)
        calls: list[list[str]] = []

        def fake_run_p4(args):
            calls.append(args)
            if args[:1] == ["fstat"] and "type,headType" in args:
                return (0, "... type binary+l\n", "")
            if args[:1] == ["fstat"] and "fileSize" in args:
                return (0, "... fileSize 99\n", "")
            return (1, "", f"unexpected: {args}")

        with patch.object(pr, "run_p4", side_effect=fake_run_p4):
            diff = pr.extract_diff(describe, actions, cl="50", is_shelved=True)

        # Synthesized header carries the discovered type, not hardcoded (text).
        assert "==== //depot/asset.uasset#1 (binary+l) ====" in diff
        assert "(binary file added: 99 bytes)" in diff
        assert not any(args[:1] == ["print"] for args in calls)
        # The text edit is untouched.
        assert "+new" in diff

    def test_text_add_omitted_from_differences_still_inlined(self):
        """fstat says text -> the historical synthesis path still runs."""
        describe = (
            "Shelved files ...\n"
            "... //depot/new.py#1 add\n"
            "Differences ...\n"
            "\n"
        )
        actions = pr.parse_file_actions(describe)

        def fake_run_p4(args):
            if args[:1] == ["fstat"] and "type,headType" in args:
                return (0, "... type text\n", "")
            if args == ["print", "-q", "//depot/new.py@=5"]:
                return (0, "x = 1\n", "")
            return (1, "", f"unexpected: {args}")

        with patch.object(pr, "run_p4", side_effect=fake_run_p4):
            diff = pr.extract_diff(describe, actions, cl="5", is_shelved=True)

        assert "==== //depot/new.py#1 (text) ====" in diff
        assert "+x = 1" in diff

    def test_fstat_failure_defaults_to_text_synthesis(self):
        """fstat unavailable -> behave exactly as before the guard existed."""
        describe = (
            "Shelved files ...\n"
            "... //depot/new.py#1 add\n"
            "Differences ...\n"
            "\n"
        )
        actions = pr.parse_file_actions(describe)

        def fake_run_p4(args):
            if args == ["print", "-q", "//depot/new.py@=5"]:
                return (0, "y = 2\n", "")
            return (1, "", f"unexpected: {args}")

        with patch.object(pr, "run_p4", side_effect=fake_run_p4):
            diff = pr.extract_diff(describe, actions, cl="5", is_shelved=True)

        assert "==== //depot/new.py#1 (text) ====" in diff
        assert "+y = 2" in diff

    def test_stderr_notes_skipped_binaries(self, capsys):
        describe = (
            "Shelved files ...\n"
            "... //depot/x.uasset#1 add\n"
            "Differences ...\n"
            "\n"
            "==== //depot/x.uasset#1 (binary) ====\n"
            "\n"
        )
        actions = pr.parse_file_actions(describe)
        with patch.object(pr, "run_p4", return_value=(1, "", "")):
            pr.extract_diff(describe, actions, cl="7", is_shelved=True)
        err = capsys.readouterr().err
        assert "skipped binary content" in err
        assert "//depot/x.uasset" in err

    def test_binary_edit_with_real_hunks_untouched(self):
        """The guard only applies to add/delete synthesis; sections that
        already carry @@ hunks pass through regardless of type."""
        describe = (
            "Affected files ...\n"
            "... //depot/a.bin#2 edit\n"
            "Differences ...\n"
            "\n"
            "==== //depot/a.bin#2 (binary) ====\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        )
        actions = pr.parse_file_actions(describe)
        with patch.object(pr, "run_p4", return_value=(1, "", "")):
            diff = pr.extract_diff(describe, actions, cl="1", is_shelved=False)
        assert "+new" in diff
        assert "binary file" not in diff


class TestFetchFiletypeAndSize:
    def test_fetch_filetype_prefers_type_over_headtype(self):
        out = "... headType text\n... type binary\n"
        with patch.object(pr, "run_p4", return_value=(0, out, "")) as mock:
            ft = pr.fetch_filetype("//d/x", "1", "9", is_shelved=True, is_delete=False)
        assert ft == "binary"
        assert mock.call_args[0][0] == ["fstat", "-T", "type,headType", "//d/x@=9"]

    def test_fetch_filetype_falls_back_to_headtype(self):
        out = "... headType binary+l\n"
        with patch.object(pr, "run_p4", return_value=(0, out, "")):
            ft = pr.fetch_filetype("//d/x", "3", "9", is_shelved=False, is_delete=False)
        assert ft == "binary+l"

    def test_fetch_filetype_returns_none_on_failure(self):
        with patch.object(pr, "run_p4", return_value=(1, "", "err")):
            assert pr.fetch_filetype("//d/x", "1", "9", True, False) is None

    def test_fetch_file_size_parses_filesize(self):
        with patch.object(pr, "run_p4", return_value=(0, "... fileSize 777\n", "")) as mock:
            size = pr.fetch_file_size("//d/x", "2", "9", is_shelved=False, is_delete=False)
        assert size == 777
        assert mock.call_args[0][0] == ["fstat", "-Ol", "-T", "fileSize", "//d/x#2"]

    def test_fetch_file_size_none_when_missing(self):
        with patch.object(pr, "run_p4", return_value=(0, "... depotFile //d/x\n", "")):
            assert pr.fetch_file_size("//d/x", "2", "9", False, False) is None

    def test_delete_at_rev_1_has_no_spec(self):
        with patch.object(pr, "run_p4") as mock:
            assert pr.fetch_filetype("//d/x", "1", "9", False, True) is None
            assert pr.fetch_file_size("//d/x", "1", "9", False, True) is None
        assert mock.call_count == 0


# ---------------------------------------------------------------------------
# --claim: arg parsing, pre-image materialization, build_bundle exclusion
# ---------------------------------------------------------------------------


class TestParseArgs:
    def test_cl_only(self):
        assert pr._parse_args(["12345"]) == (["12345"], [], False)

    def test_claim_flags_collected(self):
        pos, claims, _ = pr._parse_args(
            ["12345", "--claim", "**/CLAUDE.md", "--claim", "**/SKILL.md"]
        )
        assert pos == ["12345"]
        assert claims == ["**/CLAUDE.md", "**/SKILL.md"]

    def test_claim_equals_form(self):
        assert pr._parse_args(["12345", "--claim=**/SKILL.md"]) == (
            ["12345"],
            ["**/SKILL.md"],
            False,
        )

    def test_review_machine_emitted_flag(self):
        assert pr._parse_args(["12345", "--review-machine-emitted"]) == (["12345"], [], True)

    def test_claim_without_value_raises(self):
        with pytest.raises(ValueError):
            pr._parse_args(["12345", "--claim"])


class TestMaterializePreimageP4:
    def test_add_action_yields_none_without_p4_call(self, tmp_path):
        with patch.object(pr, "run_p4") as mock:
            assert pr.materialize_preimage("//depot/x/CLAUDE.md", "add", tmp_path) is None
        assert mock.call_count == 0

    def test_edit_prints_have_revision(self, tmp_path):
        captured = {}

        def fake_run_p4(args):
            captured["args"] = args
            # p4 print -q -o <dest> <spec>: write the file it was told to.
            Path(args[3]).parent.mkdir(parents=True, exist_ok=True)
            Path(args[3]).write_text("have content\n", encoding="utf-8")
            return (0, "", "")

        with patch.object(pr, "run_p4", side_effect=fake_run_p4):
            dest = pr.materialize_preimage("//depot/x/CLAUDE.md", "edit", tmp_path)

        assert dest is not None
        assert captured["args"][:3] == ["print", "-q", "-o"]
        assert captured["args"][4] == "//depot/x/CLAUDE.md#have"
        assert Path(dest).read_text(encoding="utf-8") == "have content\n"

    def test_print_failure_yields_none(self, tmp_path):
        with patch.object(pr, "run_p4", return_value=(1, "", "no such file")):
            assert pr.materialize_preimage("//depot/x/CLAUDE.md", "edit", tmp_path) is None


class TestBuildBundleClaims:
    def _describe(self):
        return (
            "Change 999 by user@client on 2026/01/01 12:00:00 *pending*\n"
            "\n"
            "\tEdit rules and code\n"
            "\n"
            "Affected files ...\n"
            "... //depot/src/CLAUDE.md#3 edit\n"
            "... //depot/src/foo.cpp#1 edit\n"
            "\n"
            "Differences ...\n"
            "\n"
            "==== //depot/src/CLAUDE.md#3 (text) ====\n"
            "@@ -1 +1 @@\n"
            "-old rule\n"
            "+new rule\n"
            "\n"
            "==== //depot/src/foo.cpp#1 (text) ====\n"
            "@@ -1 +1 @@\n"
            "-int x = 0;\n"
            "+int x = 1;\n"
        )

    def test_claimed_claude_md_excluded_and_preimage_materialized(self, tmp_path, monkeypatch):
        from bootstrap_lib.code_review import mechanical_config

        monkeypatch.setattr(mechanical_config, "_home_path", lambda _: tmp_path / "home")
        ws = tmp_path / "ws"
        src = ws / "src"
        src.mkdir(parents=True)
        (ws / "CLAUDE.md").write_text("workspace rule\n", encoding="utf-8")
        claude = src / "CLAUDE.md"
        claude.write_text("new rule\n", encoding="utf-8")
        foo = src / "foo.cpp"
        foo.write_text("int x = 1;\n", encoding="utf-8")

        where_out = (
            "... depotFile //depot/src/CLAUDE.md\n"
            f"... path {claude}\n"
            "\n"
            "... depotFile //depot/src/foo.cpp\n"
            f"... path {foo}\n"
        )
        info_out = f"... clientRoot {ws}\n"

        def fake_run_p4(args):
            if args[:2] == ["describe", "-du"]:
                return (0, self._describe(), "")
            if args[:2] == ["-ztag", "where"]:
                return (0, where_out, "")
            if args[:2] == ["-ztag", "info"]:
                return (0, info_out, "")
            if args[:3] == ["-ztag", "reconcile", "-n"]:
                return (1, "", "no file(s) to reconcile.\n")
            if args[:4] == ["-ztag", "opened", "-c", "default"]:
                return (0, "", "")
            if args[:3] == ["-ztag", "opened", "-c"]:
                return (0, "", "")
            if args[:4] == ["-ztag", "resolve", "-n", "-c"]:
                return (1, "", "no file(s) to resolve.\n")
            if args[:2] == ["-ztag", "fstat"]:
                return (1, "", "no such file(s)")
            if args[:3] == ["print", "-q", "-o"]:
                Path(args[3]).parent.mkdir(parents=True, exist_ok=True)
                Path(args[3]).write_text("old rule\n", encoding="utf-8")
                return (0, "", "")
            raise AssertionError(f"unexpected p4 command: {args}")

        with patch.object(pr, "run_p4", side_effect=fake_run_p4):
            bundle = pr.build_bundle(
                "999", tmp_path / "bundle", claim_globs=["**/CLAUDE.md", "**/SKILL.md"]
            )

        # foo.cpp reviewed generically; CLAUDE.md claimed.
        assert [f["depot"] for f in bundle["changed_files"]] == ["//depot/src/foo.cpp"]
        assert len(bundle["claimed_files"]) == 1
        claimed = bundle["claimed_files"][0]
        assert claimed["depot"] == "//depot/src/CLAUDE.md"
        assert claimed["action"] == "edit"
        assert Path(claimed["pre_image"]).read_text(encoding="utf-8") == "old rule\n"
        assert claimed["claude_mds"]  # nearest-first, includes self
        scan = claimed["mechanical_scan"]
        assert scan["schema_version"] == 2
        assert [record["file"] for record in scan["files"]] == [
            "//depot/src/CLAUDE.md"
        ]
        assert scan["files"][0]["checks_run"] == []
        # Claimed file's diff excluded from chunks; generic file present.
        diff = _concat_diff_from_chunks(bundle)
        assert "//depot/src/foo.cpp" in diff
        assert "//depot/src/CLAUDE.md" not in diff
        # Claimed file still contributes to the ruleset collection.
        assert any("CLAUDE.md" in c for c in bundle["unique_claude_mds"])

    def test_no_claim_flag_has_no_claimed_files_key(self, tmp_path):
        ws = tmp_path / "ws"
        src = ws / "src"
        src.mkdir(parents=True)
        claude = src / "CLAUDE.md"
        claude.write_text("new rule\n", encoding="utf-8")
        foo = src / "foo.cpp"
        foo.write_text("int x = 1;\n", encoding="utf-8")
        where_out = (
            "... depotFile //depot/src/CLAUDE.md\n"
            f"... path {claude}\n"
            "\n"
            "... depotFile //depot/src/foo.cpp\n"
            f"... path {foo}\n"
        )
        info_out = f"... clientRoot {ws}\n"

        def fake_run_p4(args):
            if args[:2] == ["describe", "-du"]:
                return (0, self._describe(), "")
            if args[:2] == ["-ztag", "where"]:
                return (0, where_out, "")
            if args[:2] == ["-ztag", "info"]:
                return (0, info_out, "")
            if args[:3] == ["-ztag", "reconcile", "-n"]:
                return (1, "", "no file(s) to reconcile.\n")
            if args[:4] == ["-ztag", "opened", "-c", "default"]:
                return (0, "", "")
            if args[:3] == ["-ztag", "opened", "-c"]:
                return (0, "", "")
            if args[:4] == ["-ztag", "resolve", "-n", "-c"]:
                return (1, "", "no file(s) to resolve.\n")
            if args[:2] == ["-ztag", "fstat"]:
                return (1, "", "no such file(s)")
            if args[:3] == ["print", "-q", "-o"]:
                Path(args[3]).parent.mkdir(parents=True, exist_ok=True)
                Path(args[3]).write_text("old content\n", encoding="utf-8")
                return (0, "", "")
            raise AssertionError(f"unexpected p4 command: {args}")

        with patch.object(pr, "run_p4", side_effect=fake_run_p4):
            bundle = pr.build_bundle("999", tmp_path / "bundle")

        assert "claimed_files" not in bundle
        assert len(bundle["changed_files"]) == 2


# ---------------------------------------------------------------------------
# build_bundle -- hygiene scan seeded from pre-routing files
# ---------------------------------------------------------------------------


class TestBuildBundleHygieneSources:
    def test_fully_claimed_cl_still_runs_the_reconcile_scan(self, tmp_path):
        """A CL whose ONLY file is claimed must not skip the forgotten-files
        gate. minimal_dirs derived from post-routing changed_files (which
        routing has already stripped the claimed file from) would be empty,
        so find_unreconciled would never run and a genuinely unreconciled
        sibling on disk would go unreported."""
        ws = tmp_path / "ws"
        src = ws / "src"
        src.mkdir(parents=True)
        claude = src / "CLAUDE.md"
        claude.write_text("new rule\n", encoding="utf-8")
        # A sibling file that exists on disk but isn't in the CL.
        forgotten = src / "forgot.cpp"
        forgotten.write_text("int y = 2;\n", encoding="utf-8")

        describe_out = (
            "Change 999 by user@client on 2026/01/01 12:00:00 *pending*\n"
            "\n"
            "\tEdit rules\n"
            "\n"
            "Affected files ...\n"
            "... //depot/src/CLAUDE.md#3 edit\n"
            "\n"
            "Differences ...\n"
            "\n"
            "==== //depot/src/CLAUDE.md#3 (text) ====\n"
            "@@ -1 +1 @@\n"
            "-old rule\n"
            "+new rule\n"
        )
        where_out = f"... depotFile //depot/src/CLAUDE.md\n... path {claude}\n"
        info_out = f"... clientRoot {ws}\n"
        reconcile_out = (
            "... depotFile //depot/src/forgot.cpp\n"
            f"... clientFile {forgotten}\n"
            "... rev 1\n"
            "... action add\n"
            "... type text\n"
        )
        reconcile_calls = []

        def fake_run_p4(args):
            if args[:2] == ["describe", "-du"]:
                return (0, describe_out, "")
            if args[:2] == ["-ztag", "where"]:
                return (0, where_out, "")
            if args[:2] == ["-ztag", "info"]:
                return (0, info_out, "")
            if args[:3] == ["-ztag", "reconcile", "-n"]:
                reconcile_calls.append(args)
                return (0, reconcile_out, "")
            if args[:4] == ["-ztag", "opened", "-c", "default"]:
                return (0, "", "")
            if args[:3] == ["-ztag", "opened", "-c"]:
                return (0, "", "")
            if args[:4] == ["-ztag", "resolve", "-n", "-c"]:
                return (1, "", "no file(s) to resolve.\n")
            if args[:2] == ["-ztag", "fstat"]:
                return (1, "", "no such file(s)")
            if args[:3] == ["print", "-q", "-o"]:
                Path(args[3]).parent.mkdir(parents=True, exist_ok=True)
                Path(args[3]).write_text("old rule\n", encoding="utf-8")
                return (0, "", "")
            raise AssertionError(f"unexpected p4 command: {args}")

        with patch.object(pr, "run_p4", side_effect=fake_run_p4):
            bundle = pr.build_bundle(
                "999", tmp_path / "bundle", claim_globs=["**/CLAUDE.md"]
            )

        assert bundle["changed_files"] == []
        assert len(reconcile_calls) == 1
        assert str(src) in reconcile_calls[0][-1]
        assert len(bundle["unreconciled"]) == 1
        assert bundle["unreconciled"][0]["depot"] == "//depot/src/forgot.cpp"

    def test_own_depot_files_excluded_from_unreconciled(self, tmp_path):
        """p4 reconcile can report a file already open in the CL (e.g. opened
        for edit then deleted locally, or opened for delete then recreated).
        Such a hit isn't something the user forgot -- it's already part of
        the CL -- so it must not appear in `unreconciled` alongside a
        genuinely missing sibling."""
        ws = tmp_path / "ws"
        src = ws / "src"
        src.mkdir(parents=True)
        own_file = src / "foo.cpp"
        own_file.write_text("int x = 1;\n", encoding="utf-8")
        sibling = src / "forgot.cpp"
        sibling.write_text("int y = 2;\n", encoding="utf-8")

        describe_out = (
            "Change 1000 by user@client on 2026/01/01\n"
            "\n"
            "\tEdit foo\n"
            "\n"
            "Affected files ...\n"
            "... //depot/src/foo.cpp#1 edit\n"
            "\n"
            "Differences ...\n"
            "\n"
            "==== //depot/src/foo.cpp#1 (text) ====\n"
            "@@ -1 +1 @@\n"
            "-int x = 0;\n"
            "+int x = 1;\n"
        )
        where_out = f"... depotFile //depot/src/foo.cpp\n... path {own_file}\n"
        info_out = f"... clientRoot {ws}\n"
        # Reconcile reports BOTH the CL's own file (a spurious re-detection)
        # and a genuine sibling that was never included in the CL.
        reconcile_out = (
            "... depotFile //depot/src/foo.cpp\n"
            f"... clientFile {own_file}\n"
            "... rev 1\n"
            "... action edit\n"
            "... type text\n"
            "\n"
            "... depotFile //depot/src/forgot.cpp\n"
            f"... clientFile {sibling}\n"
            "... rev 1\n"
            "... action add\n"
            "... type text\n"
        )

        def fake_run_p4(args):
            if args[:2] == ["describe", "-du"]:
                return (0, describe_out, "")
            if args[:2] == ["-ztag", "where"]:
                return (0, where_out, "")
            if args[:2] == ["-ztag", "info"]:
                return (0, info_out, "")
            if args[:3] == ["-ztag", "reconcile", "-n"]:
                return (0, reconcile_out, "")
            if args[:4] == ["-ztag", "opened", "-c", "default"]:
                return (0, "", "")
            if args[:4] == ["-ztag", "resolve", "-n", "-c"]:
                return (1, "", "no file(s) to resolve.\n")
            if args[:2] == ["-ztag", "fstat"]:
                return (1, "", "no such file(s)")
            if args[:3] == ["print", "-q", "-o"]:
                Path(args[3]).parent.mkdir(parents=True, exist_ok=True)
                Path(args[3]).write_text("int x = 0;\n", encoding="utf-8")
                return (0, "", "")
            raise AssertionError(f"unexpected p4 command: {args}")

        with patch.object(pr, "run_p4", side_effect=fake_run_p4):
            bundle = pr.build_bundle("1000", tmp_path / "bundle")

        assert [u["depot"] for u in bundle["unreconciled"]] == ["//depot/src/forgot.cpp"]

    def test_stale_open_edit_then_missing_surfaces_as_stale_open(self, tmp_path):
        """The CL has foo.cpp open for edit; reconcile finds it missing from
        the workspace and proposes `delete`. That is not a forgotten sibling
        -- it is a CL that will fail `p4 submit` outright -- so it must
        surface under the new `stale_open` bundle key, carrying the CL's own
        open action (`edit`, from the describe output) and
        `workspace_state: missing`, and it must not appear in `unreconciled`."""
        ws = tmp_path / "ws"
        src = ws / "src"
        src.mkdir(parents=True)
        sibling = src / "forgot.cpp"
        sibling.write_text("int y = 2;\n", encoding="utf-8")
        # foo.cpp deliberately absent from disk -- it was deleted locally
        # while still open for edit in the CL.
        own_local = src / "foo.cpp"

        describe_out = (
            "Change 1001 by user@client on 2026/01/01\n"
            "\n"
            "\tEdit foo\n"
            "\n"
            "Affected files ...\n"
            "... //depot/src/foo.cpp#1 edit\n"
            "\n"
            "Differences ...\n"
            "\n"
            "==== //depot/src/foo.cpp#1 (text) ====\n"
            "@@ -1 +1 @@\n"
            "-int x = 0;\n"
            "+int x = 1;\n"
        )
        where_out = f"... depotFile //depot/src/foo.cpp\n... path {own_local}\n"
        info_out = f"... clientRoot {ws}\n"
        reconcile_out = (
            "... depotFile //depot/src/foo.cpp\n"
            f"... clientFile {own_local}\n"
            "... rev 1\n"
            "... action delete\n"
            "... type text\n"
            "\n"
            "... depotFile //depot/src/forgot.cpp\n"
            f"... clientFile {sibling}\n"
            "... rev 1\n"
            "... action add\n"
            "... type text\n"
        )

        def fake_run_p4(args):
            if args[:2] == ["describe", "-du"]:
                return (0, describe_out, "")
            if args[:2] == ["-ztag", "where"]:
                return (0, where_out, "")
            if args[:2] == ["-ztag", "info"]:
                return (0, info_out, "")
            if args[:3] == ["-ztag", "reconcile", "-n"]:
                return (0, reconcile_out, "")
            if args[:4] == ["-ztag", "opened", "-c", "default"]:
                return (0, "", "")
            if args[:4] == ["-ztag", "resolve", "-n", "-c"]:
                return (1, "", "no file(s) to resolve.\n")
            if args[:2] == ["-ztag", "fstat"]:
                return (1, "", "no such file(s)")
            raise AssertionError(f"unexpected p4 command: {args}")

        with patch.object(pr, "run_p4", side_effect=fake_run_p4):
            bundle = pr.build_bundle("1001", tmp_path / "bundle")

        assert [u["depot"] for u in bundle["unreconciled"]] == ["//depot/src/forgot.cpp"]
        assert bundle["stale_open"] == [
            {
                "depot": "//depot/src/foo.cpp",
                "local": str(own_local),
                "open_action": "edit",
                "workspace_state": "missing",
            }
        ]

    def test_stale_open_delete_then_present_surfaces_as_stale_open(self, tmp_path):
        """The CL has bar.cpp open for delete; reconcile finds it present on
        disk (recreated after the delete was opened) and proposes `add`. The
        submit would delete content that is on disk, so this must surface as
        `stale_open` with `workspace_state: present` and the CL's own
        `delete` open action, and must not appear in `unreconciled`."""
        ws = tmp_path / "ws"
        src = ws / "src"
        src.mkdir(parents=True)
        own_local = src / "bar.cpp"
        own_local.write_text("int z = 3;\n", encoding="utf-8")

        describe_out = (
            "Change 1002 by user@client on 2026/01/01\n"
            "\n"
            "\tDelete bar\n"
            "\n"
            "Affected files ...\n"
            "... //depot/src/bar.cpp#1 delete\n"
            "\n"
            "Differences ...\n"
            "\n"
        )
        where_out = f"... depotFile //depot/src/bar.cpp\n... path {own_local}\n"
        info_out = f"... clientRoot {ws}\n"
        reconcile_out = (
            "... depotFile //depot/src/bar.cpp\n"
            f"... clientFile {own_local}\n"
            "... rev 1\n"
            "... action add\n"
            "... type text\n"
        )

        def fake_run_p4(args):
            if args[:2] == ["describe", "-du"]:
                return (0, describe_out, "")
            if args[:2] == ["-ztag", "where"]:
                return (0, where_out, "")
            if args[:2] == ["-ztag", "info"]:
                return (0, info_out, "")
            if args[:3] == ["-ztag", "reconcile", "-n"]:
                return (0, reconcile_out, "")
            if args[:4] == ["-ztag", "opened", "-c", "default"]:
                return (0, "", "")
            if args[:4] == ["-ztag", "resolve", "-n", "-c"]:
                return (1, "", "no file(s) to resolve.\n")
            if args[:2] == ["-ztag", "fstat"]:
                return (1, "", "no such file(s)")
            if args[:3] == ["print", "-q", "-o"]:
                Path(args[3]).parent.mkdir(parents=True, exist_ok=True)
                Path(args[3]).write_text("int z = 3;\n", encoding="utf-8")
                return (0, "", "")
            raise AssertionError(f"unexpected p4 command: {args}")

        with patch.object(pr, "run_p4", side_effect=fake_run_p4):
            bundle = pr.build_bundle("1002", tmp_path / "bundle")

        assert bundle["unreconciled"] == []
        assert bundle["stale_open"] == [
            {
                "depot": "//depot/src/bar.cpp",
                "local": str(own_local),
                "open_action": "delete",
                "workspace_state": "present",
            }
        ]


class TestDefaultOpenBundle:
    def test_keeps_default_open_separate_and_uses_reconcile_scope(self, tmp_path):
        ws = tmp_path / "ws"
        src = ws / "src"
        src.mkdir(parents=True)
        local = src / "foo.cpp"
        local.write_text("int x = 1;\n", encoding="utf-8")
        forgotten = src / "forgot.cpp"
        forgotten.write_text("int y = 2;\n", encoding="utf-8")
        default_file = src / "extra.cpp"
        default_file.write_text("int z = 3;\n", encoding="utf-8")
        describe = (
            "Change 812 by author@author-client on 2026/01/01 12:00:00\n"
            "\n"
            "\tEdit foo\n"
            "\n"
            "Affected files ...\n"
            "... //depot/src/foo.cpp#1 edit\n"
            "\n"
            "Differences ...\n"
            "\n"
            "==== //depot/src/foo.cpp#1 (text) ====\n"
            "@@ -1 +1 @@\n"
            "-int x = 0;\n"
            "+int x = 1;\n"
        )
        where_output = (
            "... depotFile //depot/src/foo.cpp\n"
            f"... path {local}\n"
        )
        reconcile_output = (
            "... depotFile //depot/src/forgot.cpp\n"
            f"... clientFile {forgotten}\n"
            "... action add\n"
        )
        default_output = (
            "... depotFile //depot/src/extra.cpp\n"
            f"... clientFile {default_file}\n"
            "... action edit\n"
            "... change default\n"
        )
        reconcile_commands = []
        default_commands = []

        def fake_run_p4(args):
            if args == ["describe", "-du", "812"]:
                return 0, describe, ""
            if args == ["-ztag", "info"]:
                return 0, f"... clientRoot {ws}\n", ""
            if args == ["-ztag", "where", "//depot/src/foo.cpp"]:
                return 0, where_output, ""
            if args[:3] == ["-ztag", "reconcile", "-n"]:
                reconcile_commands.append(args)
                return 0, reconcile_output, ""
            if args[:4] == ["-ztag", "opened", "-c", "default"]:
                default_commands.append(args)
                return 0, default_output, ""
            if args == ["-ztag", "resolve", "-n", "-c", "812"]:
                return 1, "", "no file(s) to resolve.\n"
            if args == ["-ztag", "fstat", "-Ol", "//...@=812"]:
                return 1, "", "no such file(s)"
            raise AssertionError(f"unexpected p4 command: {args}")

        with patch.object(pr, "run_p4", side_effect=fake_run_p4):
            bundle = pr.build_bundle("812", tmp_path / "bundle")

        assert {
            "unreconciled": bundle["unreconciled"],
            "default_open": bundle["default_open"],
        } == {
            "unreconciled": [
                {
                    "local": str(forgotten),
                    "depot": "//depot/src/forgot.cpp",
                    "action": "add",
                }
            ],
            "default_open": [
                {
                    "local": str(default_file),
                    "depot": "//depot/src/extra.cpp",
                    "action": "edit",
                }
            ],
        }
        assert default_commands[0][4:] == reconcile_commands[0][3:]


# ---------------------------------------------------------------------------
# Submitted-CL guard: --claim requires a pending CL (#have would be POST-change)
# ---------------------------------------------------------------------------


class TestSubmittedClaimGuard:
    def _submitted_describe(self):
        # NO *pending* marker -> a submitted CL.
        return (
            "Change 555 by user@client on 2026/01/01 12:00:00\n"
            "\n"
            "\tEdit rules\n"
            "\n"
            "Affected files ...\n"
            "... //depot/src/CLAUDE.md#4 edit\n"
            "\n"
            "Differences ...\n"
            "\n"
            "==== //depot/src/CLAUDE.md#4 (text) ====\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        )

    def _fake(self, ws, claude):
        where_out = (
            "... depotFile //depot/src/CLAUDE.md\n"
            f"... path {claude}\n"
        )
        info_out = f"... clientRoot {ws}\n"

        def fake_run_p4(args):
            if args[:2] == ["describe", "-du"]:
                return (0, self._submitted_describe(), "")
            if args[:2] == ["-ztag", "where"]:
                return (0, where_out, "")
            if args[:2] == ["-ztag", "info"]:
                return (0, info_out, "")
            if args[:3] == ["-ztag", "reconcile", "-n"]:
                return (1, "", "no file(s) to reconcile.\n")
            if args[:4] == ["-ztag", "opened", "-c", "default"]:
                return (0, "", "")
            if args[:4] == ["-ztag", "resolve", "-n", "-c"]:
                return (1, "", "no file(s) to resolve.\n")
            if args[:2] == ["-ztag", "fstat"]:
                return (1, "", "no such file(s)")
            raise AssertionError(f"unexpected p4 command: {args}")

        return fake_run_p4

    def test_submitted_cl_with_claim_raises(self, tmp_path):
        ws = tmp_path / "ws"
        src = ws / "src"
        src.mkdir(parents=True)
        claude = src / "CLAUDE.md"
        claude.write_text("new\n", encoding="utf-8")
        with patch.object(pr, "run_p4", side_effect=self._fake(ws, claude)):
            with pytest.raises(ValueError, match="submitted"):
                pr.build_bundle(
                    "555", tmp_path / "bundle", claim_globs=["**/CLAUDE.md"]
                )

    def test_submitted_cl_without_claim_ok(self, tmp_path):
        # Without --claim, a submitted CL is still reviewable (informational).
        ws = tmp_path / "ws"
        src = ws / "src"
        src.mkdir(parents=True)
        claude = src / "CLAUDE.md"
        claude.write_text("new\n", encoding="utf-8")
        with patch.object(pr, "run_p4", side_effect=self._fake(ws, claude)):
            bundle = pr.build_bundle("555", tmp_path / "bundle")
        assert bundle["cl"] == "555"
        assert "claimed_files" not in bundle


class TestForeignChangeClient:
    @staticmethod
    def _pending_describe():
        return (
            "Change 701 by author@author-client on 2026/01/01 12:00:00 *pending*\n"
            "\n"
            "\tEdit foo\n"
            "\n"
            "Affected files ...\n"
            "... //depot/src/foo.cpp#1 edit\n"
            "\n"
            "Differences ...\n"
            "\n"
            "==== //depot/src/foo.cpp#1 (text) ====\n"
            "@@ -1 +1 @@\n"
            "-int x = 0;\n"
            "+int x = 1;\n"
        )

    def test_parses_change_owner_from_header(self):
        assert pr._parse_change_owner(self._pending_describe()) == {
            "user": "author",
            "client": "author-client",
        }

    def test_workspace_identity_uses_one_info_call(self, tmp_path):
        calls = []

        def fake_run_p4(args):
            if args != ["-ztag", "info"]:
                raise AssertionError(f"unexpected p4 command: {args}")
            calls.append(args)
            return (
                0,
                f"... clientName reviewer-client\n... clientRoot {tmp_path}\n",
                "",
            )

        with patch.object(pr, "run_p4", side_effect=fake_run_p4):
            identity = pr.get_workspace_root()

        assert (identity, calls) == (
            (tmp_path, "reviewer-client"),
            [["-ztag", "info"]],
        )

    def test_foreign_change_skips_local_scans_and_keeps_reviewer_instructions(
        self, tmp_path
    ):
        ws = tmp_path / "reviewer-ws"
        src = ws / "src"
        src.mkdir(parents=True)
        local = src / "foo.cpp"
        local.write_text("// GENERATED BY reviewer state\n", encoding="utf-8")
        claude = ws / "CLAUDE.md"
        claude.write_text("reviewer rule\n", encoding="utf-8")
        shelf = pr.ShelfScanResult(
            digests={"//depot/src/foo.cpp": "ABC"},
            actions={"//depot/src/foo.cpp": "edit"},
        )
        owner = {"user": "author", "client": "author-client"}

        with patch.object(pr, "fetch_describe", return_value=(self._pending_describe(), True)), \
                patch.object(pr, "fetch_shelf_fingerprint", return_value=shelf), \
                patch.object(pr, "fetch_opened_files", side_effect=AssertionError("opened scan ran")) as opened_scan, \
                patch.object(pr, "resolve_local_paths", return_value={"//depot/src/foo.cpp": str(local)}), \
                patch.object(pr, "get_workspace_root", return_value=(ws, "reviewer-client")), \
                patch.object(pr, "_shelf_content_drift", side_effect=AssertionError("shelf drift ran")) as drift_scan, \
                patch.object(pr, "find_unreconciled", side_effect=AssertionError("reconcile ran")) as reconcile_scan, \
                patch.object(pr, "find_default_open", side_effect=AssertionError("default opened ran")) as default_scan, \
                patch.object(pr, "find_unresolved", side_effect=AssertionError("resolve ran")) as resolve_scan:
            bundle = pr.build_bundle("701", tmp_path / "bundle")

        assert [
            opened_scan.call_count,
            drift_scan.call_count,
            reconcile_scan.call_count,
            default_scan.call_count,
            resolve_scan.call_count,
        ] == [0, 0, 0, 0, 0]
        assert bundle["hygiene_incomplete"] == [
            {"scan": "unreconciled", "reason": "skipped for foreign client author-client"},
            {"scan": "default_open", "reason": "skipped for foreign client author-client"},
            {"scan": "unresolved", "reason": "skipped for foreign client author-client"},
            {"scan": "shelf_opened", "reason": "skipped for foreign client author-client"},
            {"scan": "shelf_drift", "reason": "skipped for foreign client author-client"},
            {"scan": "machine_emitted", "reason": "skipped for foreign client author-client"},
        ]
        assert bundle["foreign_change"] == owner
        assert bundle["unique_claude_mds"] == [str(claude)]
        assert "machine_emitted_files" not in bundle

    def test_foreign_change_with_claim_is_refused(self, tmp_path):
        with patch.object(pr, "fetch_describe", return_value=(self._pending_describe(), False)), \
                patch.object(pr, "get_workspace_root", return_value=(tmp_path, "reviewer-client")), \
                patch.object(pr, "resolve_local_paths", return_value={"//depot/src/foo.cpp": None}), \
                patch.object(pr, "materialize_preimage", side_effect=AssertionError("pre-image read")):
            with pytest.raises(
                ValueError,
                match=r"foreign client author-client.*re-run without --claim",
            ):
                pr.build_bundle(
                    "701", tmp_path / "bundle", claim_globs=["**/*.cpp"]
                )

    @pytest.mark.parametrize(
        "owner, client_name",
        [
            ({"user": "author", "client": "reviewer-client"}, "reviewer-client"),
            (None, "reviewer-client"),
            ({"user": "author", "client": "author-client"}, None),
        ],
        ids=["matching-client", "owner-undetermined", "client-undetermined"],
    )
    def test_matching_or_undetermined_client_runs_local_scans(
        self, owner, client_name, tmp_path
    ):
        local = tmp_path / "foo.cpp"
        local.write_text("int x = 1;\n", encoding="utf-8")
        shelf = pr.ShelfScanResult(
            digests={"//depot/src/foo.cpp": "ABC"},
            actions={"//depot/src/foo.cpp": "edit"},
        )
        with patch.object(pr, "fetch_describe", return_value=(self._pending_describe(), True)), \
                patch.object(pr, "_parse_change_owner", return_value=owner), \
                patch.object(pr, "fetch_shelf_fingerprint", return_value=shelf), \
                patch.object(pr, "fetch_opened_files", return_value=({"//depot/src/foo.cpp": "edit"}, [])), \
                patch.object(pr, "resolve_local_paths", return_value={"//depot/src/foo.cpp": str(local)}), \
                patch.object(pr, "get_workspace_root", return_value=(tmp_path, client_name)), \
                patch.object(pr, "_shelf_content_drift", return_value=([], [])) as drift_scan, \
                patch.object(pr, "find_unreconciled", return_value=([], [])) as reconcile_scan, \
                patch.object(pr, "find_default_open", return_value=([], [])) as default_scan, \
                patch.object(pr, "find_unresolved", return_value=([], [])) as resolve_scan:
            bundle = pr.build_bundle("701", tmp_path / "bundle")

        assert [
            drift_scan.call_count,
            reconcile_scan.call_count,
            default_scan.call_count,
            resolve_scan.call_count,
        ] == [1, 1, 1, 1]
        assert "foreign_change" not in bundle


# ---------------------------------------------------------------------------
# Ledger wiring in the bundle
# ---------------------------------------------------------------------------


class TestBundleLedgerWiring:
    def _fake(self, ws, foo):
        describe = (
            "Change 999 by user@client on 2026/01/01 12:00:00 *pending*\n"
            "\n\tEdit foo\n\n"
            "Affected files ...\n"
            "... //depot/src/foo.cpp#1 edit\n"
            "\n"
            "Differences ...\n"
            "\n"
            "==== //depot/src/foo.cpp#1 (text) ====\n"
            "@@ -1 +1 @@\n-int x = 0;\n+int x = 1;\n"
        )
        where_out = (
            "... depotFile //depot/src/foo.cpp\n"
            f"... path {foo}\n"
        )
        info_out = f"... clientRoot {ws}\n"

        def fake_run_p4(args):
            if args[:2] == ["describe", "-du"]:
                return (0, describe, "")
            if args[:2] == ["-ztag", "where"]:
                return (0, where_out, "")
            if args[:2] == ["-ztag", "info"]:
                return (0, info_out, "")
            if args[:3] == ["-ztag", "reconcile", "-n"]:
                return (1, "", "no file(s) to reconcile.\n")
            if args[:4] == ["-ztag", "opened", "-c", "default"]:
                return (0, "", "")
            if args[:3] == ["-ztag", "opened", "-c"]:
                return (0, "", "")
            if args[:4] == ["-ztag", "resolve", "-n", "-c"]:
                return (1, "", "no file(s) to resolve.\n")
            if args[:2] == ["-ztag", "fstat"]:
                return (1, "", "no such file(s)")
            if args[:3] == ["print", "-q", "-o"]:
                Path(args[3]).parent.mkdir(parents=True, exist_ok=True)
                Path(args[3]).write_text("int x = 0;\n", encoding="utf-8")
                return (0, "", "")
            raise AssertionError(f"unexpected p4 command: {args}")

        return fake_run_p4

    def test_bundle_carries_ledger_fields(self, tmp_path):
        ws = tmp_path / "ws"
        src = ws / "src"
        src.mkdir(parents=True)
        foo = src / "foo.cpp"
        foo.write_text("int x = 1;\n", encoding="utf-8")
        led = tmp_path / "ledger.json"
        with patch.object(pr, "run_p4", side_effect=self._fake(ws, foo)):
            bundle = pr.build_bundle("999", tmp_path / "bundle", ledger_path=led)
        assert bundle["change_id"] == "999"
        assert isinstance(bundle["ledger_baseline"], str) and bundle["ledger_baseline"]
        assert bundle["ledger_hits"] == []

    def test_recorded_finding_flows_back_as_hit(self, tmp_path):
        ws = tmp_path / "ws"
        src = ws / "src"
        src.mkdir(parents=True)
        foo = src / "foo.cpp"
        foo.write_text("int x = 1;\n", encoding="utf-8")
        led = tmp_path / "ledger.json"
        with patch.object(pr, "run_p4", side_effect=self._fake(ws, foo)):
            first = pr.build_bundle("999", tmp_path / "b1", ledger_path=led)
        # Record a declined finding at the baseline the first run computed.
        pr.ledger.record_declined(
            led, first["change_id"], first["ledger_baseline"],
            [{"kind": "code_review", "file": "src/foo.cpp", "reason": "bug",
              "description": "off by one in loop"}],
        )
        with patch.object(pr, "run_p4", side_effect=self._fake(ws, foo)):
            second = pr.build_bundle("999", tmp_path / "b2", ledger_path=led)
        assert len(second["ledger_hits"]) == 1
        assert second["ledger_hits"][0]["label"] == "off by one in loop"


# ---------------------------------------------------------------------------
# bootstrap_guard.data_dir redirect -- bundle root and ledger path
# ---------------------------------------------------------------------------


class TestDataRootRedirect:
    """bootstrap_guard.data_dir is the venv resolution path used by
    reexec_under_plugin_venv, so hand-building DEFAULT_BUNDLE_ROOT /
    LEDGER_PATH from Path.home() ignores the same redirect a test session
    relies on -- see tests/bootstrap/test_plugin_test_session.py::TestDataRootRedirect
    and git-kit's mirror of this class."""

    @staticmethod
    def _reload_prepare_review():
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "p4_kit_prepare_review_reload_test", pr.__file__
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_redirect_moves_the_bundle_root(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CLAUDE_BOOTSTRAP_DATA_ROOT", str(tmp_path))
        mod = self._reload_prepare_review()
        assert mod.DEFAULT_BUNDLE_ROOT == tmp_path / "plugins-kit" / "p4-kit" / "reviews"

    def test_redirect_moves_the_ledger(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CLAUDE_BOOTSTRAP_DATA_ROOT", str(tmp_path))
        mod = self._reload_prepare_review()
        assert mod._ledger_path() == (
            tmp_path / "plugins-kit" / "p4-kit" / "reviews" / "ledger.json"
        )

    def test_ledger_path_is_lazy_after_import(self, monkeypatch, tmp_path):
        """An env change AFTER import must still be honoured -- a module-level
        constant computed once at import time would not see it."""
        monkeypatch.delenv("CLAUDE_BOOTSTRAP_DATA_ROOT", raising=False)
        mod = self._reload_prepare_review()
        before = mod._ledger_path()

        monkeypatch.setenv("CLAUDE_BOOTSTRAP_DATA_ROOT", str(tmp_path))
        after = mod._ledger_path()

        assert after == tmp_path / "plugins-kit" / "p4-kit" / "reviews" / "ledger.json"
        assert after != before
# Seam B pinned P4 primitives
def test_materialize_preimage_uses_describe_base_revision(tmp_path):
    with patch.object(pr, "run_p4", return_value=(0, "", "")) as run:
        result = pr.materialize_preimage("//depot/a.md", "edit", tmp_path, "7")

    assert result is not None
    assert run.call_args.args[0][-1] == "//depot/a.md#7"


def test_p4_snapshot_reader_batches_at_one_hundred_paths(tmp_path):
    reader = pr.P4SnapshotReader(tmp_path)
    paths = tuple(f"docs/{index}.md" for index in range(201))
    assert [len(batch) for batch in reader._batches(paths)] == [100, 100, 1]


def test_p4_snapshot_reader_escapes_raw_and_preserves_encoded_depot_paths(tmp_path):
    reader = pr.P4SnapshotReader(tmp_path)
    assert reader._escape_filespec("a%40b#c@d*") == "a%2540b%23c%40d%2A"
    assert reader._revision_spec("//depot/a%40b#c", "7") == (
        "//depot/a%40b%23c#7"
    )


def test_have_error_preserves_complete_file_and_fails_unresolved_without_probe(tmp_path):
    reader = pr.P4SnapshotReader(tmp_path)
    a_local = str(tmp_path / "a.md")
    b_local = str(tmp_path / "b.md")

    def fake(args):
        if args[:2] == ["-ztag", "where"]:
            return 0, (
                f"... depotFile //depot/a.md\n... path {a_local}\n\n"
                f"... depotFile //depot/b.md\n... path {b_local}\n"
            ), ""
        if args[:2] == ["-ztag", "have"]:
            return 1, "... depotFile //depot/a.md\n... haveRev 3\n", "server unavailable"
        if args[:2] == ["-ztag", "fstat"]:
            assert "-Rh" not in args
            return 0, "... depotFile //depot/a.md\n... fileSize 4\n", ""
        raise AssertionError(args)

    with patch.object(pr, "run_p4", side_effect=fake):
        results = reader.stat(("a.md", "b.md"))

    assert results["a.md"].kind == "file"
    assert results["b.md"].kind == "error"
    assert results["b.md"].diagnostic == "server unavailable"


def test_directory_descendant_revisions_change_snapshot_identity(tmp_path):
    def identity_for(revision):
        reader = pr.P4SnapshotReader(tmp_path)
        local = str(tmp_path / "docs")

        def fake(args):
            if args[:2] == ["-ztag", "where"]:
                return 0, f"... depotFile //depot/docs\n... path {local}\n", ""
            if args[:2] == ["-ztag", "have"]:
                return 1, "", "no such file(s)"
            if args[:2] == ["-ztag", "fstat"] and "-Rh" in args:
                return 0, (
                    "... depotFile //depot/docs/a.md\n"
                    f"... haveRev {revision}\n"
                ), ""
            raise AssertionError(args)

        with patch.object(pr, "run_p4", side_effect=fake):
            assert reader.stat(("docs",))["docs"].kind == "directory"
        return reader.finalize_identity("p4:client:seed", ())

    assert identity_for("7") != identity_for("8")


def test_directory_probe_mixed_diagnostic_is_error_not_missing(tmp_path):
    reader = pr.P4SnapshotReader(tmp_path)
    local = str(tmp_path / "missing")

    def fake(args):
        if args[:2] == ["-ztag", "where"]:
            return 0, f"... depotFile //depot/missing\n... path {local}\n", ""
        if args[:2] == ["-ztag", "have"]:
            return 1, "", "no such file(s)"
        if args[:2] == ["-ztag", "fstat"] and "-Rh" in args:
            return 1, "", "no such file(s)\nserver unavailable"
        raise AssertionError(args)

    with patch.object(pr, "run_p4", side_effect=fake):
        result = reader.stat(("missing",))["missing"]

    assert result.kind == "error"
    assert result.diagnostic == "no such file(s)\nserver unavailable"


@pytest.mark.parametrize(
    ("probe_result", "diagnostic"),
    [
        ((0, "", ""), "P4 probe failed"),
        ((1, "", "file(s) not on client"), "file(s) not on client"),
        ((1, "", "file(s) not in client view"), "file(s) not in client view"),
    ],
)
def test_directory_probe_requires_documented_no_such_diagnostic(
    tmp_path, probe_result, diagnostic
):
    reader = pr.P4SnapshotReader(tmp_path)
    local = str(tmp_path / "missing")

    def fake(args):
        if args[:2] == ["-ztag", "where"]:
            return 0, f"... depotFile //depot/missing\n... path {local}\n", ""
        if args[:2] == ["-ztag", "have"]:
            return 1, "", "no such file(s)"
        if args[:2] == ["-ztag", "fstat"] and "-Rh" in args:
            return probe_result
        raise AssertionError(args)

    with patch.object(pr, "run_p4", side_effect=fake):
        result = reader.stat(("missing",))["missing"]

    assert result.kind == "error"
    assert diagnostic in (result.diagnostic or "")


def test_directory_probe_documented_no_such_diagnostic_is_missing(tmp_path):
    reader = pr.P4SnapshotReader(tmp_path)
    local = str(tmp_path / "missing")

    def fake(args):
        if args[:2] == ["-ztag", "where"]:
            return 0, f"... depotFile //depot/missing\n... path {local}\n", ""
        if args[:2] == ["-ztag", "have"]:
            return 1, "", "no such file(s)"
        if args[:2] == ["-ztag", "fstat"] and "-Rh" in args:
            return 1, "", "//depot/missing/... - no such file(s)."
        raise AssertionError(args)

    with patch.object(pr, "run_p4", side_effect=fake):
        result = reader.stat(("missing",))["missing"]

    assert result.kind == "missing"


def test_pending_shelf_race_retries_complete_capture_once(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    local = workspace / "guide.md"
    local.write_text("new\n", encoding="utf-8")
    preimage = tmp_path / "pre.md"
    preimage.write_text("old\n", encoding="utf-8")
    depot = "//depot/guide.md"
    describe = (
        "Change 9 by user@client on 2026/01/01 *pending*\n\n"
        "\tEdit guide\n\nShelved files ...\n"
        f"... {depot}#3 edit\n\nDifferences ...\n\n"
        f"==== {depot}#3 (text) ====\n"
        "@@ -1 +1 @@\n-old\n+new\n"
    )
    digest = hashlib.md5(b"new\n").hexdigest().upper()
    stable = pr.ShelfScanResult({depot: digest}, {depot: "edit"})
    changed = pr.ShelfScanResult({depot: "DIFFERENT"}, {depot: "edit"})

    with (
        patch.object(pr, "fetch_shelf_fingerprint", side_effect=[stable, changed, stable, stable]) as fingerprints,
        patch.object(pr, "fetch_describe", return_value=(describe, True)) as describes,
        patch.object(pr, "get_workspace_root", return_value=(workspace, "client")),
        patch.object(pr, "resolve_local_paths", return_value={depot: str(local)}),
        patch.object(pr, "fetch_opened_files", return_value=({depot: "edit"}, [])),
        patch.object(pr, "_shelf_content_drift", return_value=([], [])),
        patch.object(pr, "materialize_preimage", return_value=str(preimage)),
        patch.object(pr, "fetch_file_content", return_value="new\n"),
        patch.object(pr, "find_unreconciled", return_value=([], [])),
        patch.object(pr, "find_default_open", return_value=([], [])),
        patch.object(pr, "find_unresolved", return_value=([], [])),
    ):
        bundle = pr.build_bundle("9", tmp_path / "bundle")

    assert fingerprints.call_count == 4
    assert describes.call_count == 2
    assert bundle["snapshot_identity"].startswith("p4:client:")
    assert bundle["mechanical_contract"] == 2
    record = bundle["diff_chunks"][0]["mechanical_scan"]["files"][0]
    assert record["mechanical_contract"] == 2
    assert "local_link_targets" in record["checks_run"]
