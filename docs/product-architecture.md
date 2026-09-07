# OPL Relay Product Integration

Owner: `opl-relay`
Purpose: `relay_cross_repo_product_boundary`
State: `active_current`

This document owns Relay's handoffs to Persona and App. Relay's internal
implementation belongs to [Architecture](architecture.md), runtime data
invariants to [Workspace Contract](workspace-contract.md), and physical
delivery to [Distribution](distribution.md). Other repositories own their
actual implementation and operational state.

## Product And Owners

Relay is independently usable mail software. Persona combines cross-domain
PI context and produces proposals; it does not own Relay's mail store.
OPL App supplies the chat and visual product. There is no separate Relay UI
repository or Persona desktop product.

| Boundary | Owner |
| --- | --- |
| Stable mail identities, raw evidence, relationship memory, draft fingerprints and send receipts | Relay |
| Cross-domain context, private policy interpretation, provenance and proposals | Persona |
| User knowledge and personal profile values | User-selected Obsidian vault |
| Public content, approved local website changes and publication | `gflab_web` |
| Runtime, Package discovery, carrier delegation and App projection | Framework |
| App product contracts and acceptance | `one-person-lab-app` |
| Stable desktop rendering | `opl-aion-shell` |
| DSH application host, native Codex and delivery composition | `opl-studio` |

The target relationship is described in
[Persona Architecture Guidance](https://github.com/gaofeng21cn/opl-persona/blob/main/docs/architecture-guidance.md).
It does not override the current contracts of App, Framework, a carrier or a
domain provider. A host never absorbs domain identities or private state.

## Persona Handoff

Relay's `triage evidence` returns an evidence-only
`opl-relay-mail-triage-evidence.v2` envelope for one synced
`email-store://` reference. It includes original-message readback, parsed
recipient facts, freshness and policy references. Its `policy_digest` hashes
the ordered reference set; it does not read or hash Persona's private Markdown.

The envelope requires human review and forbids external writes. Relay
`triage validate` validates identity, provenance, reference-set digest and
the read-only boundary. It accepts the bare envelope or the exact successful
JSON wrapper emitted by `triage evidence`:

```bash
opl-relay --json triage evidence 'email-store://...' --policy-ref 'policy://...' \
  | opl-relay --json triage validate --input -
```

Persona owns interpretation, policy-content digests and Inbox staging.
Relay validates a separately approved Persona `mail.draft_context` proposal
before creating an Apple Mail review draft. Persona approval never authorizes
a send. Relay still owns the account and recipient route, draft identity,
post-review fingerprint and authoritative send result.

## App Contributions

The carrier-root `plugins/opl-relay/opl-package.json` declares the current
role-neutral `app_contributions` and `app-contribution` CLI ABI. App consumes
structured data and opaque actions through Framework; it must not special-case
Relay identity, require a `standard_agent` role or embed Package UI code.

Framework owns the Host within runtime, Package graph and App projection.
Studio's separate DSH Host composes profile/plugin/executor and delivery
transport; public App state/action, authentication and channel callbacks join
the scopes without sharing registries, sessions or currentness. Relay remains
a Python capability behind its declared ABI and introduces no Host or second
lifecycle manager.

Current declared views and callable refs come from the descriptor and
`cli.py`, not a second hand-maintained UI inventory here. Their presence does
not prove App rendering, native-carrier installation, healthy accounts or a
completed external write. A host can show unavailable contributions locally
without blocking unrelated Packages.

Apple Mail remains the review frontend for the implemented draft workflow.
Any future App review surface must preserve the same draft identity,
fingerprint, explicit send gate and final receipt. Unified review must not
merge Persona proposal approval with Relay sending authority.
