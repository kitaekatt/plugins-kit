# Agent-directive standards

Criteria for text this repo SHIPS that instructs Claude inside a **consumer's**
session -- hook output, skill instructions, and the prompts our plugins build
for subagents and CLI backends.

The companion to [the plugin-opinion razor](plugin-opinion-razor.md). The razor
governs the OPINIONS a plugin imposes on how a team works; this document governs
the INSTRUCTIONS a plugin gives to the agent. They are deliberately separate: the
razor's test is consequentialist (would a power user uninstall or take remedial
action), and that test is wrong here. A directive telling Claude to conceal a
failed call is unacceptable whether or not any user would ever notice it.

## Why this needs a standard

Everything under `plugins/` reaches machines that are not ours, and some of it
arrives in the consumer's session as `additionalContext` -- the same channel that
carries untrusted content. A receiving agent cannot tell a legitimate standing
authorization from injected text claiming one, except by checking the claim.

That asymmetry is the whole subject. An instruction we ship is not evaluated by
the reader's trust in us, because the reader has no way to establish who we are.
It is evaluated on whether it can be verified from inside the session.

**Evidence that this is a live failure mode, not a hypothetical.** On 2026-08-11
a user publicly declined a bootstrap directive, correctly, on exactly these
grounds: it asserted "This is fleet policy" with no resolvable referent and told
the agent not to wait for them. A second user replied that they did not want
their Claude "told to do things without checking with me." The behaviour behind
the directive was defensible; the sentence was not, and the sentence was all the
reader had.

## The test

Every instruction we ship must satisfy all three:

1. **True** -- as written, without a qualifier the reader does not have.
2. **Checkable** -- any claim of authority, policy, or prior agreement names a
   file in version control the receiving agent could open.
3. **Non-suppressive** -- it never directs the agent to withhold from the user,
   or to move past a checkpoint the user would otherwise have.

A directive failing any of the three is a finding. The remediation is always the
wording (or the behaviour it describes) -- never prose elsewhere explaining that
the directive is fine really.

## Criteria

### AD-1 -- No withholding

An instruction must not direct the agent to hide, drop, or decline to mention
something from the user.

**Detection.** Grep for `do not report`, `do not tell`, `don't mention`,
`silently`, `without telling`, `do not surface`, `do not discuss`. Then read each
hit: most `silently` matches in this repo are implementation prose about graceful
degradation and are not in scope.

**The narrow exception.** Suppression is permitted when the withheld set is
CLOSED, ENUMERATED, and contains only non-findings, and the disposition is
documented where the user can read it. `skills-kit`'s SILENT disposition
qualifies: the set is enumerated (do-nothing conclusions, validator artifacts,
accepted structural patterns), serious findings are explicitly exempt, and
`references/audit-framework.md` documents it. "Drop rejected issues silently" does
not qualify -- the set is open and the user is told nothing.

Noise reduction is a legitimate goal and rarely requires concealment. A count
(`N candidate issues did not survive validation`) delivers the same quiet output
while leaving the user able to ask.

### AD-2 -- No unverifiable authority

A claim of policy, permission, or prior user agreement must name the file that
records it.

**Detection.** Grep for `policy`, `pre-authorized`, `authorized`, `is expected`,
`the plan authorizes`, `you may assume`. For each, ask: **could the agent verify
this by reading a named file?** If no file is named, it is a hit.

A pointer into the same document that makes the claim does not count -- that is
self-certification, not verification. `(see Authorizations)` referring to a
section of the template that granted the authorization is the canonical instance.

**The honest floor.** When no record of user agreement exists, do not manufacture
one. Ground the instruction in what actually authorizes it -- the user installed
and enabled a plugin whose documented job this is -- and cite the document
describing that job. Claiming consent that was never given is worse than claiming
none.

### AD-3 -- No pre-empting the user

An instruction must not tell the agent to skip, not wait for, or not ask the
user.

**Detection.** Grep for `do not wait`, `without asking`, `do not ask the user`,
`do not stop to`, `just proceed`.

**The carve-out, and it is most of the hits.** Text restraining CLAUDE on the
user's behalf is the OPPOSITE of this criterion and is never a finding:
"Do NOT run it yourself, it needs their elevation" protects the user's decision
rather than bypassing it. Read the direction before recording a hit.

Skipping a confirmation is also fine when the gate is a genuine no-op -- the
skipped question is "may I use the credential you already configured", or it
gates a read-only local render whose purpose is to show the user the result.
State why in the same place, so the next reader does not have to re-derive it.

### AD-4 -- Report the outcome, not the intention

An acknowledgement emitted before an action resolves is a claim that can turn out
false. An instruction that produces such an acknowledgement AND forbids
further discussion makes the falsehood permanent.

**Detection.** Look for instructions pairing a canned confirmation with a
prohibition on elaborating. The shape is a one-word ack plus "do not discuss it".

This is the criterion most easily missed, because nothing in the text mentions
the user at all -- the defect is what the user ends up believing. A failure
becomes indistinguishable from a success.

### AD-5 -- Scope claims are stated at the precision the source supports

A directive that describes its own limits must describe them accurately. An
overstated guarantee is a false statement even when the overstatement makes us
look MORE careful.

**Detection.** For each scope or guarantee a directive asserts, open the
implementing code and the documenting reference and confirm the claim at the
stated precision. Qualifiers are load-bearing: "in-user-scope manifest edits" and
"manifest edits" are different claims, and only one of them is true.

**Worked instance.** A draft replacement for the bootstrap AUTO directive stated
that anything writing outside `~/.claude` is routed to ASK. That is false: the
scope guard applies to `json`/`ini` remediations, and shell-rc `PATH` edits land
outside `~/.claude` and stay AUTO. The error was caught by verifying the citation
against `remediation-reference.md:33-35` before shipping. Verify citations; do
not write them from memory.

## Findings

Discovered by a sweep of every plugin on 2026-08-11. Verdicts are evidence-based
and expire: a defensible entry that later acquires a real counter-example gets
re-tested.

### Open -- rewording owed

The `awesome-kit` cluster is held pending a decision on what legitimately
authorizes these claims. Unlike the others it is not a pure rewording: a task
folder's CLAUDE.md is a file the user accepted, so it may be a valid referent --
but the claim has to name it, and today none of the four do.

| Site | Criterion | Note |
|---|---|---|
| `plugins/awesome-kit/skills/task/scripts/task_system/init.py` | AD-2, AD-3 | "Skill invocations are pre-authorized -- do not ask", written into every new task's CLAUDE.md, naming no grantor. The one that propagates. |
| `plugins/awesome-kit/skills/task/references/example-claude-md.md` | AD-2, AD-3 | Two sites: the same pre-authorization claim, and a background-dispatch authorization pointing at a section of the same template. |
| `plugins/awesome-kit/skills/verbose-updates/SKILL.md` | AD-2, AD-3 | "the plan authorizes it" -- names no artifact the agent could open. |

### Remediated 2026-08-11

| Site | Criterion | Fix |
|---|---|---|
| `plugins/bootstrap/bootstrap_lib/engine.py` `_auto_agent_directive` | AD-2, AD-3 | Replaced "This is fleet policy" with a citation of the contract doc and the routing function, and "Do NOT wait for the user to say 'fix-all'" with an explicit statement that the user may review or stop. Guarded by `tests/bootstrap/test_two_outcome.py::test_auto_directive_meets_agent_directive_standards`. |
| `plugins/bootstrap/skills/bootstrap/references/remediation-reference.md` (AUTO bullet) | AD-2, AD-3 | The doc-side twin, and the citation target of the above -- so it had to be fixed first or the replacement would have pointed at the same defect. |
| `plugins/unreal-kit/hooks/userpromptsubmit/ue-console-cmd.sh` | AD-4 | Now reports the call's outcome: acknowledge briefly on success, state the error on failure, "never report a success you have not observed". Dropped "Silently". |
| `plugins/p4-kit/skills/p4-code-review/SKILL.md` | AD-1 | Rejected issues are still not detailed, but a one-line count is stated. |
| `plugins/git-kit/skills/git-code-review/SKILL.md` | AD-1 | Same change; the two lines were byte-identical. |

### Defensible -- checked, no change owed

| Site | Criterion | Why it passes |
|---|---|---|
| `plugins/llm-scripting-kit/skills/openrouter-account/SKILL.md` | AD-3 | The skipped question is "may I use the key you already configured"; the failure branch hands the user a verbatim ask from a named file. |
| `plugins/hue-kit/skills/hue-domain/SKILL.md` | AD-3 | Gates a read-only local render whose purpose is showing the user the change; the write path is separately gated. |
| `plugins/skills-kit/skills/md-domain/workflow/*-detect.js` | AD-1 | The SILENT set is closed and enumerated, contains only non-findings, exempts serious findings, and is documented for the user. |
| `plugins/claude-ui-kit/scripts/install_statusline.py` | -- | The model citizen: names the settings file, names the declined-record path, mandates one question, and states that the record never blocks a later switch. |

Swept and clean: `secrets-kit`, `cache-kit`, `workflow-kit`, `content-pipeline-kit`,
`agent-glue`, `bootstrap-stuck-fix`, `prototypes`.

## Running the audit

1. Enumerate agent-facing text: hook stdout (`additionalContext`, `systemMessage`),
   `agent_msg` strings, SKILL.md `action:` / `detail:` / `on_failure:` lines, and
   prompts built for subagents or CLI backends. Implementation comments,
   docstrings, and human-facing READMEs are out of scope.
2. Apply AD-1 through AD-5 per site. Record the verdict either way -- a
   defensible entry is a completed check, not a gap.
3. Quote VERBATIM in the finding. An abridged quote flatters the author: the
   clause an ellipsis swallows is disproportionately the one under dispute.
4. Verify every citation a directive makes, against both the code and the
   reference, at the precision the directive claims (AD-5).

**A grep is not the audit, and cannot be.** AD-4 has no keyword -- its defect is
in what the user ends up believing -- and AD-3's carve-out inverts the meaning of
its own search terms. Automate what automates (see below) and read the rest.

## Mechanical enforcement

**Not built as of 2026-08-11.** The audit above is judgment-only today; this
section is the design, not a description of something running.

The highest-signal subset is greppable: the literal phrases under AD-1 and AD-2
that are hits on sight (`do not report ... to the user`, `fleet policy`,
`pre-authorized` with no adjacent path). A pre-commit check over those would have
caught three of the five sites remediated on 2026-08-11 at authoring time.

This is deliberately narrower than the criteria. The razor's own experience is
the reason: its OP-1 is only partly reachable by an automated lane, and every
non-markdown artifact is invisible to all of them. The worst finding in the
2026-08-11 sweep was in a `.sh` file, which no markdown lane reads. A check that
covered only what the lanes see would have reported clean.

Per this repo's check convention the check judges the git INDEX and returns
success when the commit stages none of its inputs (`scripts/_gitindex.py`,
`classify_scope`).
