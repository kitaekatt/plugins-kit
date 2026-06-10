"""Tests for find_project_dir's cwd encoding (X12).

Claude Code encodes the project cwd into a ~/.claude/projects/ directory name
by replacing every non-alphanumeric character with '-' (not just '/'). A
dotted or underscored cwd previously resolved to a directory that does not
exist, so the report silently found no transcripts.
"""

import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "plugins" / "cache-kit" / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from cache_report import find_project_dir


class TestEncoding:
    def test_plain_path(self):
        assert find_project_dir("/Users/c/Dev/plugins-kit").name == "-Users-c-Dev-plugins-kit"

    def test_dotted_path(self):
        # Real-world example: ~/.claude itself encodes with a double dash
        assert find_project_dir("/Users/c/.claude").name == "-Users-c--claude"

    def test_underscore_and_dot_mix(self):
        assert find_project_dir("/tmp/my.proj_x").name == "-tmp-my-proj-x"

    def test_returns_projects_subdir(self):
        p = find_project_dir("/a/b")
        assert p.parent == Path.home() / ".claude" / "projects"


class TestLongPathEncoding:
    """X12: encoded names > 200 chars are truncated to 200 plus a hash
    suffix of the original cwd. Expected values below were produced by
    running the CLI bundle's own functions (PY/OYH, claude 2.1.170)
    under Node.
    """

    def test_exactly_200_chars_stays_plain(self):
        cwd = "/" + "x" * 199  # encodes to exactly 200 chars
        assert find_project_dir(cwd).name == "-" + "x" * 199

    def test_201_chars_truncates_and_hashes(self):
        cwd = "/" + "x" * 200  # encodes to 201 chars
        assert find_project_dir(cwd).name == "-" + "x" * 199 + "-d0i18x"

    def test_long_path_matches_cli_scheme(self):
        cwd = "/Users/someone/" + "a" * 250
        expected = "-Users-someone-" + "a" * 185 + "-cnjskt"
        assert find_project_dir(cwd).name == expected

    def test_long_path_with_separators_matches_cli_scheme(self):
        cwd = "/tmp/" + "very/deep/" * 30 + "project"
        name = find_project_dir(cwd).name
        assert name.startswith("-tmp-very-deep-")
        assert name.endswith("-u1ickm")
        assert len(name) == 200 + 1 + 6  # slice(0,200) + "-" + suffix
