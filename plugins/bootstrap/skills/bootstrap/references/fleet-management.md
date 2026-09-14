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
| `.claude/settings.json` | One `SessionStart` entry that runs the script. |

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

The hook needs only bash and the `claude` CLI, because it runs before
bootstrap has provisioned anything. It always exits 0, so a failed remediation
reports a message and never blocks the session. The marketplace, plugin, and
install scope are fixed in the script; the minimum version is the only value
that changes.

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
