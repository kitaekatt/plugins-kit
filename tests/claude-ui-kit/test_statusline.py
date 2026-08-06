"""Tests for statusline.sh hygiene fixes.

X14: malformed stdin must produce a minimal fallback line (exit 0), not a
silent blank (set -euo pipefail used to kill the script on jq parse failure).
X11: the plugin's bootstrap.json must declare its jq dependency explicitly
instead of relying on the bootstrap plugin's manifest transitively.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

_PLUGIN_ROOT = Path(__file__).resolve().parents[2] / "plugins" / "claude-ui-kit"
_STATUSLINE = _PLUGIN_ROOT / "scripts" / "statusline.sh"

_HAS_TOOLS = shutil.which("bash") and shutil.which("jq")


def run_statusline(stdin_text, cwd, extra_env=None):
    env = dict(os.environ)
    env.pop("BOOTSTRAP_BIN_JQ", None)  # use PATH jq deterministically
    env.pop("STATUSLINE_SHOW_MODEL", None)  # test the default unless overridden
    if extra_env:
        env.update(extra_env)
    # encoding pinned to UTF-8: the statusline emits non-ASCII glyphs
    # (effort meter blocks, separators); without this, Windows decodes the
    # pipe as cp1252, the reader thread raises UnicodeDecodeError, and
    # result.stdout comes back None.
    return subprocess.run(
        ["bash", str(_STATUSLINE)], input=stdin_text, cwd=cwd,
        env=env, capture_output=True, text=True, timeout=30,
        encoding="utf-8", errors="replace")


@pytest.mark.skipif(not _HAS_TOOLS, reason="bash + jq required")
class TestMalformedStdinFallback:
    def test_malformed_json_emits_fallback_line(self, tmp_path):
        result = run_statusline("this is not json", tmp_path)
        assert result.returncode == 0
        assert result.stdout.strip(), "fallback must not be blank"

    def test_empty_stdin_emits_fallback_line(self, tmp_path):
        result = run_statusline("", tmp_path)
        assert result.returncode == 0
        assert result.stdout.strip()

    def test_valid_json_renders_normal_line(self, tmp_path):
        payload = json.dumps({
            "model": {"display_name": "TestModel", "id": "m-1"},
            "cwd": str(tmp_path / "myproj"),
        })
        result = run_statusline(payload, tmp_path)
        assert result.returncode == 0
        assert "myproj" in result.stdout


@pytest.mark.skipif(not _HAS_TOOLS, reason="bash + jq required")
class TestModelEffortSegment:
    """Model + effort meter segment (on by default, STATUSLINE_SHOW_MODEL=0 hides)."""

    _GLYPHS = {"low": "▁", "medium": "▃", "high": "▅",
               "xhigh": "▇", "max": "█"}

    @staticmethod
    def _payload(display_name="Fable 5", effort=None):
        data = {"model": {"display_name": display_name, "id": "m-1"},
                "cwd": "/tmp/myproj"}
        if effort is not None:
            data["effort"] = {"level": effort}
        return json.dumps(data)

    @pytest.mark.parametrize("level", list(_GLYPHS))
    def test_effort_level_renders_meter_glyph(self, tmp_path, level):
        result = run_statusline(self._payload(effort=level), tmp_path)
        assert result.returncode == 0
        assert f"{self._GLYPHS[level]} Fable" in result.stdout

    def test_version_stripped_from_display_name(self, tmp_path):
        result = run_statusline(self._payload("Opus 4.8", effort="high"), tmp_path)
        assert "Opus" in result.stdout
        assert "4.8" not in result.stdout

    def test_effort_absent_renders_bare_model_name(self, tmp_path):
        result = run_statusline(self._payload(), tmp_path)
        assert "Fable" in result.stdout
        for glyph in self._GLYPHS.values():
            assert glyph not in result.stdout

    def test_show_model_zero_hides_segment(self, tmp_path):
        result = run_statusline(self._payload(effort="xhigh"), tmp_path,
                                extra_env={"STATUSLINE_SHOW_MODEL": "0"})
        assert result.returncode == 0
        assert "Fable" not in result.stdout
        assert "myproj" in result.stdout


class TestBootstrapManifestDeclaresJq:
    def test_jq_declared_in_tools(self):
        manifest = json.loads((_PLUGIN_ROOT / "bootstrap.json").read_text(encoding="utf-8"))
        names = [t.get("name") for t in manifest.get("tools", [])]
        assert "jq" in names, (
            "statusline.sh uses jq; the dependency must stay declared in "
            "claude-ui-kit/bootstrap.json (X11), not satisfied transitively")


@pytest.mark.skipif(not _HAS_TOOLS, reason="bash + jq required")
class TestSegmentApi:
    """Contributed cells from the segments dir (STATUSLINE_SEGMENTS_DIR
    override): *.sh executed with stdin JSON under a hard timeout, *.txt
    rendered while fresh. A broken segment loses only itself."""

    _PAYLOAD = json.dumps({
        "model": {"display_name": "TestModel", "id": "m-1"},
        "cwd": "/tmp/myproj",
    })

    def _run_with_segments(self, tmp_path, segments_dir, extra_env=None):
        env = {"STATUSLINE_SEGMENTS_DIR": str(segments_dir)}
        if extra_env:
            env.update(extra_env)
        return run_statusline(self._PAYLOAD, tmp_path, extra_env=env)

    def test_sh_segment_output_appended(self, tmp_path):
        segs = tmp_path / "segments"
        segs.mkdir()
        (segs / "50-hello.sh").write_text("#!/usr/bin/env bash\necho HELLO-CELL\n")
        result = self._run_with_segments(tmp_path, segs)
        assert result.returncode == 0
        assert "HELLO-CELL" in result.stdout
        assert "myproj" in result.stdout  # base bar intact

    def test_sh_segment_receives_stdin_json(self, tmp_path):
        segs = tmp_path / "segments"
        segs.mkdir()
        (segs / "50-cwd.sh").write_text(
            "#!/usr/bin/env bash\njq -r '.model.display_name' \n")
        result = self._run_with_segments(tmp_path, segs)
        assert result.stdout.count("TestModel") >= 2  # model cell + segment

    def test_failing_segment_is_absent_bar_survives(self, tmp_path):
        segs = tmp_path / "segments"
        segs.mkdir()
        (segs / "50-bad.sh").write_text("#!/usr/bin/env bash\nexit 3\n")
        result = self._run_with_segments(tmp_path, segs)
        assert result.returncode == 0
        assert "myproj" in result.stdout

    def test_hanging_segment_times_out_bar_survives(self, tmp_path):
        segs = tmp_path / "segments"
        segs.mkdir()
        (segs / "50-hang.sh").write_text(
            "#!/usr/bin/env bash\nsleep 30\necho NEVER\n")
        result = self._run_with_segments(
            tmp_path, segs, extra_env={"STATUSLINE_SEGMENT_TIMEOUT": "1"})
        assert result.returncode == 0
        assert "NEVER" not in result.stdout
        assert "myproj" in result.stdout

    def test_fresh_txt_segment_shown(self, tmp_path):
        segs = tmp_path / "segments"
        segs.mkdir()
        (segs / "10-note.txt").write_text("deploy at noon\nsecond line\n")
        result = self._run_with_segments(tmp_path, segs)
        assert "deploy at noon" in result.stdout
        assert "second line" not in result.stdout

    def test_stale_txt_segment_hidden(self, tmp_path):
        segs = tmp_path / "segments"
        segs.mkdir()
        f = segs / "10-note.txt"
        f.write_text("old news\n")
        old = 10_000
        os.utime(f, (old, old))
        result = self._run_with_segments(tmp_path, segs)
        assert "old news" not in result.stdout

    def test_lexical_order(self, tmp_path):
        segs = tmp_path / "segments"
        segs.mkdir()
        (segs / "20-b.sh").write_text("#!/usr/bin/env bash\necho BBB\n")
        (segs / "10-a.sh").write_text("#!/usr/bin/env bash\necho AAA\n")
        result = self._run_with_segments(tmp_path, segs)
        assert result.stdout.index("AAA") < result.stdout.index("BBB")

    def test_missing_segments_dir_is_noop(self, tmp_path):
        result = self._run_with_segments(tmp_path, tmp_path / "absent")
        assert result.returncode == 0
        assert "myproj" in result.stdout

    def test_multiline_sh_segment_cannot_split_the_bar(self, tmp_path):
        """ui-kit owns composition: a segment ignoring the single-line contract
        degrades itself, it does not turn the status line into two lines."""
        segs = tmp_path / "segments"
        segs.mkdir()
        (segs / "50-chatty.sh").write_text(
            "#!/usr/bin/env bash\necho FIRST-LINE\necho SECOND-LINE\n")
        result = self._run_with_segments(tmp_path, segs)
        assert result.returncode == 0
        assert result.stdout.count("\n") == 1, "status line must stay one line"
        assert "FIRST-LINE" in result.stdout
        assert "SECOND-LINE" not in result.stdout

    def test_overlong_sh_segment_is_capped(self, tmp_path):
        """An unbounded segment must not blow out the bar."""
        segs = tmp_path / "segments"
        segs.mkdir()
        (segs / "50-long.sh").write_text(
            "#!/usr/bin/env bash\nprintf 'X%.0s' $(seq 1 400)\necho\n")
        result = self._run_with_segments(tmp_path, segs)
        assert result.returncode == 0
        assert "X" * 120 in result.stdout
        assert "X" * 200 not in result.stdout


@pytest.mark.skipif(not _HAS_TOOLS, reason="bash + jq required")
class TestRateLimitSnapshot:
    """The hook payload is the only place Claude Code surfaces .rate_limits, and
    only a statusline ever receives it -- so the statusline snapshots it for
    other tools (awesome-kit's orchestrate skill). File contract, not an import
    edge: a failure here must never cost the user their status line."""

    SNAPSHOT = Path("plugins/data/plugins-kit/claude-ui-kit/rate-limits.json")

    def _payload(self, cwd, **limits):
        body = {"model": {"display_name": "TestModel", "id": "m-1"}, "cwd": str(cwd)}
        if limits:
            body["rate_limits"] = limits
        return json.dumps(body)

    def _run(self, tmp_path, payload):
        home = tmp_path / "home"
        home.mkdir()
        result = run_statusline(payload, tmp_path, extra_env={"HOME": str(home)})
        return result, home / ".claude" / self.SNAPSHOT

    def test_snapshot_is_written_from_the_payload(self, tmp_path):
        payload = self._payload(
            tmp_path,
            five_hour={"used_percentage": 42.5, "resets_at": 1800000000},
            seven_day={"used_percentage": 61.0, "resets_at": 1800000000},
        )
        result, snapshot = self._run(tmp_path, payload)
        assert result.returncode == 0
        data = json.loads(snapshot.read_text(encoding="utf-8"))
        assert data["rate_limits"]["five_hour"]["used_percentage"] == 42.5
        assert data["rate_limits"]["seven_day"]["resets_at"] == 1800000000
        assert isinstance(data["captured_at"], int)

    def test_payload_without_rate_limits_writes_nothing(self, tmp_path):
        result, snapshot = self._run(tmp_path, self._payload(tmp_path))
        assert result.returncode == 0
        assert not snapshot.exists()

    def test_snapshot_can_be_disabled(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        payload = self._payload(tmp_path, five_hour={"used_percentage": 10.0})
        result = run_statusline(
            payload, tmp_path,
            extra_env={"HOME": str(home), "STATUSLINE_RATE_LIMIT_SNAPSHOT": "0"})
        assert result.returncode == 0
        assert not (home / ".claude" / self.SNAPSHOT).exists()

    def test_no_temp_files_are_left_behind(self, tmp_path):
        payload = self._payload(tmp_path, five_hour={"used_percentage": 10.0})
        _, snapshot = self._run(tmp_path, payload)
        assert list(snapshot.parent.glob("*.tmp")) == []

    def test_statusline_still_renders_when_the_snapshot_cannot_be_written(self, tmp_path):
        """An unwritable HOME degrades the snapshot, never the status line."""
        blocker = tmp_path / "blocked"
        blocker.write_text("not a directory", encoding="utf-8")
        payload = self._payload(tmp_path / "myproj", five_hour={"used_percentage": 10.0})
        result = run_statusline(payload, tmp_path, extra_env={"HOME": str(blocker)})
        assert result.returncode == 0
        # Assert the real content rendered. `or result.stdout.strip()` would
        # pass on ANY non-empty output, including a degraded fallback line.
        assert "myproj" in result.stdout

    def test_unset_home_does_not_kill_the_statusline(self, tmp_path):
        """Under `set -u` a bare $HOME aborts the script; the snapshot must be
        skipped instead, or an env without HOME loses its status line."""
        env = dict(os.environ)
        env.pop("HOME", None)
        env.pop("BOOTSTRAP_BIN_JQ", None)
        payload = self._payload(tmp_path / "myproj", five_hour={"used_percentage": 10.0})
        result = subprocess.run(
            ["bash", str(_STATUSLINE)], input=payload, cwd=tmp_path, env=env,
            capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace")
        assert result.returncode == 0, result.stderr
        assert "myproj" in result.stdout
        assert "unbound variable" not in result.stderr
