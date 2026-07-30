# Domain layering

A domain-skill that owns a knowledge area large enough to decompose into 2+ semi-independent sub-areas needs a layering surface: the user-facing mechanics that route a request into the right sub-area without forcing the agent to re-orient on every invocation.

Use when a domain-skill has multiple sub-domains the user navigates between -- e.g. an engine-automation skill with sub-areas for asset inspection, level authoring, and PIE control; a project-management skill with sub-areas for tickets, milestones, and approvals. Do not use for a single-area domain-skill; the layering machinery has cost (a greeting menu, dispatch logic, registration index) that is wasted when only one area exists.

## Surface mechanics

A domain-skill with sub-domains exposes four user-facing behaviors. Each addresses a distinct invocation shape.

### 1. Bare-invocation greeting menu

When the user invokes the skill with no follow-up request (e.g. `/some-domain` alone), greet with a sub-domain menu rather than dumping content. The menu lets the user pick the sub-area that matches their intent.

**Format:**

```
How can I help you with <domain>?
 - <sub-domain-A description> (`/<skill> <keyword>`)
 - <sub-domain-B description> (`/<skill> <keyword>`)
 - ...

Or can I help you with something else?
```

Show every registered sub-domain. Do NOT dump operations tables, references, or other detail at this point -- the menu is the entire response, and the user picks next.

### 2. Argument dispatch

When the user invokes the skill with a domain argument (e.g. `/some-domain assets` or `/some-domain pie`), skip the greeting and jump directly into that sub-domain's capability surface. Match the argument against the sub-domain's `keyword_cues` cluster, name, or description.

The argument-dispatch path produces a sub-domain capabilities table immediately, as if the user had selected from the greeting menu.

### 3. Domain overview request detection

Before responding to any sub-domain request, ask: *is the user asking what this sub-domain can do, or asking to do something specific?* The two invocation shapes warrant different responses.

A **domain overview request** is a capability question -- the user wants to see what's available, not execute anything yet. Examples:

- "what <area> operations can you perform"
- "what can you do with <area>"
- "list <area> commands"
- "help me with <area>" (no specific task named)
- "<area> options"
- any phrasing that asks what's available rather than naming an action

When the request is a domain overview request, respond with **only** the matching sub-domain's capabilities table (name + description per capability). No extra commentary. No CLI examples. No design principles. No follow-up question like "want me to run one?". The table is the entire response, and the user picks the capability they want.

For requests that name a specific action ("run the inspector", "rebuild the index"), skip the overview table and execute the capability using normal judgment.

### 4. Sub-domain registration

Sub-domains are declared **once**, in the domain_skill contract's `index:` block -- the same declaration the schema validator checks. There is no separate layering index; the routing behaviors (greeting menu, argument dispatch, overview detection) consume the `index:` block directly. One declaration satisfies both the schema and the layering surface.

- **Reference-backed sub-domain** -- one `index.references[]` entry. The schema's fields carry the routing data: `id` is the canonical sub-domain identifier, `summary` is the one-sentence scope (the greeting-menu line), `keywords` is the routing cue cluster (argument-dispatch matches against it), `path` is the sub-domain's deeper documentation.
- **Member-skill-backed sub-domain** -- one `index.members[]` entry (`name` / `type` / `ref` / `keywords`); see "Two ways to back a sub-domain" below.

Example (reference-backed):

```yaml
domain_skill:
  # ... identity / companions / scope / orientation ...
  index:
    references:
      - id: subdomain-a
        path: references/subdomain-a.md
        keywords: [keyword-a, keyword-a-alt, alt-cue]
        summary: First sub-area description.
      - id: subdomain-b
        path: references/subdomain-b.md
        keywords: [keyword-b, keyword-b-alt]
        summary: Second sub-area description.
```

The `index:` block is the single source of truth for the greeting menu and for argument-dispatch matching. Audit tooling consumes the same block: the schema validator enforces the record shape, and the reference-resolution check verifies each declared `path` is reachable on disk.

**Do not declare a parallel `sub_domains:` block.** Earlier versions of this pattern used a bespoke `sub_domains:` index (`name` / `description` / `keyword_cues` / `reference`) alongside the schema's `index.references` -- two near-synonymous declarations over the same files, which drift. That shape is retired for domain-skills on the YAML contract. (For generic md documents NOT on the domain_skill contract, the portable `sub_areas:` unit -- same record shape -- remains the right declaration; see `authoring-patterns/area-ownership.md` (in md-domain).)

### Two ways to back a sub-domain: reference sub-area vs member skill

A sub-domain can be backed two ways. The routing behavior (greeting, argument-dispatch, overview detection) is identical; only what the sub-domain *points at* differs.

- **Reference sub-area.** The sub-domain is a `references/*.md` doc inside this domain. Declared with an `index.references[]` entry (see the registration section above). Use when the sub-area is knowledge owned by this domain, not a standalone skill -- dialog-domain's `first_pass` / `dialog_testing` are reference sub-areas.
- **Member skill.** The sub-domain is a separate flat skill, pointed at by the domain_skill's `index.members[]` (each entry carries `name` / `type` / `ref` / `keywords`). Use when the sub-domain is a substantial standalone skill that already exists -- or is itself a domain-skill. The member `type` may be `domain-skill`: a **broader union domain** routes to sub-domain members this way without nesting (see framework.md "Broader union domains over sub-domains"). The parent stays a thin router; argument-dispatch loads exactly one member.

Pick member-skill backing when the sub-domain has its own lifecycle, scripts, or reference graph (it earns a directory and a SKILL.md); pick reference-sub-area backing when it is a body of knowledge the parent owns. A broader union domain typically uses `index.members[]` because its sub-domains are pre-existing domains it is putting a roof over.

**Audit note for member-skill sub-domains:** the `check_domain_members_resolve` corpus check (skills_kit_lib) asserts every `index.members[].ref` resolves to a real skill on disk, the same way the reference-sub-area path is audited for reachable reference docs.

## Sub-agent dispatch convention

When a domain-skill ships alongside a paired sub-agent named `<skill-name>-a`, the agent is configured to invoke the skill on session start so the agent always has the domain's vocabulary and reference index available. This is the agent-bundled attribute on the skill.

The invariant: a sub-agent paired with a domain-skill never loads the skill manually mid-session. The parent skill loads the agent; the agent always invokes the skill on entry. This prevents "the sub-agent didn't know about X" failure modes.

The convention extends to dispatch: when a parent task calls for a sub-agent, the parent invokes `<skill-name>-a` rather than spawning a generic agent and instructing it to invoke `/<skill-name>` manually. Generic agents that load the skill manually defeat the agent-bundled invariant.

**Audit gate:** if a domain-skill has a paired `<skill-name>-a` agent, the SKILL.md must declare the dispatch convention explicitly so that anyone composing with the agent (or any future contributor adding new agents) knows the parent-spawns-bundled rule rather than the parent-spawns-generic-and-loads rule.

## Worked example: hypothetical project-management domain

A `/project-mgmt` domain-skill with three sub-domains -- tickets, milestones, approvals -- declares its layering as follows:

**SKILL.md sub-domain index (the domain_skill contract's own `index:` block):**

```yaml
domain_skill:
  # ... identity / companions / scope / orientation ...
  index:
    references:
      - id: tickets
        path: references/tickets.md
        keywords: [ticket, issue, bug report, task, triage]
        summary: ticket creation, triage, status transitions
      - id: milestones
        path: references/milestones.md
        keywords: [milestone, sprint, release, deadline, schedule]
        summary: milestone planning, dependency tracking, due-date management
      - id: approvals
        path: references/approvals.md
        keywords: [approval, sign-off, review request, escalate]
        summary: approval routing, sign-off tracking, escalation
```

**Bare invocation `/project-mgmt`:**

```
How can I help you with project management?
 - ticket creation, triage, status transitions (`/project-mgmt tickets`)
 - milestone planning, dependency tracking, due-date management (`/project-mgmt milestones`)
 - approval routing, sign-off tracking, escalation (`/project-mgmt approvals`)

Or can I help you with something else?
```

**Argument dispatch `/project-mgmt tickets`:**

(skips greeting; shows tickets capabilities table)

**Domain overview request "what can I do with milestones":**

(matches the `milestones` entry's keywords; responds with milestones capabilities table only, no extra commentary)

**Specific action "create a ticket for the auth bug":**

(matches the `ticket` keyword; skips overview; routes to ticket-creation capability and executes)

## Worked example: hypothetical inventory domain

An `/inventory` domain-skill with two sub-domains -- character-inventory and world-pickup -- with a paired `inventory-a` sub-agent:

**SKILL.md** declares both the sub-domain index and the dispatch convention:

```
For any inventory-domain work that warrants a subagent, spawn the
`inventory-a` subagent. It always invokes /inventory at the start, so
it already has this skill loaded. Do not spawn a generic subagent and
tell it to invoke /inventory manually -- use inventory-a instead.
```

The dispatch paragraph is required because there is a paired agent. Without it, a future contributor composing with sub-agents would not know to use `inventory-a` and would default to a generic agent + manual skill load.

## Audit hooks

A domain-skill claiming the layering pattern must satisfy these auditable conditions:

- Sub-domain declarations live in the domain_skill contract's `index:` block (references and/or members) -- ONE declaration serving both the schema validator and the routing surface. A parallel `sub_domains:` block duplicating the index is itself a finding (two declarations over the same files drift).
- Each declared sub-domain has a reachable reference file (no broken references).
- Bare-invocation greeting documented in SKILL.md.
- Overview-vs-action detection rule documented in SKILL.md.
- If a `<skill-name>-a` agent exists, the dispatch convention is declared explicitly.

Single-area domain-skills do not satisfy this pattern -- and should not. Forcing the layering surface onto a single-area domain adds noise without orientation benefit.
