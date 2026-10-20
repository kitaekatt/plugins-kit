# Recap trigger rows

A bare invocation reads `state get`: presence afk selects a `return` row
(`greeted` picks which), presence present selects the first row.

| Trigger | Window | Phase 1 | Ends with |
|---|---|---|---|
| bare, user present | recent turns, widened if thin | "What got done", "Decisions I need from you" (text only) | nothing (not an away row) |
| `return`, `greeted` false | since `afk_since` | finished, decided for you, waiting on you, and still running when owned work runs | `presence.py recap-done return <pid>` |
| `return`, `greeted` true | -- | none; the standing recap is not repeated | `presence.py recap-done return <pid>` |
| `afk-ending` (nothing owned) | since `afk_since` | finished, decided for you, waiting on you; read cold | `presence.py recap-done afk-ending <pid>` |
| `autonomous-done` | since `goal_since` | outcome against `goal`, each load-bearing decision, waiting on you | `presence.py recap-done autonomous-done <pid>` |

One recap per ending: `autonomous-done` also closes the afk ending. A recap
without its `recap-done` call satisfies nothing; the call, not the prose, is
what the Stop hook checks.
