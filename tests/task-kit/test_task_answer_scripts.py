"""Script answerers (activity, task_commits), the grouped repository log and the types it uses."""

from __future__ import annotations

import datetime
import os
import subprocess
from pathlib import Path

import pytest

from task_system import answer_scripts, questions
from task_system.answer_scripts import AnswerScriptError
from task_system.answers_types import CommitInfo, RepoLog, ScriptContext

SHIPPED = questions.default_shipped_dir()
NOW = datetime.datetime(2026, 10, 20, 15, 30)


def git(top: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    proc = subprocess.run(
        ["git", "-C", str(top), "-c", "commit.gpgsign=false", *args],
        capture_output=True, text=True, encoding="utf-8", env=env, check=True,
    )
    return proc.stdout.strip()


def commit(top: Path, subject: str, **files: str) -> str:
    """Write files (keys use ``__`` for ``/``), commit them all, return the sha."""
    for name, content in files.items():
        path = top / name.replace("__", "/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    git(top, "add", "-A")
    git(top, "commit", "-q", "-m", subject)
    return git(top, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path) -> Path:
    top = tmp_path / "repo"
    top.mkdir()
    git(top, "init", "-q", "-b", "main")
    return top


def context(top: Path, folder: Path, now=NOW, *, with_log=True) -> ScriptContext:
    return ScriptContext(
        folder=folder, repo_top=top, log=answer_scripts.read_repo_log(top) if with_log else None, now=now
    )


class TestRepoLogTypes:
    def commits(self, count):
        return [CommitInfo(sha=f"{i:040x}", date="2026-10-20T00:00:00+00:00", subject=f"c{i}") for i in range(count)]

    def test_touching_lists_commits_by_position_and_rank_is_the_smallest(self):
        log = RepoLog.build(Path("/r"), "h", self.commits(3), [["t/a/x.md"], ["t/b/y.md"], ["t/a/z.md", "t/a/x.md"]])
        assert [c.subject for c in log.touching("t/a")] == ["c0", "c2"]
        assert log.rank("t/a") == 0 and log.rank("t/b") == 1 and log.rank("t/none") is None

    def test_position_zero_is_the_newest_commit(self):
        # commits[0] is the newest by contract, so a folder touched by it has rank 0.
        log = RepoLog.build(Path("/r"), "h", self.commits(3), [["t/b/y.md"], ["t/a/x.md"], ["t/a/x.md"]])
        assert log.rank("t/a") == 1 and [c.subject for c in log.touching("t/a")] == ["c1", "c2"]

    def test_a_folders_answers_subfolder_is_not_a_task_record_change(self):
        log = RepoLog.build(
            Path("/r"), "h", self.commits(3),
            [["t/a/answers/orientation/studio.yaml"], ["t/a/answers/x.yaml", "t/b/plan.md"], ["t/a/plan.md"]],
        )
        assert [c.subject for c in log.touching("t/a")] == ["c2"]
        assert [c.subject for c in log.touching("t/b")] == ["c1"]
        # the parent folder still sees every change below it
        assert [c.subject for c in log.touching("t")] == ["c0", "c1", "c2"]

    def test_a_folder_named_like_answers_deeper_down_still_counts(self):
        log = RepoLog.build(Path("/r"), "h", self.commits(1), [["t/a/notes/answers/x.md"]])
        assert [c.subject for c in log.touching("t/a")] == ["c0"]

    def test_mismatched_inputs_raise(self):
        with pytest.raises(ValueError):
            RepoLog.build(Path("/r"), "h", self.commits(2), [["a"]])


class TestReadRepoLog:
    def test_a_repository_without_commits_has_an_empty_log(self, repo):
        log = answer_scripts.read_repo_log(repo)
        assert log.head is None and log.commits == ()

    def test_commits_come_newest_first_with_full_sha_date_and_subject(self, repo):
        first = commit(repo, "first", t__a__plan_md="1")
        second = commit(repo, "second: with colon", t__a__plan_md="2")
        log = answer_scripts.read_repo_log(repo)
        assert log.head == second
        assert [c.sha for c in log.commits] == [second, first]
        assert [c.subject for c in log.commits] == ["second: with colon", "first"]
        assert datetime.datetime.fromisoformat(log.commits[0].date).tzinfo is not None

    def test_folders_group_their_commits(self, repo):
        commit(repo, "a1", p__a__plan="1")
        commit(repo, "b1", p__b__plan="1")
        commit(repo, "a2", p__a__plan="2", p__b__other="x")
        log = answer_scripts.read_repo_log(repo)
        assert [c.subject for c in log.touching("p/a")] == ["a2", "a1"]
        assert [c.subject for c in log.touching("p/b")] == ["a2", "b1"]
        assert log.rank("p/a") == 0

    def test_both_sides_of_a_rename_are_touched(self, repo):
        commit(repo, "add", old__stub__plan="same content\n" * 20)
        git(repo, "mv", "old/stub", "new_stub")
        git(repo, "commit", "-q", "-m", "move")
        log = answer_scripts.read_repo_log(repo)
        assert [c.subject for c in log.touching("old")] == ["move", "add"]
        assert [c.subject for c in log.touching("new_stub")] == ["move"]

    def test_non_ascii_paths_are_grouped_by_their_real_names(self, repo):
        commit(repo, "accent", **{"p__caf\u00e9__plan": "1"})
        assert [c.subject for c in answer_scripts.read_repo_log(repo).touching("p/caf\u00e9")] == ["accent"]

    def test_the_log_is_one_git_process_per_read_plus_the_head_probe(self, repo, monkeypatch):
        commit(repo, "a", p__a__plan="1")
        calls = []
        real = subprocess.run
        monkeypatch.setattr(answer_scripts.subprocess, "run", lambda cmd, **kw: (calls.append(cmd), real(cmd, **kw))[1])
        answer_scripts.read_repo_log(repo)
        assert len(calls) == 2
        assert sum("log" in c for c in calls) == 1

    def test_repo_top_of_finds_the_work_tree_top(self, repo, tmp_path):
        commit(repo, "a", p__a__plan="1")
        assert answer_scripts.repo_top_of(repo / "p" / "a").resolve() == repo.resolve()
        outside = tmp_path / "outside"
        outside.mkdir()
        assert answer_scripts.repo_top_of(outside) is None

    def test_a_failing_git_command_raises(self, tmp_path):
        with pytest.raises(AnswerScriptError):
            answer_scripts.read_repo_log(tmp_path / "does-not-exist")


class TestTaskCommits:
    def test_answers_with_the_latest_task_commits_newest_first(self, repo):
        commit(repo, "one", p__a__plan="1")
        commit(repo, "other task", p__b__plan="1")
        head = commit(repo, "two", p__a__plan="2")
        answer = answer_scripts.answer("task_commits", context(repo, repo / "p" / "a"))
        assert [c["subject"] for c in answer["commits"]] == ["two", "one"]
        assert answer["commits"][0]["sha"] == head
        assert set(answer["commits"][0]) == {"sha", "date", "subject"}

    def test_answer_commits_never_include_answer_only_commits(self, repo):
        commit(repo, "task edit", p__a__plan="1")
        commit(repo, "answer written", p__a__answers__orientation__studio_yaml="x")
        answer = answer_scripts.answer("task_commits", context(repo, repo / "p" / "a"))
        assert [c["subject"] for c in answer["commits"]] == ["task edit"]

    def test_the_list_stops_at_the_limit(self, repo):
        for index in range(answer_scripts.TASK_COMMITS_LIMIT + 3):
            commit(repo, f"c{index}", p__a__plan=str(index))
        answer = answer_scripts.answer("task_commits", context(repo, repo / "p" / "a"))
        assert len(answer["commits"]) == answer_scripts.TASK_COMMITS_LIMIT
        assert answer["commits"][0]["subject"] == f"c{answer_scripts.TASK_COMMITS_LIMIT + 2}"

    def test_a_task_never_committed_answers_with_no_commits(self, repo):
        commit(repo, "other", p__b__plan="1")
        (repo / "p" / "a").mkdir()
        assert answer_scripts.answer("task_commits", context(repo, repo / "p" / "a")) == {"commits": []}

    def test_cannot_answer_without_a_log_or_a_head(self, repo, tmp_path):
        folder = repo / "p" / "a"
        folder.mkdir(parents=True)
        assert answer_scripts.answer("task_commits", context(repo, folder, with_log=False)) is None
        assert answer_scripts.answer("task_commits", context(repo, folder)) is None  # no commits yet

    def test_the_answer_matches_the_shipped_schema(self, repo):
        commit(repo, "one", p__a__plan="1")
        schema = questions.load_catalog(SHIPPED).get("task_commits").answer_schema
        answer = answer_scripts.answer("task_commits", context(repo, repo / "p" / "a"))
        assert questions.validate_value(schema, answer) == []


class TestActivity:
    def task(self, tmp_path, log: str | None) -> Path:
        folder = tmp_path / "t"
        folder.mkdir()
        if log is not None:
            (folder / "log.md").write_text(log, encoding="utf-8")
        return folder

    def run(self, folder, now=NOW):
        return answer_scripts.answer("activity", ScriptContext(folder=folder, repo_top=folder.parent, log=None, now=now))

    def test_days_since_the_last_dated_entry(self, tmp_path):
        folder = self.task(tmp_path, "- 2026-10-15 09:00: Wired the panel.\n- 2026-10-01 08:00: Started.\n")
        assert self.run(folder) == {"days_since": 5, "last_at": "2026-10-15 09:00", "last_entry": "Wired the panel."}

    def test_the_answer_changes_with_the_clock(self, tmp_path):
        folder = self.task(tmp_path, "- 2026-10-15 09:00: Wired the panel.\n")
        later = NOW + datetime.timedelta(days=3)
        assert self.run(folder, NOW)["days_since"] == 5
        assert self.run(folder, later)["days_since"] == 8

    def test_bookkeeping_lines_do_not_count_as_activity(self, tmp_path):
        folder = self.task(
            tmp_path,
            "- 2026-10-01 08:00: Started.\n- 2026-10-19 08:00: update: refresh (no field edits)\n"
            "- 2026-10-19 09:00: update: summary: refreshed\n",
        )
        assert self.run(folder)["last_entry"] == "Started."
        assert self.run(folder)["days_since"] == 19

    def test_a_date_only_entry_reports_its_date(self, tmp_path):
        folder = self.task(tmp_path, "- 2026-10-18: Parked it.\n")
        assert self.run(folder) == {"days_since": 2, "last_at": "2026-10-18", "last_entry": "Parked it."}

    def test_a_stamp_ahead_of_the_clock_reads_as_today(self, tmp_path):
        folder = self.task(tmp_path, "- 2026-10-22 09:00: Written on a machine ahead of this one.\n")
        assert self.run(folder)["days_since"] == 0

    def test_cannot_answer_without_dated_activity(self, tmp_path):
        assert self.run(self.task(tmp_path, "# Log\n\nnothing yet\n")) is None

    def test_cannot_answer_without_a_log_file(self, tmp_path):
        assert self.run(self.task(tmp_path, None)) is None

    def test_the_answer_is_never_written_to_disk(self, tmp_path):
        folder = self.task(tmp_path, "- 2026-10-15 09:00: Wired the panel.\n")

        def snapshot():
            return {p.relative_to(tmp_path): (p.read_bytes(), p.stat().st_mtime_ns)
                    for p in sorted(tmp_path.rglob("*")) if p.is_file()}

        before = snapshot()
        self.run(folder)
        self.run(folder, NOW + datetime.timedelta(days=9))
        assert snapshot() == before and not (folder / "answers").exists()

    def test_the_answer_matches_the_shipped_schema(self, tmp_path):
        folder = self.task(tmp_path, "- 2026-10-15 09:00: Wired the panel.\n")
        schema = questions.load_catalog(SHIPPED).get("activity").answer_schema
        assert questions.validate_value(schema, self.run(folder)) == []


class TestRegistry:
    def test_every_shipped_script_name_is_registered(self):
        for question in questions.load_catalog(SHIPPED).questions:
            if question.script is not None:
                assert question.script in answer_scripts.script_names()

    def test_an_unknown_script_raises(self, tmp_path):
        ctx = ScriptContext(folder=tmp_path, repo_top=tmp_path, log=None, now=NOW)
        with pytest.raises(AnswerScriptError, match="unknown script"):
            answer_scripts.answer("nope", ctx)
