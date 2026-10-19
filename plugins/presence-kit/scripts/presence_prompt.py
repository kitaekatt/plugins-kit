"""presence-kit UserPromptSubmit evaluation (unit K3, presence-kit 0.3.0).

Stdlib only; sibling imports. Contract: dev/tasks/presence-kit/contracts.md
sections 1, 2, 5 and 7. `evaluate` makes ONE presence_state.update call per
prompt: it reads the stored presence, and WRITES only on a departure -- a prompt
that contains a configured departure word while `afk-on-departure` is on sets the
user afk itself (`enter_afk(..., prompt=pid)`) and says so in `additionalContext`,
so Claude can undo a wrong match with `state set present`. The hook never infers a
departure from anything but the word, and it never reads the transcript (the new
prompt's entry is not written yet at this hook).

Already afk, a departure word keeps the user afk (ruling 19, 2026-10-19): the
hook records the prompt as `afk_prompt` (the Stop hook then does not count this
prompt as a return) and withholds the return-recap line. Output goes to Claude
only (`additionalContext`); the one `systemMessage` is the config error.

The caller (presence.py) has already parsed the payload and validated `session_id`
and `prompt_id` (class `payload` errors); the checks here are a backstop and raise
StateError.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import presence_config as pc
import presence_state as ps

# A prompt that starts (after leading whitespace) with one of these is never read for a
# departure word: the plugin's own slash commands (`/presence-kit:recap afk-ending <id>`),
# the harness's task-notification envelope, and the inter-agent envelope
# (`<agent-message from="...">`, which wraps a subagent hand-back or another session's
# message). The inter-agent envelope may be preceded by the harness's one-line
# "Another Claude session sent a message:" header (the transcript record has it; the
# queued prompt does not), so AGENT_MESSAGE_RE accepts both. Where a source has no
# reliable marker, P3's correction clause is the fallback. A /loop or cron firing has no
# marker either, so the Stop hook records the live session crons' prompts in the state
# (`cron_prompts`) and a prompt that exactly equals one is skipped too (`_cron_firing`).
NOT_THE_USER_PREFIXES = ("/presence-kit:", "<task-notification>")
AGENT_MESSAGE_RE = re.compile(
    r'(?:Another Claude session sent a message:\s*)?<agent-message from="'
)


def _not_the_user(prompt: str) -> bool:
    """True for a prompt that is a known envelope, not the user's own message."""
    text = prompt.lstrip()
    return text.startswith(NOT_THE_USER_PREFIXES) or AGENT_MESSAGE_RE.match(text) is not None


def _cron_firing(prompt: str, doc: dict) -> bool:
    """True when `prompt` (stripped) exactly equals a prompt the Stop hook recorded from
    the session's crons: a fixed-interval firing is the bare cron prompt, a dynamic
    /loop wakeup is the cron's own `/loop ...` prompt."""
    return prompt.strip() in doc["cron_prompts"]


@dataclass(frozen=True)
class PromptOutput:
    additional_context: str | None
    system_message: str | None

    def to_output(self) -> dict[str, Any]:
        """The prompt stdout object (contracts.md section 2): `{}` when nothing to say."""
        out: dict[str, Any] = {}
        if self.system_message is not None:
            out["systemMessage"] = self.system_message
        if self.additional_context is not None:
            out["hookSpecificOutput"] = {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": self.additional_context,
            }
        return out


def config_error_output(err: pc.ConfigError, payload: dict) -> PromptOutput:
    """P4 for the user and P5 for Claude; the prompt proceeds and no state changes."""
    pid = ps.check_prompt_id(payload.get("prompt_id"))
    return PromptOutput(
        additional_context=pc.line(pc.CONFIG_IDS, pid, pc.P5_CONFIG_CONTEXT),
        system_message=pc.line(
            pc.CONFIG_IDS, pid, pc.P4_CONFIG_SYSTEM.format(err_text=err.text)
        ),
    )


def _departure_word(cfg: pc.Config, prompt: str, doc: dict) -> str | None:
    """The matched departure word, or None (off, an excluded prompt kind, or no match)."""
    if not cfg.enabled("afk-on-departure"):
        return None
    if _not_the_user(prompt) or _cron_firing(prompt, doc):
        return None
    found = pc.departure_pattern(cfg.values["departure_words"]).search(prompt)
    return None if found is None else found.group(0)


def _away_line(doc: dict, pid: str) -> str:
    goal = doc["goal"]
    goal_part = "" if goal is None else f" [goal: {goal}]"
    return pc.line(
        pc.P1_AWAY_IDS,
        pid,
        pc.P1_AWAY.format(since=doc["afk_since"], goal_part=goal_part),
    )


def _departure_line(cfg: pc.Config, sid: str, pid: str, word: str, kept: bool) -> str:
    undo = pc.P3_UNDO.format(sid=sid)
    if not kept:
        text = pc.P3_DEPARTURE.format(word=word, undo=undo)
    else:
        clause = ""
        if cfg.enabled("recap-on-return"):
            clause = pc.P3_KEPT_RETURN_CLAUSE.format(pid=pid)
        text = pc.P3_DEPARTURE_KEPT.format(word=word, undo=undo, return_clause=clause)
    return pc.line(pc.P3_DEPARTURE_IDS, pid, text)


def evaluate(
    payload: dict, cfg: pc.Config, env: Mapping[str, str], now: datetime
) -> PromptOutput:
    """The additionalContext for one UserPromptSubmit payload, in one locked state update.

    P3 (a departure was acted on) then P1 (the user counts as away); P2 (return recap)
    only when afk, `recap-on-return` is on and this prompt is not the one the hook
    itself set or kept afk on. Present with no departure: nothing. `now` is injected.
    """
    sid = ps.check_session_id(payload.get("session_id"))
    pid = ps.check_prompt_id(payload.get("prompt_id"))
    prompt = payload.get("prompt")
    if not isinstance(prompt, str):
        prompt = ""
    state_file = str(ps.state_path(sid, env))

    out: list[PromptOutput] = []

    def apply(doc: dict) -> None:
        lines: list[str] = []
        word = _departure_word(cfg, prompt, doc)
        if word is not None:
            kept = doc["presence"] == "afk"
            ps.enter_afk(doc, ps.format_timestamp(now), prompt=pid)
            lines.append(_departure_line(cfg, sid, pid, word, kept))
        if doc["presence"] == "afk":
            if doc["afk_since"] is None:
                raise ps.StateError(
                    "malformed",
                    f"{state_file}: malformed state file -- presence is afk but afk_since is null",
                )
            lines.append(_away_line(doc, pid))
            if cfg.enabled("recap-on-return") and doc["afk_prompt"] != pid:
                lines.append(pc.line(pc.P2_RECAP_IDS, pid, pc.P2_RECAP.format(pid=pid)))
        out.append(PromptOutput("\n".join(lines) or None, None))

    ps.update(sid, apply, env)
    return out[0]
