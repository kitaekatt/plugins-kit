# The render lane

The RENDER procedure: print the text of named skills, and the reference files
they declare, as one block a caller can place in another model's or agent's
prompt, or print the report of what went into that block. Its subject is a
skill. It runs one command, `material`, reads files, and writes nothing.

**It is not an audit.** Render reads a skill to print it. It does not judge the
skill, so it renders no compliance verdict. A request to check, validate or
audit a skill is `audit skill`, which loads `references/lanes/audit-lane.md`.
Only the command's own four outcomes are verdicts here (step 3).

## Step 1 -- Build the command line from the request

Each skill named is one `--skill <path>`, in the order the user gave them. The
path is a skill directory or the `SKILL.md` inside it. A relative path resolves
against the directory the command runs in, and nothing searches for a skill by
name: when the user names a skill without a path, find its directory first and do
not guess. Three flags change the `--skill` given just before them, and each is
a usage error before the first `--skill`:

| Flag | Effect on the preceding skill |
| --- | --- |
| `--catalog` | renders name and description only |
| `--no-declared` | does not load the reference files the skill declares in its own `SKILL.md` |
| `--resource <rel>` | loads one further file, given relative to the skill's directory; repeatable |

`--budget <n>` is required, an integer above zero, and applies to the whole
block. The number is the user's. When the request names none, ask for one and do
not choose one. The command has no default budget, and the lane must not supply
one.

`--json` prints the report instead of the block: the digest, the token
estimates, the files each skill declares, each file rendered with its hash, and
what was suppressed as a duplicate. The report does not carry the text. Use it
when the user asks what went in, or how large it is, and the block when the user
wants the text itself.

## Step 2 -- Run it

```
"${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${CLAUDE_PLUGIN_ROOT}/scripts/skills_kit_tool.py" material --skill <path> [--catalog] [--no-declared] [--resource <rel>]... [--skill <path> ...] --budget <n> [--json]
```

The launcher runs from any directory. On success stdout holds the block (or the
report) and nothing else; on any failure stdout is empty and stderr holds the
diagnosis, marked with a `material:` prefix.

## Step 3 -- Report the outcome

Report what the command did, for every exit code. The verdict is the outcome of
the run, and the exit code and the stderr message are shown as printed.

| Exit | Verdict | What happened | What to do next |
| --- | --- | --- | --- |
| 0 | RENDERED | The block or the report was printed. | Show it as printed, without editing it. The block is UTF-8 with LF line ends. |
| 1 | REFUSED | The input was refused: a skill with no readable frontmatter, invalid YAML or no `name`; a named or declared file that is missing or unsafe; two different skills with one name. | Show the message, which names the skill and the file. For a frontmatter failure, offer `audit skill` on that skill. For a declared file that cannot load, the message names the remedy: `--no-declared` plus `--resource` for the files wanted. |
| 3 | UNAVAILABLE | The skill-material library cannot run in this interpreter: bootstrap is not installed, is older than the skills-kit floor, or has no PyYAML to read a skill. Nothing was printed. | Show the message, which names the state and the remedy. Installing or updating the plugin is the user's action. |
| 4 | OVER-BUDGET | The block would not fit the budget. Nothing was printed. | Show the message, which gives the estimate against the budget and, for a block that was rendered, itemizes it. The fixes are the user's: `--catalog` on a skill, `--no-declared` with a shorter `--resource` list, or a larger budget. Offer them and run again only with the choice the user makes. |

Exit 2 is outside the verdicts. It means the command line was malformed, so
nothing about the skill was decided and no verdict is reported. Show
argparse's message, correct the command line built in step 1 (or ask the user
for the missing part, such as the budget), and run again.

## Gotchas

- The estimate is the character count divided by four, rounded up, over the
  whole block. It is a runaway guard, not a count from any model's tokenizer.
- The command reads the files as they are when it runs. The report's digest names
  that one snapshot.
- A skill that declares many reference files can be large. Over budget is the
  designed answer, and it is not retried with a smaller selection unless the
  user chooses one.
