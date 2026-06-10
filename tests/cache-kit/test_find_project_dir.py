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
