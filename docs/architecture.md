# OPL Relay Architecture

Owner: `opl-relay`
Purpose: `relay_implementation_architecture`
State: `active_current`
Machine boundary: This document explains Relay-owned source and data boundaries. Package publication, configured-carrier installation, Framework aggregation, App rendering, and live runtime state remain authoritative only in their owning descriptors, repositories, carrier readback, and runtime output.

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
3. The owner Package descriptor exposes the same capability and its current
   role-neutral `app_contributions`; installed availability still requires
   fresh native-carrier readback.
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
- Triage evidence: a facts-only v2 envelope exposes mail headers, recipient
  routing, and reference-set provenance. Persona owns private Markdown reading,
  content digests, and all triage judgments.
- CLI: `opl-relay` is the only command entry. The retained
  `codex-mail-workbench` compatibility Skill and Keychain service name do not
  create a `codex-mail` command alias.

## Runtime Ownership

Relay separates replaceable installation bytes from one user-owned Profile
Workspace:

| Surface | Owner | Contents |
| --- | --- | --- |
| Package owner descriptor | Relay | Package identity, capabilities, content lock, role-neutral App contributions, and publication references |
| Installation | Configured native carrier | Replaceable code, manifest, Skills, lifecycle actions, and physical installed-state readback |
| Profile Workspace | User | Profile, policies, context, templates, exports, and Relay state under `data/relay` |

The installation root is replaceable. Removing, repairing, or upgrading a
carrier installation must not delete the user-owned `OPL_PROFILE_WORKSPACE`.
Framework may discover the installed descriptor, delegate an action, and
aggregate the carrier result, but it does not become a second physical
installation owner. Codex App and OPL App must use the same configured Relay
state under that workspace when they act for the same user.

See [Workspace Contract](workspace-contract.md) for the single-root rule and
[Product Architecture](product-architecture.md) for the broader OPL
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
are private local writes. Apple Mail drafts remain review-gated, and sending
requires the current post-review fingerprint. A separately contracted
`mailbox move` may move exact, freshly verified references to an existing
Archive or Trash folder under explicit `--apply`; permanent delete and mark
remain unavailable.
