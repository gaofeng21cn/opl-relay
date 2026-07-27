# OPL Relay Runtime And Workspace Contract

## Invariants

1. The installed plugin or Package contains code and capability declarations
   only.
2. The data root is user-owned and survives reinstall, upgrade, cache removal,
   checkout deletion, and workspace switching.
3. A workspace is explicit, replaceable human context. Switching it does not
   fork or move the mail database.
4. Credentials remain in the platform credential store, never in either root.
5. Codex App and OPL App share the configured Relay authority for one user.

## Resolution

Data root precedence:

1. `OPL_RELAY_HOME`
2. legacy `CODEX_MAIL_HOME`
3. `~/.opl-relay` when it contains Relay runtime state
4. existing `~/.codex-mail-workbench`
5. new `~/.opl-relay`

Workspace precedence:

1. `OPL_RELAY_WORKSPACE`
2. `~/.opl-relay/workspaces/default`

The active paths are visible in `opl-relay --json doctor`. OPL App should pass
both paths explicitly when launching a Relay runtime rather than relying on its
own installation directory or process working directory.

## Ownership Matrix

| Artifact | Authority | Git allowed |
| --- | --- | --- |
| Plugin manifest and Skills | Plugin source | Yes |
| Package contribution manifest | OPL Package source | Yes |
| Accounts and credential references | Data root | No |
| Mail, draft, and memory SQLite | Data root | No |
| Sync cursors and indexes | Data root | No |
| Keychain secrets | Keychain | No |
| Profile and policies | Workspace | Only in a deliberately private repo |
| Context, templates, notes | Workspace | Only in a deliberately private repo |
| Exports | Workspace | User decides |

## Host Contract

A host integrates Relay by providing:

- data-root and workspace locators;
- process or service lifecycle;
- permissions and explicit write approvals;
- view contributions and read-model rendering;
- secure credential-store availability.

Relay continues to own mail identities, memory evidence rules, draft
fingerprints, and send receipts. A host may visualize these contracts but must
not recreate them as a second source of truth.
