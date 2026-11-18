"""presence-kit 0.7.0: the `waiter` agent type, and the docs that launch it.

The waiter is a Bash-only haiku agent: a general-purpose waiter measured 143.7k
cache-creation tokens against 58.7k for a Bash-only one on the same task. The agent
body carries the wait.py rules the waiting rule (references/presence.md) states;
these tests pin that the two agree and that the docs launch the agent by its type.
"""
import re
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "presence-kit"
AGENT = PLUGIN / "agents" / "waiter.md"
PRESENCE_MD = PLUGIN / "references" / "presence.md"
CONFIGURING_MD = PLUGIN / "references" / "configuring.md"
README = PLUGIN / "README.md"
PAGE = PLUGIN / "docs" / "how-it-works.html"
PROCEDURES = (
    PLUGIN / "skills" / "afk" / "references" / "afk-procedure.md",
    PLUGIN / "skills" / "autonomous" / "references" / "autonomous-procedure.md",
)


def _split(text):
    match = re.match(r"---\n(.*?)\n---\n(.*)\Z", text, re.DOTALL)
    assert match, "agent file needs YAML frontmatter"
    meta = dict(line.split(": ", 1) for line in match.group(1).splitlines())
    return meta, match.group(2)


def _section(text, heading):
    match = re.search(rf"^#+ {re.escape(heading)}\n(.*?)(?=^#+ |\Z)", text, re.DOTALL | re.MULTILINE)
    assert match, heading
    return match.group(1)


def test_the_agent_is_a_bash_only_low_effort_haiku_named_waiter():
    meta, _ = _split(AGENT.read_text(encoding="ascii"))
    assert meta["name"] == "waiter"
    assert meta["tools"] == "Bash"
    assert meta["model"] == "haiku"
    assert meta["effort"] == "low"
    assert "wait.py" in meta["description"] and meta["description"].strip()


def test_the_body_is_short_and_states_the_wait_py_rules():
    _, body = _split(AGENT.read_text(encoding="ascii"))
    lines = [line for line in body.splitlines() if line.strip()]
    assert len(lines) <= 14
    text = " ".join(body.split())
    assert "foreground" in text and "timeout: 170000" in text
    assert "150" in text and "--slice-seconds" in text
    assert "exit 4" in text.lower() or "Exit 4" in text
    for code in ("0", "1", "2", "3"):
        assert re.search(rf"\b{code}\b", text)
    assert "one line" in text


def test_the_agent_launches_the_job_itself_when_the_brief_gives_the_command():
    """0.8.0 (option A): one agent starts the job, waits on it and reports once."""
    meta, body = _split(AGENT.read_text(encoding="ascii"))
    text = " ".join(body.split())
    assert meta["tools"] == "Bash"  # still Bash only
    assert "`run_in_background: true`" in text
    assert "Output is being written to" in text  # where it reads the output path from
    assert "--output <that path>" in text or "--output <output path>" in text
    assert "--path" in text  # the job's directory, for the process-activity check
    assert "20 lines" in text and "only on failure" in text
    assert "exit code" in text
    assert "no command" in text.lower() or "gives only" in text.lower()  # the fallback: wait on a running job


def test_presence_md_documents_the_waiter_launched_job():
    section = " ".join(_section(PRESENCE_MD.read_text(encoding="ascii"), "Waiter agent").split())
    assert "starts the job" in section and "`run_in_background: true`" in section
    assert "survives" in section  # the job outlives the waiter's turn (probed)
    assert "TaskStop" in section and "hypothesis" in section.lower()
    assert "not in the main session's task list" in section
    assert "already running" in section  # the fallback: main launched it
    assert "`waiter:`" in section  # the description prefix the Stop hook falls back on
    assert "agent and workflow" in section.lower()  # those jobs get no waiter
    assert "--path" in section


def test_presence_md_documents_the_age_rule_and_the_waiter_exemption():
    flat = " ".join(PRESENCE_MD.read_text(encoding="ascii").split())
    assert "`watch_after_minutes`" in flat
    assert "20 minutes" in flat
    assert "treated as old" in flat or "counts as old" in flat
    assert "running waiter agent" in flat and "needs no watch" in flat
    assert "waiter that ended while the job still runs" in flat


def test_presence_md_documents_the_once_per_episode_lines():
    flat = " ".join(PRESENCE_MD.read_text(encoding="ascii").split())
    assert "once per afk episode" in flat
    assert "context compaction" in flat
    assert "ending recap" in flat and "tool work" in flat


def test_configuring_md_lists_watch_after_minutes_and_the_five_values():
    flat = " ".join(CONFIGURING_MD.read_text(encoding="ascii").split())
    assert "| `watch_after_minutes` | integer | `20` |" in flat
    assert "change four values" not in flat and "change five values" in flat
    assert "once per afk episode" in flat  # P1 and E rows


def test_presence_md_launches_the_waiter_by_its_type_with_the_configured_model():
    section = _section(PRESENCE_MD.read_text(encoding="ascii"), "Waiter agent")
    assert "`subagent_type: presence-kit:waiter`" in section
    assert "`waiter_model`" in section
    assert "A 150 s slice returns before that window" in " ".join(section.split())


def test_the_probe_headings_survive():
    text = PRESENCE_MD.read_text(encoding="ascii")
    for heading in ("## Waiting rule", "## Waiter agent", "## Watcher"):
        assert f"\n{heading}\n" in text, heading


def test_the_afk_and_autonomous_procedures_launch_the_waiter_by_its_type():
    for path in PROCEDURES:
        text = " ".join(path.read_text(encoding="ascii").split())
        assert "`subagent_type: presence-kit:waiter`" in text, path.name
        assert "values.waiter_model" in text, path.name
        assert "starts the job itself" in text, path.name


def test_no_document_still_asks_for_a_waiting_on_last_line():
    for path in (PRESENCE_MD, CONFIGURING_MD, README, PAGE):
        text = path.read_text(encoding="ascii")
        assert "Waiting on <" not in text and "Waiting on &lt;" not in text, path.name
        assert "end with `Waiting on" not in text, path.name
        assert "C, W" not in text and "C and W" not in text and "S1, R, C, W" not in text, path.name


def test_no_document_still_says_progress_every_twenty_minutes():
    for path in (PRESENCE_MD, CONFIGURING_MD, README, PAGE):
        text = " ".join(path.read_text(encoding="ascii").split())
        assert "PROGRESS every 20" not in text, path.name
        assert "progress_minutes` value, 20" not in text, path.name
    flat = " ".join(PRESENCE_MD.read_text(encoding="ascii").split())
    assert "`progress_minutes` value, 0" in flat
    assert "off by default" in flat


def test_waiting_rule_makes_a_subagent_wait_in_foreground_slices():
    rule = " ".join(_section(PRESENCE_MD.read_text(encoding="ascii"), "Waiting rule").split())
    assert "subagent that waits on a background job it started" in rule
    assert "does not end its turn" in rule
    assert "scripts/wait.py" in rule and "foreground" in rule
    assert "timeout: 170000" in rule and "exit 4" in rule
    assert "does not spawn a waiter" in rule
