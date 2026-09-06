import json
import os
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "plugins" / "cache-kit" / "scripts" / "cache_report.py"
_SCRIPTS_DIR = _SCRIPT.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from cache_report import (
    find_project_dir,
    parse_transcript,
    render_all_sessions_report,
    render_per_request_breakdown,
    render_session_report,
    totals,
)


def usage_entry(message_id: str, value: int = 10) -> dict:
    return {
        "type": "assistant",
        "timestamp": "2026-09-05T12:00:00.000Z",
        "message": {
            "id": message_id,
            "model": "anthropic/claude-opus-5-20260401",
            "usage": {
                "input_tokens": value,
                "output_tokens": 4,
                "cache_creation_input_tokens": value * 2,
                "cache_read_input_tokens": value * 3,
                "cache_creation": {
                    "ephemeral_1h_input_tokens": value,
                    "ephemeral_5m_input_tokens": value,
                },
            },
        },
    }


def write_jsonl(path: Path, entries: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n")


def test_parse_transcript_deduplicates_message_id_and_keeps_exact_totals(tmp_path):
    path = tmp_path / "session.jsonl"
    write_jsonl(path, [usage_entry("same"), usage_entry("same")])

    entries = parse_transcript(path)

    assert len(entries) == 1
    assert totals(entries) == {
        "input_tokens": 10,
        "output_tokens": 4,
        "cache_creation_tokens": 20,
        "cache_read_tokens": 30,
        "cache_creation_1h": 10,
        "cache_creation_5m": 10,
        "total_input": 60,
        "cache_hit_rate": 50.0,
    }


def test_renderers_have_exact_small_fixture_values(tmp_path):
    path = tmp_path / "session.jsonl"
    write_jsonl(path, [usage_entry("one")])
    entries = parse_transcript(path)

    report = render_session_report(entries, path)
    detail = render_per_request_breakdown(entries)

    assert "Total input (all sources)" in report
    assert "60" in report
    assert "Hit rate:         50.0%" in report
    assert "opus-5" in detail
    assert "50%" in detail


def test_zero_tokens_have_no_trailing_blank_line(tmp_path):
    path = tmp_path / "zero.jsonl"
    write_jsonl(path, [usage_entry("zero", value=0)])

    report = render_session_report(parse_transcript(path), path)

    assert not report.endswith("\n\n")


def test_subagent_usage_is_included_once_and_reported(tmp_path):
    main = tmp_path / "session-id.jsonl"
    child_dir = tmp_path / "session-id" / "subagents"
    child_dir.mkdir(parents=True)
    child = child_dir / "agent.jsonl"
    empty_child = child_dir / "empty.jsonl"
    write_jsonl(main, [usage_entry("main")])
    write_jsonl(child, [usage_entry("child"), usage_entry("child")])
    write_jsonl(empty_child, [])

    from cache_report import parse_session

    entries, transcript_count = parse_session(main)
    report = render_session_report(entries, main, transcript_count)

    assert len(entries) == 2
    assert "Transcripts: 1 main + 2 subagent" in report


def test_all_counts_sessions_with_usage_rows_not_files(tmp_path):
    used = tmp_path / "used.jsonl"
    empty = tmp_path / "snapshot.jsonl"
    write_jsonl(used, [usage_entry("used")])
    write_jsonl(empty, [{"type": "file-history-snapshot"}])

    report = render_all_sessions_report([used, empty])

    assert "Sessions: 1" in report


def test_all_reports_subagent_transcript_count(tmp_path):
    main = tmp_path / "used.jsonl"
    child_dir = tmp_path / "used" / "subagents"
    child_dir.mkdir(parents=True)
    write_jsonl(main, [usage_entry("main")])
    write_jsonl(child_dir / "child.jsonl", [usage_entry("child")])

    report = render_all_sessions_report([main])

    assert "1 main + 1 subagent" in report


def test_model_release_date_suffix_is_removed(tmp_path):
    path = tmp_path / "session.jsonl"
    write_jsonl(path, [usage_entry("model")])

    assert "opus-5" in render_per_request_breakdown(parse_transcript(path))


def test_astral_path_uses_two_utf16_replacement_units(monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

    assert find_project_dir("/tmp/\U0001f4a9").name == "-tmp---"


def test_config_dir_override(monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/custom/config")

    assert find_project_dir("/tmp/project").parent == Path("/custom/config") / "projects"


def test_all_detailed_is_an_argparse_error(tmp_path):
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "--all", "--detailed"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "--detailed cannot be combined with --all" in result.stderr
    assert "Traceback" not in result.stderr


def test_explicit_empty_session_does_not_fall_back(monkeypatch, tmp_path):
    project = tmp_path / "projects" / "-tmp-project"
    project.mkdir(parents=True)
    empty = project / "current.jsonl"
    older = project / "older.jsonl"
    write_jsonl(empty, [{"type": "file-history-snapshot"}])
    write_jsonl(older, [usage_entry("old")])
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "--session", "current"],
        cwd=tmp_path,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "no usage recorded yet for session current" in result.stdout
    assert "old" not in result.stdout


def test_positional_session_wins_over_session_option(monkeypatch, tmp_path):
    project = tmp_path / "projects" / "-tmp-project"
    project.mkdir(parents=True)
    write_jsonl(project / "positional.jsonl", [])
    write_jsonl(project / "option.jsonl", [usage_entry("option")])
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "positional", "--session", "option"],
        cwd=workdir,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "no usage recorded yet for session positional" in result.stdout


def test_missing_config_dir_is_clean_error(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home)}
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "missing"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "Session 'missing' not found." in result.stderr
    assert "Traceback" not in result.stderr


def test_skill_docs_use_no_project_and_current_session():
    skill = (_ROOT / "plugins/cache-kit/skills/cache-report/SKILL.md").read_text()
    readme = (_ROOT / "plugins/cache-kit/README.md").read_text()

    assert "--no-project" in skill
    assert "${CLAUDE_SESSION_ID}" in skill
    assert "uv run python" not in skill
    assert "uv run python" not in readme
    assert "cost" not in skill.lower() or "costs are not reported" in skill.lower()
    # The --all/--detailed exclusivity must live where agents read it, not only in README.
    assert "cannot be combined with --all" in skill
