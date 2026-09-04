# Human HTML presentation reference

Read this reference before you generate or regenerate a `human.html` page.
It governs presentation decisions inside the human HTML contract.
The contract in `standards/human-html-standards.md` wins on divergence.

## Source and license notice

This file adapts and narrows the `frontend-design` skill from
[`anthropics/skills`](https://github.com/anthropics/skills/tree/main/skills/frontend-design).

Copyright 2025 Anthropic, PBC. Licensed under the Apache License, Version 2.0.
The upstream `LICENSE.txt` contains the complete license terms.

This file is a modified work. The modifications restrict the source guidance
to static repository orientation pages and preserve the human HTML contract.
They remove choices that SA-1 already owns and omit runtime UI guidance.

The review rules also adapt applicable parts of Vercel Labs'
[`web-design-guidelines`](https://github.com/vercel-labs/agent-skills/tree/main/skills/web-design-guidelines)
and its vendored Web Interface Guidelines.

Copyright (c) 2025 Vercel Labs. The source material uses the MIT License.

No runtime fetch is part of this reference. The rules below are the vendored
rules for this artifact.

## Scope

The page is an orientation surface beside repository files. It helps a
returning owner recover context and helps a newcomer follow the same route.
It is not a landing page, a dashboard, or a file inventory.

The analysis report controls the admitted claims. This reference controls how
the page presents those claims. Do not add a claim to improve the composition.

### The generator decides

Decide these presentation axes from the admitted units:

- The information hierarchy inside the generated body.
- The order and names of sections.
- The opening orientation statement.
- Which facts need paragraphs, lists, a definition list, or a table.
- Whether one fact earns the existing `.hh-panel` emphasis treatment.
- Which literal commands, paths, identifiers, or excerpts need code markup.
- Whether a complex relationship needs a static inline diagram.

Make each choice specific to the directory. Structure carries meaning. A
border, label, number, table, or panel must identify a real distinction.

### The generator does not decide

SA-1 owns these axes. Leave them unchanged:

- The palette and every concrete color.
- The body and monospace font stacks.
- The type sizes and dark-only theme.
- The page width. The page always fills its viewport.
- The spacing tokens and shared component styling.
- The announce script and all other script policy.
- External assets, remote fonts, stylesheets, or runtime dependencies.

Do not add a `max-width` or prose measure to `main`, sections, paragraphs, or
another body wrapper. Do not narrow the page to imitate an article column.
Use the full viewport and the section rhythm that SA-1 supplies.

## Presentation method

### 1. Name the page's job

Read the analysis report and the record instructions. State the directory's
identity in one sentence. Identify the first fact that restores useful context.

The title must orient. Do not use a generic title such as "Overview" or
"Welcome" when the record supplies a precise identity.

### 2. Plan the reading order

Put the answer before its support. Order sections by the reader's recovery
path, not by the repository's directory order.

A useful default order is:

1. What the directory is for.
2. Why it has this shape.
3. What can cause harm or confusion.
4. Where the reader goes next.

This order is not a template. Omit a section with no admitted unit. Rename or
reorder sections when the evidence gives the reader a better route.

### 3. Build one heading hierarchy

Use exactly one `h1` for the page identity. Use `h2` for top-level sections.
Use `h3` only when one section contains distinct subordinate topics.

Headings describe their section. They do not number a set unless the content
is a real sequence. They do not carry ornamental labels or all-caps eyebrows.

Start the generated body with the `h1` or with a header that contains it.
Follow it with a short orientation statement when that statement adds context.

### 4. Mark up the navigation spine

PC-2 computes destinations. Present those destinations as one semantic list.
Each list item contains one link with two text levels:

```html
<nav data-human-html-chrome="nav" aria-label="Orientation pages">
  <ul>
    <li>
      <a href="engine/human.html">
        <span class="hh-nav-label">engine</span>
        <span class="hh-nav-identity">The shared engine and test surface.</span>
      </a>
    </li>
  </ul>
</nav>
```

Use `Repository root` for the root label. For another directory, use its final
path segment. Use the target record's complete identity as secondary text.

Keep every destination visible at rest. Do not hide navigation in a menu,
disclosure, tab set, tooltip, hover state, or script-controlled component.

### 5. Present claims and evidence

Write one idea per paragraph. Keep the claim first. Put its significance and
evidence in a definition list when both accompany the claim:

```html
<dl>
  <dt>Why it matters</dt>
  <dd>A handler that computes game state crosses the layer boundary.</dd>
  <dt>Evidence</dt>
  <dd><a href="router.c">router.c</a></dd>
</dl>
```

Use `dt` for the stable relationship name. Use `dd` for its value. Do not fake
this relationship with bold labels at the start of ordinary paragraphs.

Evidence links name the repository item that they open. Avoid labels such as
"here", "more", or "this file" when a path or document title is available.

### 6. Choose lists and tables by information shape

Use an unordered list for a set of routes, hazards, or related items. Use an
ordered list only when order changes the meaning or outcome.

Use a table only for repeated rows with two or more attributes that readers
must compare across rows. A file-to-purpose mapping can justify a table. A set
of links, steps, or one-attribute facts does not.

Give every table a header row. Keep comparable values in the same column. Do
not use a table to create columns or to make prose look denser.

### 7. Use code markup literally

Use inline `code` for commands, paths, identifiers, keys, and exact values.
Use `pre` with `code` for a literal block that readers can copy or inspect.

Do not put ordinary labels, headings, evidence prose, or decorative metadata
in monospace. Do not shrink code to fit the viewport. SA-1 scrolls long blocks.

### 8. Spend emphasis once

Use `.hh-panel` only when one fact must stand apart from the normal reading
flow. A safety boundary or a decisive invariant can earn it.

Do not wrap every section in a panel. Repeated rounded cards erase hierarchy.
Use whitespace and headings for normal separation.

## What the page must not resemble

Reject these generated-page defaults unless the evidence requires the form:

- A hero with a large metric, small label, and decorative accent.
- A dashboard made from equal cards for unequal information.
- A newspaper layout with dense columns and ornamental rules.
- A numbered procession for content that is not sequential.
- An all-caps eyebrow above each heading.
- Labels that repeat the section heading without adding meaning.
- A wall of centered prose.
- Navigation written as adjacent identity sentences.
- Repeated `h1` elements used as section separators.
- Link affordance that appears only during hover.
- "Why it matters" and "Evidence" as bold paragraph prefixes.

The page can be quiet. Distinctive presentation comes from an evidence-shaped
hierarchy, not from decoration or a new visual theme.

## Accessibility and resilience

Use semantic HTML before ARIA. Use `a` for navigation. Keep headings in order.
Give a meaningful image alternative if the admitted content requires an image.
Use an empty alternative only for a decorative image.

Every link remains identifiable without color or hover. Keep the SA-1 visible
focus outline. Do not disable browser zoom. Do not cover a focused link with
sticky or overlapping content.

Let headings and labels wrap. Let long code and tables scroll inside their own
box. The page itself must not grow wider because one token cannot wrap.

The output remains useful with scripts unavailable. The announce snippet can
report page identity to a parent, but it does not control the presentation.

## Before you accept the page

Review the real generated file, not a detached body fragment.

### Wide standalone view

- Open the file as a standalone page at a wide viewport.
- Confirm that the page fills the viewport and has no prose-width cap.
- Confirm that one `h1` names the page and `h2` headings expose its route.
- Scan only headings, nav labels, and first sentences. The purpose must survive.
- Confirm that navigation reads as labels with secondary identity text.
- Confirm that evidence uses definition lists, not bold-label paragraphs.
- Confirm that every visible link is recognizable without hover.
- Confirm that tables compare attributes rather than arrange content.
- Confirm that decoration does not compete with the orientation facts.

### Narrow framed view

- Open the page in a narrow frame that represents the host viewer.
- Confirm that navigation becomes a legible single-column list.
- Confirm that headings, labels, and evidence values wrap without clipping.
- Confirm that code blocks and tables scroll inside their bounds.
- Traverse every link with the keyboard and confirm the focus outline remains.
- Confirm that no horizontal page scrollbar appears from body content.
- Confirm that the same hierarchy and section order remain understandable.

Accept the page only when both views pass and CK-1 reports no `FAIL`.
