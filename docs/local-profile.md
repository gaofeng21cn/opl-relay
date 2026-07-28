# Local Data And Workspace

OPL Relay separates installed capability, durable user data, and task context.
Do not place new private state under a Git checkout or plugin directory.

## Data Root

The default user-owned Profile Workspace is:

```text
~/OPL/profiles/<user>/
  AGENTS.md
  profile/
  policies/
  context/
  templates/
  exports/
  data/
    relay/
    persona/
```

Relay stores accounts, SQLite databases, sources, and sync state under
`<profile>/data/relay`. `OPL_PROFILE_WORKSPACE` is the only runtime selector;
Relay does not inspect old state directories or alternate environment variables.

Credentials remain in macOS Keychain service `codex-mail-workbench`. The service
name is a local credential contract; it is never exported to Git or the Profile
Workspace.

## Workspace

`OPL_PROFILE_WORKSPACE` selects the active Profile Workspace.

```text
<workspace>/
  .opl-profile-workspace.json
  AGENTS.md
  profile/
  policies/
  context/
  templates/
  exports/
  data/relay/
  data/persona/
```

Initialize or inspect it:

```bash
opl-relay --json workspace init
opl-relay --json workspace inspect
```

Select a Profile Workspace without moving the mail database:

```bash
export OPL_PROFILE_WORKSPACE=/path/to/profile
opl-relay --json doctor
```

## Copying An Explicit Overlay

Preview first, then copy the supported human-editable overlay:

```bash
OPL_PROFILE_WORKSPACE=/path/to/profile \
  opl-relay --json workspace migrate --from /path/to/source-overlay
OPL_PROFILE_WORKSPACE=/path/to/profile \
  opl-relay --json workspace migrate --from /path/to/source-overlay --apply
```

The migration copies `AGENTS.md`, `profile.md`, and the `profile`, `policies`,
`context`, and `templates` trees. It reports and skips accounts,
SQLite databases, `sources.toml`, and sync state. It refuses conflicting target
bytes and verifies every copied file after writing. It does not delete the
source; source cleanup remains an explicit, separately verified operation.

## Obsidian

Configure Obsidian roots in the data root's `sources.toml`. The provider is
read-only and writes only a rebuildable derived index into `memory.sqlite`.
Use narrow include paths:

```bash
opl-relay --json sources list
opl-relay --json sources index
opl-relay --json sources search "<person or project>"
```

## Publication Check

Before committing, verify that accounts, database files, raw messages, private
workspace content, Obsidian paths, credentials, and sync cursors are absent from
Git.
