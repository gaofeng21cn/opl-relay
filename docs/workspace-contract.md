# OPL Relay Runtime And Workspace Contract

Owner: `opl-relay`
Purpose: `runtime_workspace_contract`
State: `active_contract`
Machine boundary: Human-readable runtime and workspace invariants. Current
source and fresh `opl-relay --json doctor` readback own the effective selector,
resolved paths, and runtime state.

## Invariants

1. The installed plugin or Package contains code and capability declarations
   only.
2. The data root is user-owned and survives reinstall, upgrade, cache removal,
   and checkout deletion.
3. The single Profile Workspace is the human context and owns the module data
   subtree; Relay does not switch to an independent workspace or data root.
4. Credentials remain in the platform credential store, never in either root.
5. Codex App, OPL App, and OPL Persona share configured user-owned runtime
   roots without moving those roots into an installed Package or plugin cache.

## Resolution

`OPL_PROFILE_WORKSPACE` selects the only runtime root. Its default is
`~/OPL/profiles/<user>`. Relay's data directory is always
`<profile>/data/relay`.

The active path is visible in `opl-relay --json doctor`. OPL App should pass
`OPL_PROFILE_WORKSPACE` when launching Relay rather than relying on its own
installation directory or process working directory.

## Ownership Matrix

| Artifact | Authority | Git allowed |
| --- | --- | --- |
| Plugin manifest and Skills | Plugin source | Yes |
| Package contribution manifest | OPL Package source | Yes |
| Accounts and credential references | Data root | No |
| Mail, mailbox-operation receipts, draft, and memory SQLite | Data root | No |
| Sync cursors and indexes | Data root | No |
| Keychain secrets | Keychain | No |
| Profile and policies | Workspace | Only in a deliberately private repo |
| Context, templates, notes | Workspace | Only in a deliberately private repo |
| Exports | Workspace | User decides |

Persona proposal bundles are structured, evidence-linked review artifacts. They
may be stored under the Persona data root, but they are not accepted as a
website publication, vault write, or mail send until the target adapter
re-reads the exact proposal and the user approves it.

## Host Contract

A host integrates Relay by providing:

- the shared Profile Workspace locator;
- process or service lifecycle;
- permissions and explicit write approvals;
- view contributions and read-model rendering;
- secure credential-store availability.

Relay continues to own mail identities, memory evidence rules, draft
fingerprints, controlled mailbox-operation receipts, and send receipts. Persona
owns cross-domain provenance and proposal state. A host may visualize these
contracts but must not recreate them as a second source of truth. Mailbox
movement remains limited to exact references, existing Archive/Trash folders,
fresh raw-message and `Message-ID` verification, explicit `--apply`, and
post-operation source/target readback.
