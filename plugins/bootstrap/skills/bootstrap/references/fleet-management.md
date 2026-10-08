# Fleet management: getting bootstrap onto every machine

How a bootstrap administrator makes sure everyone who works in a project gets
the bootstrap plugin, so bootstrap can then install and provision the project's
other plugins.

Audience: the administrator who owns a project's `.claude/` configuration, and
anyone reading an `ensure-bootstrap` hook in a project's `settings.json`.

## Why a hook is needed

A project's `.claude/settings.json` can list plugins under `enabledPlugins` and
their marketplaces under `extraKnownMarketplaces`. `enabledPlugins` records
whether an installed plugin is on or off; it does not install anything. Claude
Code stopped installing plugins enabled only by a project file:

| Claude Code | Changelog entry |
|---|---|
| 2.1.144 | Plugins enabled only by a project's `.claude/settings.json` show a `claude plugin install` hint instead of installing. |
| 2.1.195 | Such plugins require explicit install consent on every loader path. |

Marketplaces declared in a committed project `settings.json` also wait for
workspace trust. On a fresh clone, bootstrap therefore never arrives, and
nothing that depends on bootstrap does either. Bootstrap installs every other
declared plugin (see manifest-reference.md, `plugins[]`), so the only plugin a
project must deliver itself is bootstrap.

## The ensure-bootstrap hook

`bootstrap install-hook` writes two files into the project in the working
directory:

| File | Content |
|---|---|
| `.claude/hooks/ensure-bootstrap.sh` | The hook script, with the minimum bootstrap version written into it. |
| `.claude/settings.json` | One `SessionStart` entry that runs the script, with `async` and `asyncRewake` set. |

The entry is asynchronous, so a session never waits for the hook. When the
hook has something to report, it writes the message to stderr and exits 2.
`asyncRewake` then delivers that message to Claude, which passes it on to the
user. The healthy path exits 0 with no output.

At every session start the hook does this:

1. **Opt-out check.** If the project's `.claude/bootstrap.json` does not exist,
   do nothing. The settings entry also checks that the script exists, so a
   deleted script produces no hook error.
2. **Detect.** Read `claude plugin list --json` and find the
   `bootstrap@plugins-kit` record that applies to this project: a `local`
   record for this project, else a `project` record for this project, else a
   `user` record. A record's project path must match this project exactly
   apart from slash direction and drive-letter case, because Claude Code
   treats paths that differ only in case as different projects. If its
   version is at or above the minimum, do nothing.
3. **Remediate.**
   - If the `plugins-kit` marketplace is missing, add it from
     `https://github.com/kitaekatt/plugins-kit.git`.
   - Update the marketplace.
   - If bootstrap is not installed, run
     `claude plugin install bootstrap@plugins-kit --scope user`.
   - If bootstrap is below the minimum, run
     `claude plugin update bootstrap@plugins-kit --scope <scope of that record>`.
   - Tell the user the result in one message. After a successful install or
     update, the user restarts Claude Code to load bootstrap.
4. **Repair the CLI, only on failure.** If `claude` is not on PATH, or any
   `claude` step above fails, reinstall the CLI with Anthropic's native
   installer (`install.ps1` under Git Bash on Windows, `install.sh`
   elsewhere). Then run steps 2 and 3 once more.
   - The hook replaces only a missing CLI or the native install in
     `~/.local/bin`. A CLI that another tool installed (a package manager,
     for example) is left alone, and the report says to update it with that
     tool.
   - The hook records the CLI version it installed in
     `~/.claude/plugins/data/plugins-kit/bootstrap/ensure-bootstrap-cli-repair`.
     If that same version fails again in a later session, the CLI is not the
     cause, so the hook reports the failure without downloading the CLI
     again.
   - The hook puts `~/.local/bin` first on its own PATH, so the copy it
     installed is the one it runs.

5. **Repair the marketplace, only on failure.** If `claude plugin marketplace
   update plugins-kit` fails and `claude plugin marketplace list --json` shows
   the `plugins-kit` entry with an empty `installLocation` (a corrupted entry in
   `~/.claude/plugins/known_marketplaces.json`), the hook runs
   `claude plugin marketplace remove plugins-kit`, adds the marketplace again
   from the source above, and updates it once more.
   - Removing a marketplace also uninstalls every plugin installed from it, at
     every scope. The hook therefore reads the install state again and
     installs bootstrap with `--scope user` when its record is gone. The
     report says the marketplace entry was repaired and its plugins
     uninstalled; bootstrap reinstalls the project's plugins after the
     restart, so the developer reinstalls nothing by hand.
   - The removal also rewrites the project's `.claude/settings.json` and
     `.claude/settings.local.json` (it drops the marketplace's
     `enabledPlugins` entries), and a CLI skips a read-only
     `settings.json`. The hook saves both files before the removal and puts
     back their exact content and read-only state afterwards, on every path
     out of the repair, so a tracked file is never left modified. The user
     settings file is left as the CLI writes it.
   - The hook detects the state, not the error wording. It does not remove the
     marketplace when the update fails for any other reason, and it does not
     repair a different marketplace.

Step 4 exists because an outdated CLI can crash on
`claude plugin marketplace update` (observed with 2.1.56:
`panic: index out of bounds`). A broken CLI blocks every repair that goes
through it.

A record whose `installPath` no longer exists needs no special handling. A
working CLI's `claude plugin update` reinstalls the missing cache directory
and corrects the record.

The hook needs only bash and the `claude` CLI, plus `powershell` or `curl` for
the step 4 installer, because it runs before bootstrap has provisioned
anything. The marketplace, plugin, and install scope are fixed in the script;
the minimum version is the only value that changes.

## Install or refresh the hook in a project

1. Update your own bootstrap to the version you want as the minimum. The
   command uses the version of the bootstrap plugin that runs it.
2. For a Perforce project, open the two files for edit or add first.
   `bootstrap install-hook` refuses a read-only file and writes nothing.
3. From the project root, run:

   ```bash
   bootstrap install-hook
   ```

   It prints the minimum version and which files it wrote. It refuses a
   directory without `.claude/bootstrap.json`, with exit code 2.
4. Commit or submit the files. The command does not touch source control.

Run the command again after a bootstrap release to raise the minimum. A re-run
replaces the script and the settings entry; it never adds a second entry, and
it keeps every other setting and hook.

## Opting out

A user opts out of bootstrap for a project by deleting their local copy of the
project's `.claude/bootstrap.json`. The hook then does nothing in that
project. The deletion stays local if the user does not commit or submit it.

## Related

- manifest-reference.md -- the `plugins[]` and `marketplaces[]` entries
  bootstrap uses to install the rest of the project's plugins.
- bootstrap-cli.md -- the other `bootstrap` command verbs.
- plugin-reload-lifecycle.md -- why a newly installed plugin loads at the next
  session.
