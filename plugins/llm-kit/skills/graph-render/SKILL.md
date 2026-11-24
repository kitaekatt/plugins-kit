---
_schema_version: 1
name: graph-render
author: christina
skill-type: technique-skill
description: Use when the user asks to see, draw, render or browse llm_kit.graph flows as a diagram or HTML page. Do NOT use for running or replaying a graph.
---

# Graph Render

Draws `llm_kit.graph` flows as one self-contained HTML page and opens it in the
user's browser. The page needs no server and makes no network request. It is
one `llm-kit graph render` call plus an open step. Only graph files are
supported.

```yaml
technique_skill:
  _schema_version: "1"
  trigger_model: auto
  identity: Render the user's graphs to an HTML page outside their repository, open it, and report its path.
  scope:
    covers:
      - rendering every graph in the project config as a browsable page
      - rendering one named graph or one graph file
      - opening the written page in the default browser
    excludes:
      - running, replaying or editing a graph
      - writing the page into the user's repository unless the user names that path
  techniques:
    - id: render_graphs
      name: Render graphs to a page and open it
      keywords: [graph, render, html, browser, diagram, flow, graphs.yaml]
      goal: The user sees the graph or graph browser in their browser and knows where the file is.
      steps:
        - n: 1
          action: Pick the mode from the request, one command word, no pipes (see "Running the call").
        - n: 2
          action: Pick the output path (see "Where the page goes").
        - n: 3
          action: Run the render call, then the open call on the path the render call printed.
          on_failure: Exit 1, 2 or 3 prints the reason on stderr; report it as written and stop. On exit 3 offer the user `--no-labels`; never run `llm-kit consent set` yourself.
        - n: 4
          action: Tell the user the printed path.
      gotchas:
        - Without -o the verb writes into the current directory, which dirties the user's repository; always pass -o.
        - The graph browser opens only with no GRAPH; a single graph that runs no other file opens straight on that graph.
        - Labels are on by default, so the first build of a large project can take up to about a minute; progress lines go to stderr and a rebuild from the cache is quick.
```

## Running the call

`llm-kit` below stands for the plugin's launcher shim, whose path Claude Code
writes into this skill when it loads. If the path below still reads
`${CLAUDE_PLUGIN_ROOT}` literally, report that and stop.

All graphs in the project's `.llm-kit/graphs.yaml`, as a graph browser:

```
${CLAUDE_PLUGIN_ROOT}/bin/llm-kit graph render --project <project dir> -o <out>/graphs.html
```

One graph, named in that config or given as a graph file path (several may be
listed, and a path needs no project config):

```
${CLAUDE_PLUGIN_ROOT}/bin/llm-kit graph render <graph name or file> --project <project dir> -o <out>/<name>.html
```

`--project` defaults to the current directory; `--title TEXT` sets the page
title. Exit codes: `0` written (the path is printed), `1` a graph does not load
or the file cannot be written, `2` usage (for example no `.llm-kit/graphs.yaml`
and no graph named), `3` the labels could not be made (no page is written).

## Labels

A model (haiku by default) infers a title and description for each node, and
the page shows them as purple italic text marked inferred. Its Labels button
hides or shows that text. The first build can take up to about a minute (one
call per graph file, progress on stderr); answers are cached outside the
project, so a rebuild makes no calls. `--relabel` ignores the cache and asks
again; `--no-labels` draws the plain picture with no model call.

On exit 3 (consent for the `graph-labels` scope refused, or the model
unavailable), report the message as written. It names the fix, either
`llm-kit consent set ...` or `--no-labels`. Never change consent yourself:
offer the user `--no-labels` and let them choose.

## Where the page goes

Write to the path the user names. Otherwise write to
`<temp dir>/llm-kit-graphs/`, where the temp dir is the session's scratchpad
directory when the system prompt lists one, else `${TMPDIR:-/tmp}`. The
directory is created on demand and nothing is added to the user's repository.

## Opening the page

Open the path the render call printed, in any OS:

```
"${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" -c "import sys, pathlib, webbrowser; webbrowser.open(pathlib.Path(sys.argv[1]).resolve().as_uri())" <printed path>
```

Then print the path in the reply, so the user can reopen it.
