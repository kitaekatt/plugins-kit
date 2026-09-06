#!/usr/bin/env python3
"""Cache usage report for Claude Code sessions.

Reads session transcript JSONL files from ~/.claude/projects/ and reports
per-request and aggregate cache hit statistics.

Usage:
    python3 cache-report.py                  # most recent session for CWD project
    python3 cache-report.py SESSION_ID       # specific session by ID
    python3 cache-report.py --all            # all sessions for CWD project
    python3 cache-report.py --detailed       # include per-request breakdown
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path


def _config_dir() -> Path:
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(configured) if configured else Path.home() / ".claude"


def _encode_cwd(cwd: str) -> str:
    """Encode a cwd one UTF-16 code unit at a time like Claude Code."""
    encoded = []
    for character in cwd:
        if character.isascii() and character.isalnum():
            encoded.append(character)
        elif ord(character) > 0xFFFF:
            encoded.append("--")
        else:
            encoded.append("-")
    return "".join(encoded)


def _cli_hash_suffix(cwd: str) -> str:
    """Mirror the CLI's overflow hash suffix for long encoded cwds.

    Verified against the CLI bundle (claude 2.1.170):

        function OYH(H){let _=0;for(let A=0;A<H.length;A++)
            _=(_<<5)-_+H.charCodeAt(A)|0;return _}
        function hI4(H){return Math.abs(OYH(H)).toString(36)}

    i.e. the signed-32-bit JS string hash (h = 31*h + codeunit, over UTF-16
    code units of the ORIGINAL un-encoded cwd), absolute value, base 36.
    """
    h = 0
    units = cwd.encode("utf-16-le")
    for i in range(0, len(units), 2):
        h = (31 * h + int.from_bytes(units[i:i + 2], "little")) & 0xFFFFFFFF
    if h >= 1 << 31:
        h -= 1 << 32
    n = abs(h)
    if n == 0:
        return "0"
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = []
    while n:
        n, r = divmod(n, 36)
        out.append(digits[r])
    return "".join(reversed(out))


def find_project_dir(cwd: str) -> Path:
    """Find the ~/.claude/projects/ directory for the given working directory.

    Claude Code encodes the cwd by replacing EVERY non-alphanumeric character
    with '-' (verified against the CLI bundle: path.replace(/[^a-zA-Z0-9]/g,
    "-")), so dots and underscores encode too -- e.g. /Users/x/.claude ->
    -Users-x--claude. Encoded names longer than 200 chars are truncated to
    200 and suffixed with a hash of the original cwd (CLI bundle:
    `if(_.length<=DQH)return _;return\\`${_.slice(0,DQH)}-${hI4(H)}\\``
    with DQH=200); `_cli_hash_suffix` mirrors hI4.
    """
    encoded = _encode_cwd(cwd)
    if len(encoded) > 200:
        encoded = f"{encoded[:200]}-{_cli_hash_suffix(cwd)}"
    return _config_dir() / "projects" / encoded


def find_transcript(session_id: str | None, project_dir: Path) -> Path | None:
    """Find transcript file by session ID or return the most recent one."""
    if session_id:
        candidate = project_dir / f"{session_id}.jsonl"
        if candidate.exists():
            return candidate
        # Search all project dirs
        projects_dir = _config_dir() / "projects"
        if projects_dir.exists():
            for p in projects_dir.iterdir():
                candidate = p / f"{session_id}.jsonl"
                if candidate.exists():
                    return candidate
        return None

    if not project_dir.exists():
        return None
    transcripts = sorted(project_dir.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
    for transcript in transcripts:
        if _has_usage_data(transcript):
            return transcript
    return None


def _has_usage_data(transcript_path: Path) -> bool:
    """Return True if the transcript contains at least one assistant usage entry."""
    return bool(parse_transcript(transcript_path))


def find_all_transcripts(project_dir: Path) -> list[Path]:
    """Return all transcript files for a project, sorted oldest-first."""
    if not project_dir.exists():
        return []
    return sorted(project_dir.glob("*.jsonl"), key=lambda f: f.stat().st_mtime)


def _parse_transcript(transcript_path: Path, seen_ids: set[str]) -> list[dict]:
    """Extract usage data from each assistant message in the transcript."""
    entries = []
    with open(transcript_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            if entry.get("type") != "assistant":
                continue

            message = entry.get("message", {})
            usage = message.get("usage")
            if not usage:
                continue
            message_id = message.get("id")
            if message_id is not None:
                if message_id in seen_ids:
                    continue
                seen_ids.add(message_id)

            cache_creation = usage.get("cache_creation", {})
            entries.append(
                {
                    "timestamp": entry.get("timestamp", ""),
                    "model": message.get("model", "unknown"),
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                    "cache_creation_tokens": usage.get("cache_creation_input_tokens", 0),
                    "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
                    "cache_creation_1h": cache_creation.get("ephemeral_1h_input_tokens", 0),
                    "cache_creation_5m": cache_creation.get("ephemeral_5m_input_tokens", 0),
                }
            )
    return entries


def parse_transcript(transcript_path: Path) -> list[dict]:
    """Extract unique usage data from one transcript."""
    return _parse_transcript(transcript_path, set())


def _session_paths(transcript_path: Path) -> list[Path]:
    subagents = transcript_path.with_suffix("") / "subagents"
    if not subagents.exists():
        return [transcript_path]
    return [transcript_path] + sorted(subagents.glob("*.jsonl"))


def parse_session(transcript_path: Path) -> tuple[list[dict], int]:
    """Extract unique usage data from a main transcript and its subagents."""
    paths = _session_paths(transcript_path)
    seen_ids: set[str] = set()
    entries: list[dict] = []
    for path in paths:
        entries.extend(_parse_transcript(path, seen_ids))
    return entries, len(paths) - 1


def totals(entries: list[dict]) -> dict:
    keys = [
        "input_tokens",
        "output_tokens",
        "cache_creation_tokens",
        "cache_read_tokens",
        "cache_creation_1h",
        "cache_creation_5m",
    ]
    t = {k: sum(e[k] for e in entries) for k in keys}
    t["total_input"] = t["input_tokens"] + t["cache_creation_tokens"] + t["cache_read_tokens"]
    t["cache_hit_rate"] = (
        t["cache_read_tokens"] / t["total_input"] * 100 if t["total_input"] > 0 else 0.0
    )
    return t


def fmt(n: int) -> str:
    return f"{n:,}"


def render_session_report(entries: list[dict], transcript_path: Path, subagent_count: int = 0) -> str:
    if not entries:
        return f"No usage data found in: {transcript_path}"

    t = totals(entries)
    requests = len(entries)

    # Session timestamp from first/last entry
    first_ts = entries[0]["timestamp"][:19].replace("T", " ") if entries[0]["timestamp"] else "?"
    last_ts = entries[-1]["timestamp"][:19].replace("T", " ") if entries[-1]["timestamp"] else "?"

    lines = [
        f"## Cache Usage Report",
        f"",
        f"Session:   {transcript_path.stem}",
        f"Transcripts: 1 main + {subagent_count} subagent",
        f"Period:    {first_ts} -> {last_ts}",
        f"Requests:  {requests}",
        f"",
        f"### Token Summary",
        f"{'Metric':<38} {'Tokens':>12}",
        f"{'-'*51}",
        f"{'Total input (all sources)':<38} {fmt(t['total_input']):>12}",
        f"{'  Direct input tokens':<38} {fmt(t['input_tokens']):>12}",
        f"{'  Cache write tokens':<38} {fmt(t['cache_creation_tokens']):>12}",
    ]

    if t["cache_creation_1h"] or t["cache_creation_5m"]:
        lines += [
            f"{'    1h TTL':<38} {fmt(t['cache_creation_1h']):>12}",
            f"{'    5m TTL':<38} {fmt(t['cache_creation_5m']):>12}",
        ]

    lines += [
        f"{'  Cache read tokens (hits)':<38} {fmt(t['cache_read_tokens']):>12}",
        f"{'Output tokens':<38} {fmt(t['output_tokens']):>12}",
        f"",
        f"### Cache Performance",
        f"Hit rate:         {t['cache_hit_rate']:.1f}%",
        f"Tokens from cache: {fmt(t['cache_read_tokens'])} / {fmt(t['total_input'])} total input",
        f"Tokens bypassed cache: {fmt(t['input_tokens'])} ({t['input_tokens']/t['total_input']*100:.1f}%)" if t["total_input"] else "",
    ]

    return "\n".join(l for l in lines if l)


def render_per_request_breakdown(entries: list[dict]) -> str:
    lines = [
        f"",
        f"### Per-Request Breakdown",
        f"{'#':<4} {'Model':<28} {'Input':>8} {'Write':>8} {'Read':>8} {'Out':>8} {'Hit%':>6}",
        f"{'-'*73}",
    ]

    for i, e in enumerate(entries, 1):
        row_total = e["input_tokens"] + e["cache_creation_tokens"] + e["cache_read_tokens"]
        hit_pct = e["cache_read_tokens"] / row_total * 100 if row_total > 0 else 0.0
        model = e["model"].split("/")[-1]
        # Shorten common model names
        model = re.sub(r"-\d{8}$", "", model.replace("claude-", ""))
        lines.append(
            f"{i:<4} {model:<28} {fmt(e['input_tokens']):>8} "
            f"{fmt(e['cache_creation_tokens']):>8} {fmt(e['cache_read_tokens']):>8} "
            f"{fmt(e['output_tokens']):>8} {hit_pct:>5.0f}%"
        )

    return "\n".join(lines)


def render_all_sessions_report(transcripts: list[Path]) -> str:
    if not transcripts:
        return "No transcripts found."

    lines = [
        f"## Cache Usage Report -- All Sessions",
        f"Project: {transcripts[0].parent.name}",
        f"Sessions: 0",
        f"",
        f"{'Session ID':<38} {'Transcripts':<20} {'Reqs':>5} {'TotalIn':>10} {'Write':>10} {'Read':>10} {'Hit%':>6}",
        f"{'-'*103}",
    ]

    grand = {"total_input": 0, "cache_creation_tokens": 0, "cache_read_tokens": 0, "requests": 0}

    session_rows = []
    for t_path in transcripts:
        entries, subagent_count = parse_session(t_path)
        if not entries:
            continue
        session_rows.append((t_path, entries, subagent_count))

    lines[2] = f"Sessions: {len(session_rows)}"
    for t_path, entries, subagent_count in session_rows:
        t = totals(entries)
        session_short = t_path.stem[:36]
        transcript_label = f"1 main + {subagent_count} subagent"
        lines.append(
            f"{session_short:<38} {transcript_label:<20} {len(entries):>5} "
            f"{fmt(t['total_input']):>10} {fmt(t['cache_creation_tokens']):>10} "
            f"{fmt(t['cache_read_tokens']):>10} {t['cache_hit_rate']:>5.1f}%"
        )
        grand["total_input"] += t["total_input"]
        grand["cache_creation_tokens"] += t["cache_creation_tokens"]
        grand["cache_read_tokens"] += t["cache_read_tokens"]
        grand["requests"] += len(entries)

    grand_hit = (
        grand["cache_read_tokens"] / grand["total_input"] * 100
        if grand["total_input"] > 0
        else 0.0
    )
    lines += [
        f"{'-'*103}",
        f"{'TOTAL':<38} {'':<20} {grand['requests']:>5} "
        f"{fmt(grand['total_input']):>10} {fmt(grand['cache_creation_tokens']):>10} "
        f"{fmt(grand['cache_read_tokens']):>10} {grand_hit:>5.1f}%",
    ]

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("session_id", nargs="?")
    parser.add_argument("--session", dest="option_session_id")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--detailed", action="store_true")
    parsed = parser.parse_args()
    cwd = os.getcwd()
    project_dir = find_project_dir(cwd)

    if parsed.all and parsed.detailed:
        parser.error("--detailed cannot be combined with --all")

    if parsed.all:
        transcripts = find_all_transcripts(project_dir)
        print(render_all_sessions_report(transcripts))
        return

    session_id = parsed.session_id or parsed.option_session_id
    transcript = find_transcript(session_id, project_dir)

    if transcript is None:
        if session_id:
            print(f"Error: Session '{session_id}' not found.", file=sys.stderr)
        else:
            print(f"Error: No transcripts found in {project_dir}", file=sys.stderr)
        sys.exit(1)

    entries, subagent_count = parse_session(transcript)
    if session_id and not entries:
        print(f"no usage recorded yet for session {session_id}")
        return
    report = render_session_report(entries, transcript, subagent_count)
    if parsed.detailed:
        report += "\n" + render_per_request_breakdown(entries)
    print(report)


if __name__ == "__main__":
    main()
