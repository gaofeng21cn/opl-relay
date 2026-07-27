# Local Data And Workspace

OPL Relay separates installed capability, durable user data, and task context.
Do not place new private state under a Git checkout or plugin directory.

## Data Root

`OPL_RELAY_HOME` selects the durable data root:

```text
~/.opl-relay/
  accounts.toml
  mail.sqlite
  drafts.sqlite
  memory.sqlite
  sources.toml
  sync-state/
  workspaces/
```

For an existing installation, `CODEX_MAIL_HOME` remains supported. When neither
variable is set, Relay uses an existing `~/.codex-mail-workbench` runtime before
starting a new `~/.opl-relay` runtime. This preserves existing accounts, mail,
memory, drafts, and cursors without an implicit data move.

Credentials remain in macOS Keychain service `codex-mail-workbench`. The service
name is intentionally unchanged so an upgrade does not invalidate credentials.

## Workspace

`OPL_RELAY_WORKSPACE` selects the active human-editable workspace. Its default is
`~/.opl-relay/workspaces/default`.

```text
<workspace>/
  .opl-relay-workspace.json
  AGENTS.md
  profile.md
  skills/
  policies/
  context/
  templates/
  notes/
  exports/
```

Initialize or inspect it:

```bash
opl-relay --json workspace init
opl-relay --json workspace inspect
```

Select a project workspace without moving the mail database:

```bash
export OPL_RELAY_WORKSPACE=/path/to/project/.opl-relay
opl-relay --json doctor
```

## Moving A Legacy Overlay

Preview first, then copy the supported human-editable overlay:

```bash
opl-relay --workspace ~/.opl-relay/workspaces/default \
  --json workspace migrate --from /path/to/old/checkout/local
opl-relay --workspace ~/.opl-relay/workspaces/default \
  --json workspace migrate --from /path/to/old/checkout/local --apply
```

The migration copies `AGENTS.md`, `profile.md`, and the `skills`, `policies`,
`context`, `templates`, and `notes` trees. It reports and skips accounts,
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
Git. The ignored `local/` path remains only as a compatibility guard; it is not
the recommended runtime layout.
