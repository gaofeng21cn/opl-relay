# OPL Relay Architecture

For the cross-repository product and authority model, read
`opl-persona/docs/architecture-guidance.md` in the sibling `opl-persona`
repository first. This document is the Relay-specific implementation view.

OPL Relay is the mail capability module in the planned personal digital
delegate stack. It is local-first, evidence-backed, and useful independently of
OPL Persona or OPL App.

## Layers

1. The engine owns IMAP sync, raw EML storage, stable identities, structured
   memory, knowledge indexing, context assembly, and draft approval semantics.
2. The Codex Plugin supplies the `opl-relay` Skill. It is an installable
   capability carrier, not a database owner.
3. OPL Package metadata will expose the same capability to OPL App after the
   platform has a role-neutral `app_contributions` contract.
4. OPL App is the long-term user entry and visual management surface. It calls
   Relay through the same runtime boundary as Codex rather than implementing a
   second mail engine.
5. OPL Persona is the cross-domain orchestration product that may call Relay
   together with other domain modules. It does not absorb Relay's mail truth.

## Engine Modules

- Configuration: account metadata and Keychain credential references.
- Protocol: explicit IMAP synchronization with per-folder cursors.
- Store: local SQLite raw EML store with stable `email-store://` identities.
- Memory: evidence-backed candidate and approved relationship memory.
- Knowledge: read-only Obsidian indexing into a rebuildable local index.
- Context: bounded packages combining approved memory, selected mail evidence,
  and relevant knowledge excerpts.
- Drafts: Apple Mail as the editable review UI plus a local lifecycle ledger and
  exact approval fingerprint. An approved Persona `mail.draft_context`
  proposal may prepare this review draft, but it never authorizes sending.
- CLI: `opl-relay`, with `codex-mail` retained as a compatibility alias.

## Runtime Ownership

Relay resolves three independent roots:

| Surface | Owner | Contents |
| --- | --- | --- |
| Installation | Plugin or Package manager | Code, manifest, Skills |
| Data root | User | Accounts, SQLite stores, sync cursors, derived indexes |
| Workspace | User or current project | Profile, policies, context, templates, exports |

The installation root is replaceable. Deleting or upgrading a plugin must not
delete either user-owned root. Codex App and OPL App must use the same configured
Relay data service when they act for the same user.

See [Workspace Contract](workspace-contract.md) for resolution and compatibility
rules and [Product Architecture](product-architecture.md) for the broader OPL
integration.

## Stable References

Mail and memory use stable references rather than direct SQLite facts:

```text
email-store://<account_id>/<folder_slug>/<uid>/<raw_sha256_prefix>
mail-memory://entity/<uuid>
mail-memory://fact/<uuid>
mail-draft://apple-mail/<account_id>/<apple-mail-uuid>
```

## Safety Boundary

Relay remains read-first. Local memory lifecycle and derived knowledge indexing
are private local writes. The only externally visible write is the Apple Mail
draft flow, and sending requires the current post-review fingerprint. Delete,
archive, move, and mark remain unavailable until separately contracted and
authorized.
